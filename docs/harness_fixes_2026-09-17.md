# Harness fixes, 2026-09-17 / 18 / 21

Fixes for the problems surfaced by the first `spider_pi_2.0` Qwen batch
(`runs/pi/ecom-v2-qwen-*`, 15 cells, 2026-09-16). Each item says what was wrong,
what changed, where, and how to undo just that item. All changes are on branch
`spider_pi_2.0`, in two files plus one new tracked file; nothing in Pi itself was
touched.

Files:
- `pi_runner/run_task.py`
- `pi_runner/run_ecom_v2.sh`
- `pi_runner/pi_models.json` (new)
- `pi_runner/pi_ext/dbt_harness.ts`, `lib.ts`, `lib.test.ts` (new, fix 9)

To revert **everything** in one go: `git checkout <commit-before> -- pi_runner/run_task.py pi_runner/run_ecom_v2.sh && git rm -q pi_runner/pi_models.json`.
To revert one item, follow its "Revert" line. Items are independent unless noted.

Evidence for each problem is in the batch table at the end.

## Status matrix (2026-09-18)

All 14 problems flagged from the 2026-09-16 Qwen batch, what was done about each,
and where it stands. Fix numbers refer to the sections below.

| # | Problem | Evidence (batch of 15) | Fix | Status | Left to do |
|---|---|---|---|---|---|
| 1 | Model emits a shell command as the tool *name* (`"dbt deps"`); Bedrock 400s every later turn; run dies and is scored 0 | 3 cells died (holistic-r3, recharge002-r2 with nothing built; holistic-r2 passed first) | 1, 9 | **Fixed** -- fix 9's `context` hook rewrites any such name in history before every LLM call, so the 400 never happens. Fix 1's detection stays as the safety net (`HARNESS_ERROR` if it somehow still dies). | verify on a v3 batch (unit-tested against the real dead-cell messages; not yet provoked live) |
| 2 | `agent_settled` after an API error treated as a normal finish | same cells + recharge002-r3 (DNS drop) | 1 | **Fixed** -- `stopReason == "error"` -> `harness_error.kind = api_error` | -- |
| 3 | `edit` exact-match failures burn turns | 55 failed edits (41 no-match, 14 bad args) | 6, 9 | **Fixed** -- fix 9 overrides `edit`: whitespace/indent-mismatched `oldText` is repaired when the match is unique (and the same indent delta is stripped from `newText`); `newText`-only edits become a whole-file overwrite; `edits` as a JSON string and legacy top-level `oldText/newText` are coerced. Live-verified. | Content mismatches (not whitespace) still fail as before -- correctly. `RUN_TASK_EXTRA="--tools read,bash,write,grep,find,ls"` remains available as a no-`edit` arm. |
| 4 | Calls to a non-existent `python` tool | 5 | 6 | **Fixed** by wording ("no `python` tool; run Python through bash") | verify in v3 batch |
| 5 | `read` with no `path` | 6 (one per cell) | 6 | **Fixed** by wording | verify in v3 batch (schema validation still rejects it, which is correct) |
| 6 | `read` 50 KB cap truncates `models/shopify.yml` above the target declaration (`shopify__daily_shop` at line 915/1041) | shopify001-r1, shopify002-r1 | 6, 9 | **Fixed** -- fix 9 overrides `read` with a 200 KB / 5000-line cap (`shopify.yml` now comes back whole: 1041/1041 lines, live-verified). Binary files (the `.duckdb`) get a clear error pointing at the python one-liner instead of garbage. | -- |
| 7 | No step cap; only wall-clock timeout. Runs plateau for 30-55 min | recharge002-r1 176 calls, shopify002-r2 174 calls, smoke 143 | 4 | **Fixed** (opt-in) -- `--max-tool-calls N`, Pi `abort`, `kind: tool_call_cap`. Default 0 = off | Decide default N (Spider used 30) and whether capped = 0 or null. |
| 8 | Bash output truncation (2000 lines / 50 KB) on 105-model `dbt run`s | 2 | -- | **Not fixed** -- constants in Pi's `bash.ts`, no settings hook | Only by patching Pi. Tail is kept so dbt errors survive. |
| 9 | Cost / tokens recorded from the last turn only | smoke run: $0.03 reported vs $2.56 actual (144 turns, 11.6 M input) | 3 | **Fixed** -- summed per assistant message; `usage_total` in record | Old records need recompute from `trajectory.jsonl`. |
| 10 | Rule 4 (`dbt deps`) contradicts rule 8 (no networking) | every passing holistic cell broke rule 8 | 8 | **Fixed** -- rule 8 names `dbt deps` as the exception. Scaffold v3 | -- |
| 11 | Wrong / missing target name (never reads the schema yml, or builds `int_` prefix) | recharge001-r1/r2, recharge002-r1 | -- | **Not a harness bug** -- this is the research question | DFC `namegate` arm over the same cells, after the v3 re-baseline. |
| 12 | `DEFAULT_PI` points at a path that no longer exists | every driver passed `--pi` | 5 | **Fixed** -- `_find_pi()`: `$PI_BIN`, then known clone paths | -- |
| 13 | Qwen only works with an untracked `~/.pi/agent/models.json`; fresh machine fails silently | -- | 7 | **Fixed** -- `pi_runner/pi_models.json` tracked; `check_models_json()` refuses to start without it | `docs/qwen_arm_blocker.md` still says Qwen is blocked; needs a stale-note. |
| 14 | No prompt caching for Qwen; every turn resends full context | $1.6-2.6 per long cell | -- | **Not fixable** -- Bedrock / model limitation | Cost only; bounded by fix 7 if a cap is set. |

