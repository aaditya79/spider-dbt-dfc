# DFC on Spider 2.0-DBT: e-commerce results

Consolidated for the team call. Every number below was re-derived from the saved run
records (`runs/pi/*.stdout.json`, trajectories, produced DuckDBs) at write time, not
carried over from notes. Claims are traced to the artifact they come from.

---

## 1. Setup

**Harness.** `pi_runner/run_task.py` drives one spider2-dbt task end to end:

1. copy the **pristine** fixture `spider2-dbt/examples/<task>/` to a fresh run dir
   (copied as-is, deliberately not pre-patched — the `order_data` compile blocker in
   `dbt_project.yml` is part of the task);
2. spawn `pi --mode rpc` with cwd = the run dir;
3. send the task instruction, stream JSONL events to `trajectory.jsonl`, drive to
   `agent_settled`;
4. optionally run a DFC policy checker on the produced DuckDB and steer (§4-5);
5. score with `score_run.py` / `duckdb_match` against `evaluation_suite/gold/`.

**Scoring is always official.** DFC checkers only *steer*; the 0/1 always comes from
`duckdb_match`. This separation is enforced structurally — the checker never sees gold.

**The harness is validated and not task-specific.** It drives tasks outside the
e-commerce family to a scored result: `gen-tpch001` (tpch001, a cold task from a
different domain) ran to `settled=True` and scored, as did `gen-recharge002`
(score 1) and `gen-shopify001`. Cold-task coverage means the harness generalizes;
it does not mean the agent passes those tasks.

**No step cap.** Pi exposes no step/turn/iteration limit, and none exists in its agent
loop source. The only bound is wall-clock `--timeout` (2400s here). Empirically the
highest turn count across all saved runs is **85** (`haiku-on-r3`), and 14 runs exceed
50 turns — so nothing in this report was truncated by a cap. This differs from the old
50-step harness and matters for comparability.

**Scope.** 5 e-commerce tasks x 2 models, plain baseline (no DFC):
`shopify001`, `shopify002`, `shopify_holistic_reporting001`, `recharge001`,
`recharge002` x Opus 4.8 / Haiku 4.5.

---

## 2. Baseline results (10 cells)

### The headline gap

| model | n | dbt-artifact | target built | **OFFICIAL pass** | settled/cap | avg turns | avg edits | avg dbt runs | total $ |
|---|---|---|---|---|---|---|---|---|---|
| Opus 4.8 | 5 | 5/5 | 3/5 | **2/5** | 5/5 | 19.0 | 2.6 | 9.2 | $2.96 |
| Haiku 4.5 | 5 | 5/5 | 3/5 | **1/5** | 5/5 | 43.2 | 3.6 | 14.4 | $1.47 |

Three deliberately separate signals:

- **dbt-artifact** — the produced project's `target/run_results.json` shows a dbt run
  with no errored node. Closest analog to a "did dbt run" metric.
- **target built** — every scored `condition_tab` exists and is non-empty
  (`score_report.all_targets_materialized_nonempty`).
- **OFFICIAL** — `duckdb_match` vs gold.

**dbt-artifact success is 10/10 (100%). Official pass is 3/10 (30%).** Every run
produced a clean `dbt run`. A metric that stops at the dbt artifact reports total
success on a task set where 70% of the work is wrong. This gap is the single most
important number in the baseline.

### Per cell

