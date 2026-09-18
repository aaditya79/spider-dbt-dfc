// Run: <pi>/node_modules/.bin/tsx pi_runner/pi_ext/lib.test.ts
import { fuzzyFindExact, safeToolName, sanitizeToolNames, stripLinePrefix, uniformIndentDelta } from "./lib.ts";
import { readFileSync } from "node:fs";
let fails = 0;
const eq = (a: unknown, b: unknown, msg: string) => {
	if (JSON.stringify(a) !== JSON.stringify(b)) { console.log("FAIL", msg, JSON.stringify(a), "!=", JSON.stringify(b)); fails++; }
	else console.log("ok  ", msg);
};
eq(safeToolName("dbt deps"), "dbt_deps", "space -> underscore");
eq(safeToolName("dbt run --profiles-dir ."), "dbt_run_--profiles-dir", "cmd with flags");
eq(safeToolName("python -c \\"), "python_-c", "trailing backslash stripped");
eq(safeToolName("   "), "invalid_tool", "empty -> placeholder");
eq(safeToolName("bash"), "bash", "valid unchanged");
const file = "select\n    a,\n    b\nfrom t\nwhere x = 1\n";
eq(fuzzyFindExact(file, "select\n    a,"), "select\n    a,", "exact passthrough");
eq(fuzzyFindExact(file, "select\n  a,\n  b"), "select\n    a,\n    b", "indent repaired");
eq(fuzzyFindExact(file, "\tfrom t\n\twhere x   =  1"), "from t\nwhere x = 1", "tabs + inner spaces repaired");
eq(fuzzyFindExact(file, "where x = 2"), null, "absent -> null");
eq(fuzzyFindExact("a\n b\na\n b\n", "a\nb"), null, "ambiguous -> null");
eq(uniformIndentDelta("    name: x\n    version: 1", "name: x\nversion: 1"), "    ", "uniform 4-space delta");
eq(uniformIndentDelta("  a\n    b", "a\n  b"), "  ", "delta on top of existing indent");
eq(uniformIndentDelta("  a\n b", "a\nb"), "", "non-uniform -> none");
eq(uniformIndentDelta("a\nb", "a\nb"), "", "identical -> none");
eq(stripLinePrefix("    name: x\n    version: 2\nplain", "    "), "name: x\nversion: 2\nplain", "strip prefix where present");
// Replay the exact assistant/toolResult messages from a cell that died on this.
const traj = "/Users/aadityapai/Desktop/spider-current/runs/pi/ecom-v2-qwen-recharge002-r2/_pi_meta/recharge002/trajectory.jsonl";
try {
	const msgs: any[] = [];
	for (const line of readFileSync(traj, "utf8").split("\n")) {
		if (!line) continue;
		const o = JSON.parse(line);
		if (o.type === "message_end" && o.message) msgs.push(o.message);
		if (o.type === "tool_execution_end") msgs.push({ role: "toolResult", toolName: o.toolName, toolCallId: o.toolCallId, content: o.result?.content });
	}
	const bad = (ms: any[]) => ms.flatMap((m) => (m.role === "assistant" ? (m.content ?? []).filter((c: any) => c.type === "toolCall").map((c: any) => c.name) : m.role === "toolResult" ? [m.toolName] : [])).filter((n) => !/^[a-zA-Z0-9_-]+$/.test(n));
	eq(bad(msgs).length > 0, true, "dead cell really contains a bad tool name (" + JSON.stringify(bad(msgs)) + ")");
	eq(sanitizeToolNames(msgs), true, "sanitizer reports a change");
	eq(bad(msgs), [], "no bad names remain after sanitizing");
	eq(sanitizeToolNames(msgs), false, "idempotent");
} catch (e) { console.log("skip replay test:", String(e).slice(0, 80)); }
process.exit(fails ? 1 : 0);
