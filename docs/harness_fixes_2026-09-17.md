# Harness fixes, 2026-09-17

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

---

## Not changed (known, deliberate)

- **Pi source** — untouched; the clone is still identical to upstream.
- **Rule 4 vs rule 8** (`dbt deps` vs "no networking") in the prompt — a policy
  call. `dbt deps` ran successfully in every holistic cell, so the network rule
  is being ignored when it matters.
- **Bash output truncation** (2000 lines / 50 KB) — Pi's default; the tail is
  kept so dbt errors survive. Left alone.
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