| model | task | dbt_ok | target built | OFFICIAL | turns | edits | dbt | wall | $ | failing columns |
|---|---|---|---|---|---|---|---|---|---|---|
| opus | shopify001 | YES | YES | FAIL | 15 | 2 | 4 | 103s | 0.455 | 14 cols of `shopify__daily_shop` |
| opus | shopify002 | YES | YES | **PASS** | 13 | 1 | 4 | 126s | 0.486 | — |
| opus | holistic | YES | YES | **PASS** | 29 | 3 | 19 | 205s | 0.963 | — |
| opus | recharge001 | YES | no | FAIL | 20 | 5 | 12 | 147s | 0.576 | target absent |
| opus | recharge002 | YES | no | FAIL | 18 | 2 | 7 | 136s | 0.479 | target absent |
| haiku | shopify001 | YES | no | FAIL | 39 | 4 | 10 | 193s | 0.234 | targets absent |
| haiku | shopify002 | YES | no | FAIL | 46 | 6 | 17 | 255s | 0.308 | target absent |
| haiku | holistic | YES | YES | **PASS** | 39 | 2 | 16 | 242s | 0.325 | — |
| haiku | recharge001 | YES | YES | FAIL | 47 | 4 | 14 | 221s | 0.290 | `[6]amount` |
| haiku | recharge002 | YES | YES | FAIL | 45 | 2 | 15 | 248s | 0.316 | `[37]active_months_to_date` |

Total spend $4.43; all 10 settled naturally.

### Foundational fact: the target name is always discoverable

Each task declares its exact target in a fixture `models/*.yml`. No cell failed for
lack of available information.

| task | declaring file | lines | target declared at |
|---|---|---|---|
| recharge001 | `models/recharge.yml` | **41** | 4 |
| recharge002 | `models/recharge.yml` | 402 | 128 |
| shopify001 | `models/shopify.yml` | 1041 | **472**, **915** |
| shopify002 | `models/shopify.yml` | 963 | **847** |
| holistic | `models/shopify_holistic_reporting.yml` | 447 | 138 |

### Failure taxonomy — the four buckets

**Bucket 1 — WHY PASS (3 cells).** All three read the declaring YAML *before* the first
write and named the exact target in their own reasoning first. They never entered the
wrong-name trap, so no recovery was needed.

| cell | YAML read | first write |
|---|---|---|
| opus/shopify002 | **T6** `read models/shopify.yml offset:847 limit:120` | T10 |
| opus/holistic | **T3** `read models/shopify_holistic_reporting.yml` (whole) | T7 |
| haiku/holistic | **T9** `read models/shopify_holistic_reporting.yml` (whole) | T16 |

Two effective routes: *grep-then-seek* on the large file (opus/shopify002:
`T5 grep {"pattern":"discount","glob":"models/**/*.yml"}` -> `T6 read offset:847`,
which is exactly the declaration line), and *read-whole* on the medium file.

> haiku/holistic, pre-write: *"The YAML file already references
> `shopify_holistic_reporting__daily_customer_metrics` which seems to be the model I
> need to create/update."*

**Bucket 2 — WRONG-NAME FAIL (4 cells).** See §3a.

**Bucket 3 — CONTENT FAIL (2 cells).** Correct name, wrong values. See §3b.

**Bucket 4 — CROSS-MODEL.**

| task | Opus | Haiku | tag |
|---|---|---|---|
| shopify001 | FAIL — benchmark artifact (§6) | FAIL — wrong name | **different** |
| shopify002 | **PASS** | FAIL — wrong name | **Haiku-only gap** |
| holistic | **PASS** | **PASS** | both pass |
| recharge001 | FAIL — wrong name | FAIL — content (`amount`) | **different** |
| recharge002 | FAIL — wrong name | FAIL — content (`active_months`) | **different** |

Three of four shared failures are *different-mechanism*. On both Recharge tasks the
inversion is total: **Opus name-fails; Haiku names correctly and fails on a value.**

---

## 3. Two distinct failure mechanisms

These are **not** the same defect and do not respond to the same lever. Keeping them
apart is the main analytical result of the baseline.

### (a) Wrong table name = never retrieved the spec

The agent invents a target name from its own prose description of what it is building,
and never (or only partially) opens the file that declares the real name.

| cell | touches of the declaring YAML | first write | invented name |
|---|---|---|---|
| opus/recharge001 | **ZERO** | T6 | `recharge__charge_details` |
| opus/recharge002 | **ZERO** | T7 | `recharge__customer_daily_transactions` |
| haiku/shopify001 | T4, truncated `limit:100` | T9, T10 | `shopify__product_performance`, `shopify__daily_shop_performance` |
| haiku/shopify002 | **ZERO** in 45 calls | T16 | `shopify__discount_codes_comprehensive` |