Totals (after fix 9): 11 fixed, 3 out of scope (8: Pi-internal bash cap,
11: the research question, 14: model limitation).

Scaffold versions: v1 = the 2026-09-16 batch; v2 = fix 6; v3 = fix 8 (current).
v1 numbers are not comparable to v2/v3 -- re-baseline.

---

## 1. Malformed tool name → Bedrock 400 → run dies, scored as a model 0

**Problem.** Qwen sometimes emits a tool call whose *name* is a shell command
(`"dbt deps"`, `"dbt run --profiles-dir ."`, `"python -c \"`). Pi answers that one
call with "Tool not found" but keeps the message in history; Bedrock Converse then
rejects every later request (`toolUse.name … must satisfy [a-zA-Z0-9_-]+`). Pi
records `stopReason: "error"`, `willRetry: false`, the session settles, and the
runner scored whatever was on disk. 3 of 15 cells died this way (2 with nothing
built); the run record was indistinguishable from a genuine failure.

**Fix.** `drive_round()` now
- checks every `tool_execution_start.toolName` against `TOOL_NAME_OK`
  (`^[a-zA-Z0-9_-]+$`) and records `harness_error = {kind: "bad_tool_name", …}`;
- checks every assistant `message_end` for `stopReason == "error"` and records
  `harness_error = {kind: "api_error", message: …}` (this also catches the
  network/DNS drop that killed recharge002-r3);
- writes `harness_error` into the `_runner_round_end` marker and the run record.

`main()` then: if `harness_error` is set **and the run did not pass**, sets
`verdict = "HARNESS_ERROR"`, `score = None`, and keeps the scorer's own verdict in
`verdict_scorer`. A run that passed despite a late harness error keeps its 1 (the
scorer is authoritative; holistic-r2 is the real example).

`score = None` is what makes the resumable drivers treat the cell as not done.

**Where.** `run_task.py`: `TOOL_NAME_OK`, the `tool_execution_start` /
`message_end` branches of `drive_round`, and the `HARNESS_ERROR` block after
`record["verdict"]` in `main`.

**Revert.** Delete the `TOOL_NAME_OK` constant, the two `r["harness_error"] = …`
blocks in `drive_round`, `"harness_error"` from the round-end marker and the
record, and the `if record["harness_error"] and record["score"] != 1:` block.
Runs then go back to scoring harness deaths as 0.

