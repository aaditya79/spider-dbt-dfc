# Harness fixes, 2026-09-17 / 18

Fixes for the problems surfaced by the first `spider_pi_2.0` Qwen batch
(`runs/pi/ecom-v2-qwen-*`, 15 cells, 2026-09-16). Each item says what was wrong,
what changed, where, and how to undo just that item. All changes are on branch
`spider_pi_2.0`, in two files plus one new tracked file; nothing in Pi itself was
touched.

Files:
- `pi_runner/run_task.py`
- `pi_runner/run_ecom_v2.sh`
- `pi_runner/pi_models.json` (new)

To revert **everything** in one go: `git checkout <commit-before> -- pi_runner/run_task.py pi_runner/run_ecom_v2.sh && git rm -q pi_runner/pi_models.json`.
To revert one item, follow its "Revert" line. Items are independent unless noted.

Evidence for each problem is in the batch table at the end.

## Status matrix (2026-09-18)

All 14 problems flagged from the 2026-09-16 Qwen batch, what was done about each,
and where it stands. Fix numbers refer to the sections below.

| # | Problem | Evidence (batch of 15) | Fix | Status | Left to do |
|---|---|---|---|---|---|
| 1 | Model emits a shell command as the tool *name* (`"dbt deps"`); Bedrock 400s every later turn; run dies and is scored 0 | 3 cells died (holistic-r3, recharge002-r2 with nothing built; holistic-r2 passed first) | 1 | **Detected & reclassified** -- `verdict: HARNESS_ERROR`, `score: null`, scorer verdict kept in `verdict_scorer` | Not *prevented*: needs a Pi extension to rewrite the call before it enters history. Fix 6 tells the model not to. |
| 2 | `agent_settled` after an API error treated as a normal finish | same cells + recharge002-r3 (DNS drop) | 1 | **Fixed** -- `stopReason == "error"` -> `harness_error.kind = api_error` | -- |
| 3 | `edit` exact-match failures burn turns | 55 failed edits (41 no-match, 14 bad args) | 6 | **Mitigated** by wording (read-before-edit, small edits, fall back to `write`) | Option (a): drop `edit` from `--tools` for Qwen so it must `write` whole files, as the Spider `EditFile` did. One flag, no code. Option (b): fuzzy-match extension (Pi side). |
| 4 | Calls to a non-existent `python` tool | 5 | 6 | **Fixed** by wording ("no `python` tool; run Python through bash") | verify in v3 batch |
| 5 | `read` with no `path` | 6 (one per cell) | 6 | **Fixed** by wording | verify in v3 batch |
| 6 | `read` 50 KB cap truncates `models/shopify.yml` above the target declaration (`shopify__daily_shop` at line 915/1041) | shopify001-r1, shopify002-r1 | 6 | **Mitigated** -- prompt says use `grep -n "  - name: "` then `read` with `offset` | The cap itself is Pi's. Not changing. |
| 7 | No step cap; only wall-clock timeout. Runs plateau for 30-55 min | recharge002-r1 176 calls, shopify002-r2 174 calls, smoke 143 | 4 | **Fixed** (opt-in) -- `--max-tool-calls N`, Pi `abort`, `kind: tool_call_cap`. Default 0 = off | Decide default N (Spider used 30) and whether capped = 0 or null. |
| 8 | Bash output truncation (2000 lines / 50 KB) on 105-model `dbt run`s | 2 | -- | **Not fixed** -- constants in Pi's `bash.ts`, no settings hook | Only by patching Pi. Tail is kept so dbt errors survive. |
| 9 | Cost / tokens recorded from the last turn only | smoke run: $0.03 reported vs $2.56 actual (144 turns, 11.6 M input) | 3 | **Fixed** -- summed per assistant message; `usage_total` in record | Old records need recompute from `trajectory.jsonl`. |
| 10 | Rule 4 (`dbt deps`) contradicts rule 8 (no networking) | every passing holistic cell broke rule 8 | 8 | **Fixed** -- rule 8 names `dbt deps` as the exception. Scaffold v3 | -- |
| 11 | Wrong / missing target name (never reads the schema yml, or builds `int_` prefix) | recharge001-r1/r2, recharge002-r1 | -- | **Not a harness bug** -- this is the research question | DFC `namegate` arm over the same cells, after the v3 re-baseline. |
| 12 | `DEFAULT_PI` points at a path that no longer exists | every driver passed `--pi` | 5 | **Fixed** -- `_find_pi()`: `$PI_BIN`, then known clone paths | -- |
| 13 | Qwen only works with an untracked `~/.pi/agent/models.json`; fresh machine fails silently | -- | 7 | **Fixed** -- `pi_runner/pi_models.json` tracked; `check_models_json()` refuses to start without it | `docs/qwen_arm_blocker.md` still says Qwen is blocked; needs a stale-note. |
| 14 | No prompt caching for Qwen; every turn resends full context | $1.6-2.6 per long cell | -- | **Not fixable** -- Bedrock / model limitation | Cost only; bounded by fix 7 if a cap is set. |

Totals: 8 fixed, 2 mitigated (3, 6), 1 partially (1: detected not prevented),
3 not fixable / not a harness bug (8, 11, 14).

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