The name traces to the agent's own words, verbatim:

- opus/recharge001 **T6**: *"I'm designing a unified **charge details** table…"*
- opus/recharge002 **T7**: *"…join **transaction** aggregates from billing history…"*
- haiku/shopify002 **T15**: *"Let me create a **comprehensive** model that combines…"*
- haiku/shopify001 **T8**: *"1. **Product Performance Table** (`shopify__product_performance`)…"*

Two aggravating details. `models/recharge.yml` is **41 lines** — Opus never opened it
on either recharge task, so "file too large" does not explain those two. And
opus/recharge001 escalated at **T9** by *authoring its own schema entry*
(`- name: recharge__charge_details`, with description and tests) rather than finding the
one already there — which is also why the name-gate reads declarations from the pristine
fixture and not the run dir (§4).

The truncation case has a clean control on the identical 1041-line file:
haiku/shopify001 read `limit:100` (targets at lines 472 and 915, neither in range);
opus/shopify001 ran `T5 grep "shopify__products|shopify__daily_shop"` then
`T6 read offset:472` and `T7 read offset:915`, and got both names right.

**This is model-symmetric: Opus 2/5, Haiku 2/5.** It is *not* a small-model deficit. If
anything the polarity runs the other way on this set — Opus is worse at consulting the
schema YAML and better at the SQL. The team reports the same "guesses model structure"
behaviour on the old harness and on Qwen, so it is **not a Pi-harness artifact** either.

**Contrast with the earlier recharge001 column-level finding, which is the opposite
shape.** There, 5/5 wrong-column runs *demonstrably read* the YAML; the correct token
`'charge line'` appeared in the agent's own pre-write reasoning and was then snake_cased
to `'charge_line'`, and `'line_item'` was traceable to a sibling fixture file. That is
*consulted-but-misused*. Wrong-naming is *never-consulted*. The hypothesis refuted at
column level is confirmed at table-name level. Collapsing them into one
"prefers-own-convention" story would overclaim and point at the wrong fix.

### (b) Content / invariant errors on a correctly-named table

- **haiku/recharge001** — `[6]amount`: raw discount values `-8.0` / `-15.0` where derived
  `0.96` / `1.49` expected (`round(value/100 * total_line_items_price, 2)`).
- **haiku/recharge002** — `[37]active_months_to_date`: pred spans **-2.00 … 2.03**, gold
  **0.03 … 2.03**. Same step, same max. The SQL's datediff argument order and `+1` are
  correct; the guard covers only a NULL first-charge date, not `date_day <
  first_charge_date`, so spine days before the first charge go negative. Invariant:
  `active_months_to_date >= 0`.

Both are checkable on the produced DuckDB with no gold access.

---

## 4. The name-gate policy

**What it checks.** A model the agent *creates* must carry a name the project's schema
YAML actually declares. "What should have been built" is derived purely from the project:
models **declared** in `models/**/*.yml` that ship with **no `.sql` file**. That set is
what a developer reading the project sees as unfinished work.

**Gold-free.** The checker never opens the gold DuckDB or the eval spec. On the five
tasks the declared-but-unbuilt set is 1, 1, 5, 6 and 2 members respectively and always
contains the scored target — but the checker does not know which member is graded, so
discovery stays the agent's job and remains measurable.

**Declarations come from the pristine fixture, not the run dir** — because agents edit
schema YAML (opus/recharge001, T9). Reading the run dir would let an agent legalise its
invented name.

**Violation condition** (narrow, to avoid false positives):

```
offenders        = agent-created models that materialized and are NOT declared
built_candidates = declared-but-unshipped models that DID materialize
violation  <=>  offenders  AND  NOT built_candidates
```

Requiring `not built_candidates` means an agent that built the right model *and* an extra
undeclared helper is not steered — it engaged with the spec.

**Two-way gate: 15/15 PASS** (re-run at write time):