**Not fixed.** The model still *emits* the bad name; nothing here prevents the
first occurrence. Preventing it would need a Pi extension that rewrites the tool
call before it enters history, which touches the Pi side. Item 6 reduces the
frequency from the prompt side instead.

## 2. Driver re-runs harness-error cells

**Problem.** Even with item 1, a dead cell just sat there with `score: null`
until someone re-invoked the driver.

**Fix.** `run_ecom_v2.sh` wraps each cell in a retry loop: up to
`HARNESS_RETRIES` (default 2) extra attempts while the verdict is
`HARNESS_ERROR`. Any other verdict breaks out. The per-cell log line now prints
`verdict=` and the harness-error kind.

**Where.** `run_ecom_v2.sh`, the `for ATTEMPT in …` loop inside the seed/task
loops; `HARNESS_RETRIES` and `MAX_TOOL_CALLS` env defaults near the top.

**Revert.** Replace the loop body with the single `run_task.py` invocation it
wrapped (see `git show 33b643b:pi_runner/run_ecom_v2.sh`). Or set
`HARNESS_RETRIES=0` to disable without editing.

## 3. Token / cost accounting was last-turn-only

**Problem.** `rounds[].usage` came from `agent_end.messages[-1].usage`, i.e. the
final turn. The recharge001 smoke run reported `$0.03`; summing every assistant
message gives **$2.56** (144 turns, 11.6 M input tokens — Qwen has no prompt
caching, so every turn resends the full context). ~80× under-count.

**Fix.** `_add_usage()` accumulates `input/output/cacheRead/cacheWrite/
totalTokens/cost_total/turns` from every assistant `message_end` in the round.
`rounds[].usage` is now that sum; the record also gets `usage_total` across
rounds (`_sum_usage`).

**Where.** `run_task.py`: `USAGE_KEYS`, `_add_usage`, `_sum_usage`, the
`message_end` branch in `drive_round`, `"usage_total"` in the record.

**Revert.** Restore `r["usage"] = m.get("usage")` in the `agent_end` branch,
drop the `_add_usage` call and `usage_total`. Note `analyze_runs.py` was already
summing per-message and is unaffected either way.

**Reading old records.** Any `run_record.json` written before this commit has
the last-turn number in `rounds[].usage`; recompute from `trajectory.jsonl` if
you need cost.

## 4. Optional tool-call cap (`--max-tool-calls`)

**Problem.** Pi has no step cap; the only bound was the wall-clock `--timeout`.
Stuck runs plateaued (8 identical `dbt run`s at PASS=25/ERROR=5) for 30–55 min.
The Spider harness capped at 30 steps, which also makes Pi numbers not directly
comparable.

**Fix.** `run_task.py --max-tool-calls N` (default **0 = off**, so nothing
changes unless you ask). When a round reaches N tool calls the runner sends Pi
`{"type": "abort"}`; Pi settles normally, the round is marked `capped: true`,
and unless the run passed anyway it is recorded as `HARNESS_ERROR` with
`kind: "tool_call_cap"` (so it is *not* counted as a model failure, and the
driver will retry it — set `HARNESS_RETRIES=0` if you want a capped run to be
final). `run_ecom_v2.sh` passes `MAX_TOOL_CALLS` through.

Verified live: cap 3 → abort → settled in 8 s, record as described.

**Where.** `run_task.py`: `max_tool_calls` parameter on `drive_round`, the
`sess.send({"type": "abort"})` branch, the `capped` post-processing, the
argparse flag, both `drive_round(...)` call sites. `run_ecom_v2.sh`:
`MAX_TOOL_CALLS`.

**Revert.** Leave it at the default 0 — behaviour is identical to before. To
remove: delete the flag, the parameter, and the `if max_tool_calls …` branch.

**Open question.** Whether a capped run should score 0 (like the Spider harness
did at 30 steps) or `null`. Currently `null`; change the `record["score"] != 1`
condition to exclude `kind == "tool_call_cap"` if you want 0.

