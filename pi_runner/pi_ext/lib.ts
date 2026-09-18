/**
 * Pure helpers for dbt_harness.ts, kept free of Pi imports so they can be unit
 * tested with plain tsx: see pi_runner/pi_ext/lib.test.ts.
 */
import { isAbsolute, resolve } from "node:path";

export const NAME_OK = /^[a-zA-Z0-9_-]+$/;
export const READ_MAX_BYTES = 200 * 1024;
export const READ_MAX_LINES = 5000;

export function safeToolName(name: string): string {
	const s = name.replace(/[^a-zA-Z0-9_-]+/g, "_").replace(/^_+|_+$/g, "");
	return s.length > 0 ? s.slice(0, 64) : "invalid_tool";
}

/**
 * Rewrite any toolCall.name / toolResult.toolName in `messages` that Bedrock
 * would reject. Mutates in place; returns true if anything changed.
 */
export function sanitizeToolNames(messages: any[]): boolean {
	let changed = false;
	for (const m of messages) {
		if (m?.role === "assistant" && Array.isArray(m.content)) {
			for (const c of m.content) {
				if (c?.type === "toolCall" && typeof c.name === "string" && !NAME_OK.test(c.name)) {
					c.name = safeToolName(c.name);
					changed = true;
				}
			}
		} else if (m?.role === "toolResult" && typeof m.toolName === "string" && !NAME_OK.test(m.toolName)) {
			m.toolName = safeToolName(m.toolName);
			changed = true;
		}
	}
	return changed;
}

function normLine(s: string): string {
	return s.replace(/\s+/g, " ").trim();
}

/**
 * Find `needle` in `hay` ignoring differences in indentation and internal
 * whitespace runs. Returns the exact original slice when there is exactly one
 * line-aligned match, else null (ambiguous or absent -> leave it to the tool's
 * own error so the model re-reads).
 */
/**
 * If every line of `modelText` starts with the same extra leading whitespace
 * relative to the corresponding line of `fileText` (the model indented the
 * whole block), return that extra prefix; else "". Blank lines are ignored.
 */
export function uniformIndentDelta(modelText: string, fileText: string): string {
	const a = modelText.replace(/\n$/, "").split("\n");
	const b = fileText.replace(/\n$/, "").split("\n");
	if (a.length !== b.length) return "";
	let delta: string | null = null;
	for (let i = 0; i < a.length; i++) {
		if (a[i].trim() === "" && b[i].trim() === "") continue;
		const ma = a[i].match(/^[ \t]*/)![0];
		const mb = b[i].match(/^[ \t]*/)![0];
		if (!ma.startsWith(mb)) return "";
		const d = ma.slice(mb.length);
		if (delta === null) delta = d;
		else if (delta !== d) return "";
	}
	return delta ?? "";
}

/** Remove `prefix` from the start of every line of `text` that has it. */
export function stripLinePrefix(text: string, prefix: string): string {
	if (!prefix) return text;
	return text
		.split("\n")
		.map((l) => (l.startsWith(prefix) ? l.slice(prefix.length) : l))
		.join("\n");
}

export function fuzzyFindExact(hay: string, needle: string): string | null {
	if (hay.includes(needle)) return needle;
	const hayLines = hay.split("\n");
	const needleLines = needle.replace(/\n$/, "").split("\n");
	const n = needleLines.length;
	if (n === 0) return null;
	const target = needleLines.map(normLine);
	// Ignore leading/trailing blank lines in the needle for matching purposes.
	let hits: number[] = [];
	for (let i = 0; i + n <= hayLines.length; i++) {
		let ok = true;
		for (let j = 0; j < n; j++) {
			if (normLine(hayLines[i + j]) !== target[j]) {
				ok = false;
				break;
			}
		}
		if (ok) hits.push(i);
	}
	if (hits.length !== 1) return null;
	const start = hits[0];
	let slice = hayLines.slice(start, start + n).join("\n");
	if (needle.endsWith("\n") && start + n < hayLines.length) slice += "\n";
	return slice;
}

export function resolveIn(cwd: string, p: string): string {
	return isAbsolute(p) ? p : resolve(cwd, p);
}

export function looksBinary(buf: Buffer): boolean {
	const probe = buf.subarray(0, Math.min(buf.length, 8192));
	return probe.includes(0);
}