```
1. GOLD (fixture models/ + target .sql + gold DuckDB, all 5 tasks)   5/5 pass
2. KNOWN WRONG-NAME FAILURES                                          4/4 flagged
3. NEGATIVE CONTROLS (duckdb_match == 1)                              3/3 pass
4. CONTENT-FAILS (right name, wrong values)                           3/3 pass
```

Bucket 4 matters: the gate is silent on value errors, so name-gate and the value
checkers are cleanly non-overlapping.

**Seed sweep** (recharge001 + Opus, N=10 per arm, fresh fixture per run):

- **Base rate, policy-off: wrong-name 4/10.** Invented names `recharge__charges` x2,
  `recharge__charge_items` x2. Consistent with 20 archived Opus recharge001 runs:
  `off-v2` 3/10, `opus-r001` 2/5, `opus-r001b` 2/5 = **7/20 (35%)**.
- **Gate fired 3/10; converted the name 3/3.** Each fired run shows
  violation -> one retry -> gate passes -> correct table with 8 rows, and the
  wrongly-named `.sql` removed from `models/`.
- **Wrong-name surviving to scoring: 4/10 -> 0/10.**
- Official pass stayed **0/10**, because all ten runs ended blocked on `[6]amount`.

---

## 5. The stacking result

recharge001 is a **multi-defect task**. Each policy clears its own defect; neither alone
clears the task; stacked, they do.