## 5. `DEFAULT_PI` was a dead path

**Problem.** `~/Desktop/DAPLab/pi/pi-test.sh` stopped existing when the Desktop
moved; every driver had to pass `--pi`.

**Fix.** `_find_pi()` returns the first existing launcher from `$PI_BIN`, then
the current clone path, then the old path, then `~/pi/pi-test.sh`. Falls back
to the old string so the error still names something.

**Where.** `run_task.py` top, replacing the one-line `DEFAULT_PI`.

**Revert.** Restore `DEFAULT_PI = os.path.expanduser("~/Desktop/DAPLab/pi/pi-test.sh")`.

## 6. `dbt` scaffold wording, v2 (prompt-side mitigation of items 1 and the turn sinks)

**Problem.** Counted across the batch: 55 failed `edit` calls (41 exact-match
misses, 14 malformed args), 5 calls to a non-existent `python` tool, 6 `read`
calls with no `path`, 2 `read`s of the `.duckdb` binary, and `models/shopify.yml`
truncated by `read`'s 50 KB cap *above* the `shopify__daily_shop` declaration
(line 915 of 1041).

**Fix.** `DBT_SYSTEM_PROMPT` `# TOOLS` section now says, in order:
- the seven tool names are exact and a shell command is never a tool name
  (targets item 1's root cause);
- `read` requires `path`; it caps at ~50 KB / 860 lines; use
  `grep -n "  - name: " models/*.yml` then `read` with `offset`;
- prefer `write` for new model files / large replacements; `edit` needs a
  byte-exact `oldText`, read immediately before, and fall back to `write` after
  two failures;
- there is no `python` **tool**; run Python through `bash`;
- do not `read` the `.duckdb` file.

The `# DBT PROJECT RULES` section and `TASK_TEMPLATE` are unchanged.

`SCAFFOLD_VERSION = 2` is recorded in every run record so v1 (the 2026-09-16
batch) and v2 runs are separable without diffing `system_prompt`.

**Where.** `run_task.py`: `DBT_SYSTEM_PROMPT` (`# TOOLS` block only),
`SCAFFOLD_VERSION`, `"scaffold_version"` in the record.

**Revert.** `git show 33b643b:pi_runner/run_task.py` has the v1 text; paste the
`# TOOLS` block back and set `SCAFFOLD_VERSION = 1`. Old run records carry the
exact `system_prompt` they ran with regardless.

**Caveat.** This is a prompt change, so v2 numbers are not directly comparable
to the v1 batch. Re-baseline before comparing.

## 7. Qwen `models.json` tracked + preflight check

**Problem.** Qwen only works if `~/.pi/agent/models.json` registers the
inference-profile ARN with `maxTokens: 65536`. That file lives outside the repo;
a fresh machine fails every call with a max-tokens validation error and nothing
in the runner says why. `docs/qwen_arm_blocker.md` still describes Qwen as
blocked (its diagnosis was right; the fix was the `models.json` *format* —
`providers.amazon-bedrock.models[]`).

**Fix.** `pi_runner/pi_models.json` is a tracked copy. `check_models_json()`
runs before Pi is spawned: if `--model` starts with `arn:` and the agent dir's
`models.json` does not list that id, the run exits immediately with a
`cp` instruction. Respects `PI_CODING_AGENT_DIR`.

**Where.** `run_task.py`: `PI_MODELS_JSON`, `check_models_json`, the call just
before `build_env` in `main`. New file `pi_runner/pi_models.json`.

**Revert.** Delete the function and its call; `git rm pi_runner/pi_models.json`.
Runs would then fail at the first API call instead of at startup.

## 8. Rule 4 vs rule 8: `dbt deps` is now the named networking exception (scaffold v3)

**Problem.** Rule 4 said to run `dbt deps` when `dbt_packages/` is missing; rule 8
said "do not use networking". `dbt deps` fetches from the dbt hub. The
`shopify_holistic_reporting001` fixture ships without `dbt_packages/`, so every
run of it had to break one rule -- and every cell that passed broke rule 8.

**Fix.** Rule 8 now reads: "Do not use networking (the one exception is
`dbt deps`, which fetches packages), …". Rule 4 unchanged. `SCAFFOLD_VERSION = 3`.

