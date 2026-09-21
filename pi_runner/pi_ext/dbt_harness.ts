/**
 * Pi extension for the spider-dbt harness. Loaded per run by run_task.py via
 * `pi -e pi_runner/pi_ext/dbt_harness.ts` (disable with --no-pi-extension).
 *
 * Fixes three things that the 2026-09-16 Qwen batch showed cost real cells,
 * none of which can be fixed from outside the Pi process:
 *
 *  1. Tool-name sanitizer (`context` hook). A model that emits a tool call
 *     whose name is a shell command ("dbt deps") poisons the session: Bedrock
 *     Converse rejects every later request (toolUse.name must match
 *     [a-zA-Z0-9_-]+). Before each LLM call, any such name in history is
 *     rewritten to a safe form. The model still sees Pi's "Tool X not found"
 *     result, so it learns; the session just no longer dies.
 *
 *  2. `edit` override. Same schema and behaviour as the built-in, plus a
 *     prepareArguments shim that (a) repairs an oldText that differs from the
 *     file only in whitespace/indentation, when the match is unique, and
 *     (b) turns an edit with newText but no oldText into a whole-file
 *     overwrite (that is what the model meant; 8 of 14 validation errors).
 *
 *  3. `read` override with a larger cap (READ_MAX_*). The built-in caps at
 *     50 KB / ~860 lines, which cut models/shopify.yml off above the target
 *     declaration. Text files only; binaries (the .duckdb) get a clear error
 *     instead of garbage.
 *
 *  4. `duckdb_sql` tool: read-only SQL against the project's DuckDB through
 *     pi_ext/duckdb_query.py (ported from codeboi07/Self-improving-Harness). The
 *     model kept calling a non-existent `python` tool -- it wants a query tool;
 *     this gives it one, with DDL/DML/ATTACH/COPY rejected so tables are only
 *     ever built by dbt.
 *
 *  5. `terminate` tool (same source): an explicit finish signal, as the Spider
 *     harness had. Returns terminate:true so Pi skips the follow-up LLM call.
 *
 * Everything here is additive and reversible: drop the -e flag and Pi is stock.
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { createEditToolDefinition } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { readFileSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { spawn } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { READ_MAX_BYTES, READ_MAX_LINES, fuzzyFindExact, looksBinary, resolveIn, sanitizeToolNames, stripLinePrefix, uniformIndentDelta } from "./lib.ts";

const PYTHON = process.env.SPIDER_PYTHON ?? "python";
const DUCKDB_HELPER = join(dirname(fileURLToPath(import.meta.url)), "duckdb_query.py");
const SQL_MAX_OUTPUT_CHARS = 30000;

function runHelper(args: string[], cwd: string, signal?: AbortSignal): Promise<{ code: number; stdout: string; stderr: string }> {
	return new Promise((resolve) => {
		const child = spawn(PYTHON, [DUCKDB_HELPER, ...args], { cwd, signal, env: { ...process.env, PYTHONUTF8: "1" } });
		let stdout = "";
		let stderr = "";
		child.stdout.setEncoding("utf8");
		child.stderr.setEncoding("utf8");
		child.stdout.on("data", (d) => (stdout += d));
		child.stderr.on("data", (d) => (stderr += d));
		child.on("error", (e) => resolve({ code: -1, stdout, stderr: stderr + String(e) }));
		child.on("close", (code) => resolve({ code: code ?? -1, stdout, stderr }));
	});
}

// -------------------------------------------------------------- extension ----

export default function (pi: ExtensionAPI) {
	// 1. Sanitize tool names in history before every LLM call.
	pi.on("context", (event) => {
		return sanitizeToolNames(event.messages as any[]) ? { messages: event.messages } : undefined;
	});

	pi.on("session_start", (_event, ctx) => {
		const cwd = ctx.cwd;

		// NOTE: --tools is an allowlist that filters extension tools out of Pi's
		// registry entirely (agent-session.ts isAllowedTool), whenever they are
		// registered. run_task.py therefore names duckdb_sql and terminate in
		// --tools; without that the model never sees them (confirmed live: it
		// shelled out to a non-existent duckdb CLI instead).
	// 4. duckdb_sql: read-only queries via the Python helper (no `duckdb` CLI here).
		pi.registerTool({
			name: "duckdb_sql",
			label: "DuckDB SQL",
			description:
				"Run a read-only SQL query against the project's DuckDB database file. Returns up to 200 rows as text when output is 'direct', or writes all rows to a CSV when output is a relative file path. Use it to inspect source tables and to verify dbt output tables. DDL/DML, ATTACH, COPY and EXPORT are rejected: build tables only with dbt.",
			promptSnippet: "Run read-only SQL against the project's DuckDB file",
			parameters: Type.Object({
				file_path: Type.String({ description: "DuckDB file, relative to the working directory (see profiles.yml)" }),
				command: Type.String({ description: "The SQL query to run" }),
				output: Type.Optional(Type.String({ description: "'direct' (default) to return rows inline, or a CSV path relative to the working directory" })),
			}),
			async execute(_id: string, params: { file_path: string; command: string; output?: string }, signal: AbortSignal | undefined, _onUpdate: unknown, ctx: any) {
				const cwd = (ctx as { cwd?: string })?.cwd ?? process.cwd();
				const r = await runHelper(["--db", params.file_path, "--sql", params.command, "--output", params.output ?? "direct"], cwd, signal);
				if (r.code !== 0) throw new Error(r.stderr.trim() || `duckdb_query.py exited with ${r.code}`);
				const out = r.stdout.length <= SQL_MAX_OUTPUT_CHARS ? r.stdout : r.stdout.slice(0, SQL_MAX_OUTPUT_CHARS) + `\n... [truncated ${r.stdout.length - SQL_MAX_OUTPUT_CHARS} chars]`;
				return { content: [{ type: "text", text: out }], details: { file_path: params.file_path, output: params.output ?? "direct" } };
			},
		} as any);

		// 5. terminate: explicit finish, as the Spider harness had.
		pi.registerTool({
			name: "terminate",
			label: "Terminate",
			description:
				"Declare the task finished. For dbt tasks the output is the DuckDB file name from profiles.yml (for example 'recharge.duckdb'), not a CSV. Call this exactly once, at the end, after `dbt run` has succeeded.",
			promptSnippet: "Finish the task and report the final answer",
			parameters: Type.Object({
				output: Type.String({ description: "Final answer: the DuckDB file name, or a literal answer if the task asks for one" }),
			}),
			async execute(_id: string, params: { output: string }) {
				return { content: [{ type: "text", text: `Terminated with output: ${params.output}` }], details: { output: params.output }, terminate: true };
			},
		} as any);

		pi.setActiveTools([...new Set([...pi.getActiveTools(), "duckdb_sql", "terminate"])]);

		// 2. edit: built-in behaviour + argument repair.
		const baseEdit = createEditToolDefinition(cwd);
		pi.registerTool({
			...baseEdit,
			prepareArguments(args: any) {
				if (!args || typeof args !== "object") return args;
				// edits passed as a JSON string
				if (typeof args.edits === "string") {
					try {
						args.edits = JSON.parse(args.edits);
					} catch {
						/* leave it; schema error will say so */
					}
				}
				// legacy top-level oldText/newText
				if (!Array.isArray(args.edits) && (typeof args.newText === "string" || typeof args.oldText === "string")) {
					args.edits = [{ oldText: args.oldText, newText: args.newText }];
					delete args.oldText;
					delete args.newText;
				}
				if (!Array.isArray(args.edits) || typeof args.path !== "string") return args;
				let content: string | null = null;
				try {
					content = readFileSync(resolveIn(cwd, args.path), "utf8");
				} catch {
					return args;
				}
				for (const e of args.edits) {
					if (!e || typeof e !== "object" || typeof e.newText !== "string") continue;
					if (typeof e.oldText !== "string" || e.oldText.length === 0) {
						// newText only => the model wants the whole file replaced
						if (args.edits.length === 1) e.oldText = content;
						continue;
					}
					if (!content.includes(e.oldText)) {
						const fixed = fuzzyFindExact(content, e.oldText);
						if (fixed !== null) {
							// The model usually indents newText the same wrong way it
							// indented oldText; undo that delta so the file keeps its
							// real indentation.
							const delta = uniformIndentDelta(e.oldText, fixed);
							e.oldText = fixed;
							if (delta) e.newText = stripLinePrefix(e.newText, delta);
						}
					}
				}
				return args;
			},
		} as any);

		// 3. read: larger cap, text only.
		pi.registerTool({
			name: "read",
			label: "Read",
			description:
				`Read the contents of a text file. Output is truncated to ${READ_MAX_LINES} lines or ${READ_MAX_BYTES / 1024}KB (whichever is hit first); use offset/limit to continue. Not for binary files such as .duckdb -- query those with bash + python.`,
			promptSnippet: "Read file contents",
			parameters: Type.Object({
				path: Type.String({ description: "Path to the file to read (relative or absolute)" }),
				offset: Type.Optional(Type.Number({ description: "Line number to start reading from (1-indexed)" })),
				limit: Type.Optional(Type.Number({ description: "Maximum number of lines to read" })),
			}),
			async execute(_toolCallId: string, params: { path: string; offset?: number; limit?: number }) {
				const abs = resolveIn(cwd, params.path);
				let buf: Buffer;
				try {
					buf = await readFile(abs);
				} catch (err: any) {
					throw new Error(`${err?.code ?? "error"}: cannot read ${params.path}${err?.code === "EISDIR" ? " (it is a directory; use ls)" : ""}`);
				}
				if (looksBinary(buf)) {
					throw new Error(`${params.path} is a binary file. For a DuckDB database use bash: python -c "import duckdb; print(duckdb.connect('${params.path}').sql('show tables'))"`);
				}
				const all = buf.toString("utf8").split("\n");
				const total = all.length;
				const start = params.offset ? Math.max(0, params.offset - 1) : 0;
				if (start >= total) throw new Error(`Offset ${params.offset} is beyond end of file (${total} lines total)`);
				let end = params.limit !== undefined ? Math.min(start + params.limit, total) : total;
				let lines = all.slice(start, end);
				// byte / line caps
				let truncated = false;
				if (lines.length > READ_MAX_LINES) {
					lines = lines.slice(0, READ_MAX_LINES);
					truncated = true;
				}
				let text = lines.join("\n");
				if (Buffer.byteLength(text, "utf8") > READ_MAX_BYTES) {
					let bytes = 0;
					const kept: string[] = [];
					for (const l of lines) {
						const b = Buffer.byteLength(l, "utf8") + 1;
						if (bytes + b > READ_MAX_BYTES) break;
						kept.push(l);
						bytes += b;
					}
					lines = kept;
					text = lines.join("\n");
					truncated = true;
				}
				end = start + lines.length;
				if (truncated || end < total) {
					text += `\n\n[Showing lines ${start + 1}-${end} of ${total}${truncated ? ` (${READ_MAX_BYTES / 1024}KB / ${READ_MAX_LINES}-line limit)` : ""}. Use offset=${end + 1} to continue.]`;
				}
				return { content: [{ type: "text", text }], details: { truncated, total } };
			},
		} as any);
	});
}