`--dfc-policy` accepts a comma-separated list. The composite checker evaluates policies
**left to right and returns the first violation**, tagged with the firing policy; the
retry dispatcher routes to that policy's own message. The existing loop already re-checks
after every retry, so sequencing needed no loop change. Order matters:
`namegate,recharge001` is correct because the discount checker returns `error` ("target
table not materialized") on a run that built the model under an invented name. An `error`
from one policy does not mask another; the composite reports `error` only if all error.

### Matched three-arm table (recharge001 + Opus, N=10 each)

| arm | OFFICIAL pass | wrong-name surviving | `amount` surviving | policy fired |
|---|---|---|---|---|
| policy-off | **0/10** | 4/10 | 6/10 | 0/10 |
| namegate only | **0/10** | **0/10** | 10/10 | 3/10 |
| **stacked (namegate + recharge001)** | **10/10** | **0/10** | **0/10** | 10/10 |

Firing breakdown for the stacked arm: **both policies 4/10**, discount only 6/10,
namegate only 0, neither 0. Every pass was preceded by at least one firing — **no run
passed without steering**, so there is no attribution ambiguity.

### The sequential handoff (the 4 runs where both fired)

All four follow an identical three-round trace and clear both defects:

```
st-r3   r0: namegate! (recharge__charge_line_items)  -> r1: recharge001! -> r2: BOTH PASS -> 8 rows
st-r4   r0: namegate! (recharge__charges)            -> r1: recharge001! -> r2: BOTH PASS -> 8 rows
st-r7   r0: namegate! (recharge__charge_line_item)   -> r1: recharge001! -> r2: BOTH PASS -> 8 rows
st-r10  r0: namegate! (recharge__charge_line_item)   -> r1: recharge001! -> r2: BOTH PASS -> 8 rows
```

Name violation -> steer -> name fixed but amount now violates -> steer -> both clear,
all inside **one Pi session**. Note `recharge__charge_line_item` (singular) in r7/r10 —
near-misses of the declared `…_history`, still caught.

All 10 stacked runs ended with `UNMATCHED = []` — every scored column matched, not just
`amount`. The "third defect" seen in earlier Haiku discount work (amount matched 7/8 but
only 3/8 passed, because other columns then failed) **did not recur** in this Opus arm.
No run hit the retry cap (max 2 of 3 used).

### Scaffold arm, for completeness

A separate 6-run test (current vs schema-first scaffold, no DFC) is **inconclusive**: all
6 runs — both scaffolds — read the YAML before writing and built the correct name, so the
control never reproduced the failure it was meant to control for. Scores were
current 1/3, schema-first 2/3, with the one differing pair flipping on a *content*
invariant, not on naming. It does establish that the wrong-name failure is **stochastic
rather than deterministic** — same task, same model, same scaffold, different day.

---

## 6. Honest limits

**n=10, single batch.** The 10/10 stacked figure is a point estimate from one batch, not
a converged rate; so are the 0/10 arms. The wrong-name base rate is the best-supported
number here (4/10 in the sweep, 7/20 archived, ~35-40%) and even that is two batches.
Nothing in this report distinguishes 3/10 from 4/10.

**Opus, not Haiku.** The stacking result is Opus-only on one task. Haiku's recharge001
failure mode differs (content, not naming), and earlier Haiku discount work showed
additional columns failing after `amount` was fixed — exactly the third-defect pattern
that did not appear here. **Do not assume 10/10 transfers to Haiku or to other tasks.**

**Steering vs encoding the answer key — the open question.** Every policy here targets a
defect that was first identified by reading traces of failures on that same task. The
name-gate is the most general (task-agnostic, derives everything from project structure,
never reads gold) and the discount/idtype/enum checkers are the most specific
(recharge001 invariants). None has been tested on a task whose failure modes were not
already known. **Generalization to unseen tasks is untested**, and until it is, the
honest framing is "policies clear defects we characterized", not "DFC solves the task
class". The obvious next test is running the name-gate unchanged against a task family
nobody has trace-analyzed.

**Retry budget.** Stacked runs used up to 2 retries of a cap of 3. A task needing more
defects cleared than the cap allows would truncate silently; the cap should scale with
stack depth.

**Benchmark-data findings to report upstream** (neither is a model failure):

- **shopify001 — stale, time-dependent gold.** The fixture ships
  `models/utils/shopify__calendar.sql` with `end_date="current_date"`; it is
  pre-existing fixture code the agent did **not** modify (`diff` vs pristine:
  identical). Gold's `shopify__daily_shop` spans `2019-01-01..2024-09-07` (2077 rows) —
  frozen when gold was generated. The produced table spans `2019-01-01..2026-08-20`
  (2789 rows). Surplus **2789-2077 = 712 rows**, and days from 2024-09-08 to 2026-08-20
  inclusive = **712**. Exact match. Because unequal-length vectors never match, this one
  issue fails all 14 scored columns of that table at once. **The task is effectively
  unpassable as shipped and degrades daily** — any published shopify001 number is a
  function of when the run happened.
- **chinook001 — unscorable.** Gold DuckDB contains only raw source tables; none of the
  three scored targets (`dim_customer`, `fct_invoice`, `obt_invoice`) were ever built.
  Statically auto-0. (Also confirmed auto-0 for fixture reasons: `xero_new001`,
  `xero_new002`, `social_media001`, `gitcoin001`.)

**Qwen arm not run.** Blocked at smoke test on a harness-level issue, not a model issue —
see `docs/qwen_arm_blocker.md`.

---

## 7. Artifacts

| what | where |
|---|---|
| baseline 10 cells | `runs/pi/ecom-base-{opus,haiku}-<task>.stdout.json` + `/<task>/` |
| seed sweep, policy-off / namegate | `runs/pi/ns-off-r1..10`, `runs/pi/ns-on-r1..10` |
| stacked sweep | `runs/pi/st-r1..10` |
| scaffold arm | `runs/pi/armA-{cur,sf}-*` |
| archived Opus recharge001 | `runs/pi/off-v2-r1..10`, `opus-r001-r1..5`, `opus-r001b-r1..5` |
| checkers | `Spider2/methods/spider-agent-dbt/spider_agent/agent/dfc_check{,_idtype,_enum,_namegate}.py` (mirrored in `dfc/spider_agent/agent/`) |
| runner + stacking | `pi_runner/run_task.py` (`_load_dfc_stack`) |
| drivers | `pi_runner/run_matrix.sh`, `run_arms.sh`, `run_namegate_sweep.sh`, `run_stack_sweep.sh` |
| deeper baseline analysis | `docs/ecommerce_baseline_analysis.md` |
| Qwen blocker | `docs/qwen_arm_blocker.md` |

Trajectories are compacted (streaming token deltas stripped; tool calls, thoughts and
terminal signals preserved — verified to reproduce all analysis numbers identically).