**Where.** `run_task.py`: `DBT_SYSTEM_PROMPT` rule 8; `SCAFFOLD_VERSION`.

**Revert.** Restore the rule 8 line from `git show 3a9f0ea:pi_runner/run_task.py`
and set `SCAFFOLD_VERSION = 2`.

**Alternative not taken.** Vendoring `dbt_packages/` into the holistic fixture
would remove the need for network entirely, but the fixtures are copied pristine
from `Spider2/` by design (the `order_data` blocker is part of the task), so
patching one is out of scope here.

## 9. Pi extension: tool-name sanitizer, `edit` repair, larger `read` (2026-09-18)

**Problem.** Items 1, 3 and 6 could only be mitigated from outside Pi: the
Bedrock 400 happens because the bad tool name is *in Pi's message history*; the
`edit` failures are Pi's exact-match rule; the `read` cap is Pi's constant.
Patching Pi was ruled out (keep the clone identical to upstream).

**Fix.** Pi's extension API covers all three without touching Pi source. One
file, `pi_runner/pi_ext/dbt_harness.ts`, loaded per run with `-e`:

- `context` hook (fires before every LLM call, may rewrite the messages):
  any `toolCall.name` / `toolResult.toolName` outside `[a-zA-Z0-9_-]+` is
  rewritten (`"dbt deps"` -> `dbt_deps`). The model still sees Pi's "Tool X not
  found" result; the session just no longer dies.
- `edit` override (`pi.registerTool` with the built-in's name replaces it;
  wraps `createEditToolDefinition` so behaviour is otherwise identical) with a
  `prepareArguments` shim: `edits` given as a JSON string is parsed; legacy
  top-level `oldText/newText` is wrapped; an edit with `newText` but no
  `oldText` (8 of the 14 validation errors) becomes a whole-file overwrite;
  an `oldText` that differs from the file only in indentation / whitespace
  runs is replaced by the exact file slice **when the match is unique**, and
  the same indentation delta is stripped from `newText` so the file keeps its
  real indentation (without that, a 4-space-indented edit of
  `dbt_project.yml` produced invalid YAML -- caught in the live test).
- `read` override: 200 KB / 5000 lines, same `path/offset/limit` schema and
  the same `[Showing lines a-b of n. Use offset=…]` footer. Binary files get
  an error naming the python one-liner instead of 1 line of garbage.

Pure logic lives in `pi_runner/pi_ext/lib.ts` (no Pi imports) with tests in
`lib.test.ts` (`<pi>/node_modules/.bin/tsx pi_runner/pi_ext/lib.test.ts`),
including a replay of the exact messages from the dead `recharge002-r2` cell.

`run_task.py`: `--pi-extension <file>` (default this one), `--no-pi-extension`
for stock Pi. `pi_extension` is recorded in the run record and the startup log.
`run_ecom_v2.sh`: `RUN_TASK_EXTRA` passes arbitrary args through for A/B arms.

**Verified live** (shopify001 fixture, Qwen, scripted prompts): `read
models/shopify.yml` returns 1041/1041 lines (built-in: 864); an `edit` with
4-space over-indented `oldText` succeeds and the file stays unindented; a
`newText`-only edit overwrites `packages.yml`; `read shopify.duckdb` returns
the binary-file error and the model then used the suggested query. The
sanitizer is unit-tested against the real dead-cell trace but has not been
provoked live (v2/v3 prompt wording steered the model into `bash` instead).

**Where.** New: `pi_runner/pi_ext/{dbt_harness.ts,lib.ts,lib.test.ts}`.
`run_task.py`: `PI_EXTENSION`, the two argparse flags, the `-e` append in
`main`, `"pi_extension"` in the record. `run_ecom_v2.sh`: `RUN_TASK_EXTRA`.

**Revert.** Without editing: `--no-pi-extension` (or
`RUN_TASK_EXTRA="--no-pi-extension"`) runs stock Pi tools. To remove: delete
`pi_runner/pi_ext/`, the `PI_EXTENSION` constant, the two flags and the
`pi_extra += ["-e", …]` block. Pi itself is untouched either way.

**Interaction with other fixes.** Fix 1 (detection) stays: if a session still
dies for any reason it is `HARNESS_ERROR`, not 0. Fix 6's wording about tool
names, `read` caps and `edit` remains accurate but is now belt-and-braces.

## 10. `duckdb_sql` + `terminate` tools, scaffold v4 (2026-09-21)

Ported from `codeboi07/Self-improving-Harness` (`ext/spider_tools.ts`,
`ext/duckdb_query.py`, reference only -- nothing is pushed there).

**Problem.** Qwen kept calling a non-existent `python` tool: it wanted a query tool
and we had told it to shell out with a quoted one-liner. It also had no explicit
finish, so runs ended when it stopped talking (or never).

**Fix.** Two tools registered by `pi_ext/dbt_harness.ts`: `duckdb_sql` (read-only;
`pi_ext/duckdb_query.py` rejects DDL/DML/ATTACH/COPY/EXPORT/LOAD by statement type)
and `terminate` (returns `terminate: true`, so Pi skips the follow-up LLM call).
Scaffold v4 describes both and adds rule 9 (never copy `dbt_packages/` into
`models/`; macros never in `models/`; build the target first) after both v3 smoke
cells died on macros-as-models.

**Gotcha found.** Pi's `--tools` is an allowlist that filters extension tools out of
the registry entirely (`agent-session.ts` `isAllowedTool`), whenever they are
registered. `DEFAULT_TOOLS` now names `duckdb_sql,terminate`; `--no-pi-extension`
strips them again. Without this the model shelled out to a non-existent duckdb CLI.

**Result (v4 smoke, 5 tasks).** 0 hallucinated tool names, 0 shell-outs for
queries, `terminate` called correctly in 4/4 finished cells, run time 1-6 min.

**Revert.** `--no-pi-extension`; or delete the two `registerTool` blocks and
`pi_ext/duckdb_query.py`, and drop the two names from `DEFAULT_TOOLS`.

## 11. DFC policy `shape`, scaffold v5 (2026-09-21)

**Problem.** Every v4 cell built the right-named table, `dbt run` was green, the
model called `terminate` -- and 4/4 scored 0: recharge001 emitted 18 columns, none
of the 9 declared; holistic dropped columns; daily_shop had 10 rows of activity days
instead of one per calendar day (2077); discounts fanned out to 6 rows with NULL
keys. The model treats the YAML as a name lookup, not a spec.

**Fix.** `dfc/spider_agent/agent/dfc_check_shape.py`, `--dfc-policy shape`.
Gold-free, task-agnostic. For each declared-but-unbuilt model that HAS a table:
every declared column present; the YAML's uniqueness test holds with dbt semantics
(`group by key having count(*) > 1`; NULLs grouped, since gold has NULL key parts);
dense daily models (declared key absent or <= 2 cols incl. `date_day`) must cover
>= 50% of the project's calendar/spine days. Absent tables are skipped (namegate's
job, and the shopify fixtures declare other tasks' models). Scaffold v5 rule 10
asks the model to run the same checks itself before `terminate`.

Safe to enforce columns: the scorer matches gold columns by value against any pred
column, so extra/renamed columns never cost a pass. Verified on the v4 cells: catches
recharge001 / shopify001 / shopify002; passes both v1 gold-passes' tables.

**Known limit.** Columns the YAML does not declare cannot be checked (the v4
holistic failure was 4 `klaviyo_sum_revenue_*` columns; the YAML declares 28 of the
gold's 47).

**Result (v5 + shape, 5 tasks).** shopify002 PASS (shape steered a missing
column), holistic PASS, recharge001 shape-clean but `amount` wrong (the existing
`recharge001` checker's domain: run `shape,recharge001`), shopify001 dense but 2820
vs 2077 rows (fixture calendar runs to today, gold cut 2024-09 -- not model-fixable),
recharge002 stalled.

**Revert.** Don't pass `--dfc-policy shape`. To remove: delete the file and its two
entries in `run_task.py` (`DFC_POLICIES`, `_load_dfc_checker`).

## 12. Stall watchdog + streaming deltas dropped (2026-09-21)

**Problem.** Twice today 3/5 and 2/3 parallel cells went silent within the same
30 s (Bedrock-side), mid-stream. Pi has no read timeout, so each sat until the
40-min wall clock and was scored 0. Separately, `message_update` /
`tool_execution_update` per-token events were ~70% of every trajectory.jsonl
(714 of 883 lines even in the v1 traces).

**Fix.** `--stall-timeout` (default 600 s): no Pi event for that long -> `abort`,
`harness_error.kind = stall`, `HARNESS_ERROR` unless the run had already passed
(shopify002 did: stalled in the retry round after the table was correct, kept its
1). Both delta types added to `TRAJ_DROP` (1022 -> 264 lines on a v5 cell).

**Revert.** `--stall-timeout 0`; remove the two names from `TRAJ_DROP`.

## 13. Scorer patch: numeric sort before tolerance compare (2026-09-21)

Ported one hunk from `codeboi07/Self-improving-Harness` (`d5cfe39`) into
`Spider2/spider2-dbt/evaluation_suite/eval_utils.py`: `ignore_order` sorted
values as strings, so floats differing in a rounding tail could swap and fail a
correct table. Re-scored all 17 cells on disk: no score changed. Lives in the
gitignored `Spider2/` clone; revert with
`git -C Spider2 checkout -- spider2-dbt/evaluation_suite/eval_utils.py`.

Also checked and NOT applicable: their finding that 11/68 shipped DuckDBs already
contain the graded tables -- none of the 5 e-commerce fixtures do.

---

## Not changed (known, deliberate)

- **Pi source** — untouched; the clone is still identical to upstream.
- **Bash output truncation** is Pi's `DEFAULT_MAX_LINES`/`DEFAULT_MAX_BYTES`
  (2000 / 50 KB), hardcoded in `packages/coding-agent/src/core/tools/bash.ts`
  with no settings hook -- changing it means patching Pi. Left alone.
- **`pi_runner/namegate_bin/`** — untracked `dbt` shim from earlier work,
  unreferenced; not part of this change.
- **The `--dfc-policy` loop** — unchanged. Harness-error detection applies to
  DFC retry rounds too (same `drive_round`).

## What to re-run

The three harness-killed cells from the 2026-09-16 batch:
`ecom-v2-qwen-shopify_holistic_reporting001-r3`, `ecom-v2-qwen-recharge002-r2`,
`ecom-v2-qwen-recharge002-r3`. Their `stdout.json` files hold `score: 0`, so the
driver will *skip* them; delete those three `.stdout.json` files first, then
`TASKS=<task> pi_runner/run_ecom_v2.sh qwen 3`. Note they will run under
scaffold v2, not v1.

## Reference: the batch that motivated this

Qwen3-235B, `dbt` scaffold v1, 2026-09-16.

| task | r1 | r2 | r3 |
|---|---|---|---|
| shopify001 | 0 — `daily_shop` 2/2077 rows | 0 — `daily_shop` 10/2077 | 0 — `daily_shop` 2/2077 |
| shopify002 | 0 — `price_rule_id` | 0 — 0 rows, timeout (DNS window) | **1** |
| shopify_holistic_reporting001 | 0 — target not built | **1** (bad tool name after build) | HARNESS — bad tool name |
| recharge001 | 0 — wrong name | 0 — wrong name | 0 — `amount`, `title` |
| recharge002 | 0 — built as `int_…` | HARNESS — bad tool name | HARNESS — DNS drop |

2/15 raw; 2/12 excluding harness deaths.
