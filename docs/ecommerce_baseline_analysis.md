# E-commerce baseline: 10-cell trace-level root-cause analysis

**Scope.** 5 e-commerce tasks x 2 models (Opus 4.8, Haiku 4.5), plain baseline, no DFC
policy, no step cap (Pi has none; wall-clock `--timeout 2400` only). Fixture copied
pristine per run, never pre-patched. Scored by `score_run.py` / `duckdb_match` against
`evaluation_suite/gold/`.

**Method.** Every claim below is traced to a run artifact: a trajectory event (cited as
`Tn` = turn number, `cN` = tool-call index), a fixture file, or a DuckDB query. Analysis
was performed on the compacted trajectories under
`runs/pi/ecom-base-<model>-<task>/_pi_meta/<task>/trajectory.jsonl` (streaming
`message_update` deltas stripped; tool calls, thoughts, and terminal signals fully
preserved and verified equivalent).

> **n=1 CAVEAT, APPLIES THROUGHOUT.** Every cell is a *single draw*. The failure
> *mechanisms* documented here are well-evidenced per-run — each is pinned to specific
> tool calls and file contents. The *frequencies* are not established. Prior recharge001
> work showed large run-to-run variance (Haiku 3/8 with policy on; Opus name-failed here
> but built the correct table in all 10 `off-v2` runs). Treat every rate in this document
> as a hypothesis sized for a multi-seed confirmation, not as a measurement.

---

## 1. Results

### Per-model summary

| model | n | dbt-artifact | target built | **OFFICIAL pass** | settled/cap | avg turns | avg edits | avg dbt runs | total $ |
|---|---|---|---|---|---|---|---|---|---|
| Opus 4.8 | 5 | 5/5 | 3/5 | **2/5** | 5/5 settled | 19.0 | 2.6 | 9.2 | $2.96 |
| Haiku 4.5 | 5 | 5/5 | 3/5 | **1/5** | 5/5 settled | 43.2 | 3.6 | 14.4 | $1.47 |

Three deliberately separated signals:

- **dbt-artifact** — the produced project's `target/run_results.json` shows a dbt run with
  no errored node. This is the closest analog to a "did dbt run" metric.
- **target built** — every scored `condition_tab` exists and is non-empty in the produced
  DuckDB (`score_report.all_targets_materialized_nonempty`).
- **OFFICIAL** — `score_run.py` / `duckdb_match` vs gold. The real number.

**The headline: dbt-artifact success is 10/10 (100%); official pass is 3/10 (30%).** Every
run produced a clean `dbt run`. A metric that stops at the dbt artifact reports total
success on a task set where 70% of the work is wrong.

### Per-cell

| model | task | dbt_ok | target built | OFFICIAL | settled | turns | edits | dbt runs | wall | $ |
|---|---|---|---|---|---|---|---|---|---|---|
| opus | shopify001 | YES | YES | FAIL | settled | 15 | 2 | 4 | 103s | 0.455 |
| opus | shopify002 | YES | YES | **PASS** | settled | 13 | 1 | 4 | 126s | 0.486 |
| opus | shopify_holistic_reporting001 | YES | YES | **PASS** | settled | 29 | 3 | 19 | 205s | 0.963 |
| opus | recharge001 | YES | no | FAIL | settled | 20 | 5 | 12 | 147s | 0.576 |
| opus | recharge002 | YES | no | FAIL | settled | 18 | 2 | 7 | 136s | 0.479 |
| haiku | shopify001 | YES | no | FAIL | settled | 39 | 4 | 10 | 193s | 0.234 |
| haiku | shopify002 | YES | no | FAIL | settled | 46 | 6 | 17 | 255s | 0.308 |
| haiku | shopify_holistic_reporting001 | YES | YES | **PASS** | settled | 39 | 2 | 16 | 242s | 0.325 |
| haiku | recharge001 | YES | YES | FAIL | settled | 47 | 4 | 14 | 221s | 0.290 |
| haiku | recharge002 | YES | YES | FAIL | settled | 45 | 2 | 15 | 248s | 0.316 |

Total spend **$4.43**. All 10 settled naturally (`agent_settled=1`, `agent_end=1`,
`timed_out=False` in every trajectory); none was truncated. Haiku uses ~2.3x the turns and
~1.5x the dbt invocations of Opus for ~half the cost.

### Foundational fact: the target name is discoverable in every task

Each task declares its exact target table name in a fixture `models/*.yml`:

| task | declaring file | file lines | target declared at line |
|---|---|---|---|
| recharge001 | `models/recharge.yml` | **41** | 4 |
| recharge002 | `models/recharge.yml` | 402 | 128 |
| shopify001 | `models/shopify.yml` | 1041 | **472** and **915** |
| shopify002 | `models/shopify.yml` | 963 | **847** |
| shopify_holistic_reporting001 | `models/shopify_holistic_reporting.yml` | 447 | 138 |

No cell failed for lack of available information.

---

## 2. Bucket 1 — WHY PASS (3 cells)

**Generalized pattern: all three read the declaring yml *before* the first write.**
They did not hit the wrong-name trap and recover — they never entered it.

| cell | declaring-yml read | first write | gap |
|---|---|---|---|
| opus/shopify002 | **T6** `read {"path":"models/shopify.yml","offset":847,"limit":120}` | **T10** `write models/shopify__discounts.sql` | 4 turns |
| opus/holistic | **T3** `read {"path":"models/shopify_holistic_reporting.yml"}` (whole file) | **T7** `write …__daily_customer_metrics.sql` | 4 turns |
| haiku/holistic | **T9** `read {"path":"models/shopify_holistic_reporting.yml"}` (whole file) | **T16** `write …__daily_customer_metrics.sql` | 7 turns |

### Two routes to the declaration, both effective

**Grep-then-seek**, on the large file. opus/shopify002, `models/shopify.yml` = 963 lines:

```
T5  c9   [grep] {"pattern": "discount", "glob": "models/**/*.yml"}
T6  c10  [read] {"path": "models/shopify.yml", "offset": 847, "limit": 120}
T10 c16  [write] {"path": "models/shopify__discounts.sql", ...}
```

Line 847 is exactly where `- name: shopify__discounts` is declared. It searched rather
than reading blindly.

**Read-whole**, on the medium file. Both holistic runs read the 447-line yml in full
(opus T3, haiku T9), each following immediately with the intermediate yml
(`models/intermediate/int_shopify_holistic_reporting.yml`, opus T4 / haiku T10).

Both then stated the target verbatim before writing:

> opus/holistic: *"I need to build the `shopify_holistic_reporting__daily_customer_metrics`
> model that merges daily Shopify customer orders with Klaviyo user metrics."*

> haiku/holistic: *"The YAML file already references
> `shopify_holistic_reporting__daily_customer_metrics` which seems to be the model I need
> to create/update."*

### The revision habit is ABSENT here

On recharge001, what separated passes was a re-read-then-rewrite move (e.g. `on-v2-r1`
wrote a wrongly-named draft, re-read `models/recharge.yml`, deleted the draft, rewrote).
**No such cycle appears in any of these three passes.** Writes land at T10/T7/T16 with no
delete-and-rewrite. On these tasks the decisive move is *locating the spec before writing*,
not recovering after. (n=1: three draws.)

### One wrinkle: a passing run still edited the spec to suit itself

haiku/holistic passed, but at **T21** it edited the declaring yml:

```
T21 c21 [edit] {"path": "models/shopify_holistic_reporting.yml", "edits": [{
  "oldText": "  - name: shopify_holistic_reporting__customer_enhanced\n    description: >",
  "newText": "  - name: shopify_holistic_reporting__daily_customer_metrics\n    description: > ..."}]}
```

It renamed a *different* model's schema entry to its own model's name — a name already
declared at line 138, so the yml now carries it twice. This is harmless for scoring
(`duckdb_match` reads tables from the DuckDB, not the yml) and happened *after* the
correct write at T16. It is noted because it shows the self-convention instinct operating
even inside a pass — landing on an unscored surface rather than the scored one.

---

## 3. Bucket 2 — WHY WRONG-NAME FAIL (4 cells) — the dominant mechanism

**Generalized mechanism: the target name is generated from the agent's own prose
description of what it is building, and is never retrieved from the spec.** In 3 of 4
cells the declaring file was never opened at all; in the 4th it was read but truncated
above the declaration.

| cell | touches of the declaring yml | first write | invented name | gold expected |
|---|---|---|---|---|
| opus/recharge001 | **ZERO** | T6 | `recharge__charge_details` | `recharge__charge_line_item_history` |
| opus/recharge002 | **ZERO** | T7 | `recharge__customer_daily_transactions` | `recharge__customer_daily_rollup` |
| haiku/shopify001 | T4, truncated `limit:100` | T9, T10 | `shopify__product_performance`, `shopify__daily_shop_performance` | `shopify__products`, `shopify__daily_shop` |
| haiku/shopify002 | **ZERO** | T16 | `shopify__discount_codes_comprehensive` | `shopify__discounts` |

### 3.1 Never opened the declaring file (3 of 4)

A targeted search for `models/recharge.yml` across both Opus recharge trajectories returns
**nothing**:

```
### opus/recharge001 : touches of models/recharge.yml   -> (none)
### opus/recharge002 : touches of models/recharge.yml   -> (none)
```

The only `*recharge.yml` touches in those runs are to a **different file** —
`dbt_packages/recharge_source/models/src_recharge.yml` — and both occur *after* the write
(opus/recharge001 T14; opus/recharge002 T12), while chasing the unrelated `order_data`
compile blocker.

haiku/shopify002 never touches `models/shopify.yml` anywhere in its 45 tool calls. Its
calls c7–c15 are DuckDB introspection; it inferred schema from data rather than from the
project's declared spec.

**The sharpest single fact: `models/recharge.yml` is 41 lines** — smaller than several
files Opus did read in the same runs. There is no "file too large" explanation for those
two cells.

### 3.2 Read but truncated above the declaration (1 of 4)

haiku/shopify001:

```
T4 c6 [read] {"path": "models/shopify.yml", "limit": 100}
```

`models/shopify.yml` is **1041 lines**; the targets are declared at lines **472** and
**915**. The first 100 lines contain `shopify__customer_cohorts`,
`shopify__customer_email_cohorts` and part of `shopify__orders` — **neither target**. The
second touch (T17, `grep -A 20 "name: shopify__order_lines"`) comes *after* both writes.

**The controlled comparison is opus/shopify001 on the identical file** (that cell got both
names right, and failed for an unrelated reason — see §4):

```
T5 c12 [grep] {"pattern": "shopify__products|shopify__daily_shop", "path": "."}
T6 c13 [read] {"path": "models/shopify.yml", "offset": 472, "limit": 120}
T7 c14 [read] {"limit": 180, "offset": 915, "path": "models/shopify.yml"}
T8 c15 [write] {"path": "models/shopify__products.sql", ...}
T11 c20 [write] ... (shopify__daily_shop)
```

Same file, same task, same information available. Targeted search vs truncated prefix read.

### 3.3 The name traces to the agent's own words

In every case the invented name is a snake_case rendering of the phrase the agent used to
describe its own design, one or two turns earlier:

- **opus/recharge001 T6**: *"I'm designing a unified **charge details** table that
  consolidates line items, discounts, taxes, shipping, and refunds…"* -> writes
  `recharge__charge_details.sql` in the same turn.
- **opus/recharge002 T7**: *"I'm building a model using the daily rollup spine to join
  **transaction** aggregates from billing history…"* -> writes
  `recharge__customer_daily_transactions.sql`.
- **haiku/shopify001 T8**: *"1. **Product Performance Table** (`shopify__product_performance`)
  … 2. **Daily Shop Performance Table** (`shopify__daily_shop_performance`)"* — the names
  appear fully formed in the plan, derived from the task instruction's prose ("product
  data…", "daily shop performance").
- **haiku/shopify002 T15**: *"Let me create a **comprehensive** model that combines…"* ->
  writes `shopify__discount_codes_comprehensive.sql`.

**Opus never deliberated about naming at all.** Filtering its pre-write thinking across
both recharge runs for name-related terms (`name`, `call it`, `charge_details`,
`daily_transactions`) returns nothing. There is no moment of choosing a name over an
alternative; a name is simply emitted as part of writing the file.

### 3.4 Escalation: authoring a spec for the invented name

opus/recharge001 did not merely skip the schema file — at **T9** it wrote its own:

```
T9 c12 [edit] {"edits": [{"newText":
  "version: 2\n\nmodels:\n  - name: recharge__charge_details\n    description: >\n
   Combines all charge-related components (line items, discounts, taxes,\n
   shipping, and refunds) into a single unified table. Each record is\n
   uniquely identified via `charge_detail_id` ...
```

It produced a schema entry for a model the project never asked for, in a project that
already declared the real target 41 lines away in `models/recharge.yml`.

---

## 4. Benchmark artifact — NOT a model error (1 cell)

### opus/shopify001 — `shopify__daily_shop`: stale, time-dependent gold

This cell is **removed from the content-failure bucket.** It is not an agent reasoning
error.

**Mechanism.** The fixture ships a calendar spine model,
`models/utils/shopify__calendar.sql`, whose end date is **`current_date`**:

```jinja
{% set start_date = var('shopify__calendar_start_date', '2019-01-01') %}
{{ dbt_utils.date_spine(
    datepart="day",
    start_date="cast('" ~ start_date ~ "' as date)",
    end_date="current_date"
```

This file is **pre-existing fixture code**, present in
`Spider2/spider2-dbt/examples/shopify001/models/utils/shopify__calendar.sql`, and the agent
**did not modify it** (`diff` against pristine: identical). The agent's
`shopify__daily_shop.sql` simply references it:

```sql
with spine as (
    select cast(date_day as date) as date_day
    from {{ ref('shopify__calendar') }}
),
```

**Arithmetic.** Gold's `shopify__daily_shop` spans `2019-01-01 .. 2024-09-07` (2077 rows) —
i.e. it was frozen on the day gold was generated. The produced table spans
`2019-01-01 .. 2026-08-20` (2789 rows) — i.e. to *today*. The surplus:

```
2789 - 2077 = 712 rows
days from 2024-09-08 to 2026-08-20 inclusive = 712
```

Exact match. The extra rows are precisely the calendar days elapsed since gold was built.

**Consequence.** `compare_pandas_table` never matches vectors of unequal length, so a
single row-count mismatch fails **every** scored column of the table at once. That is why
this cell reports 14 unmatched columns for what is one issue — and why
`shopify__products` in the same run matched cleanly (`cols_UNMATCHED: []`).

**This task is effectively unpassable as-shipped.** Passing requires an agent to
*deliberately override* the spine model the fixture provides and tells it to use. That is
not a data-flow reasoning failure and DFC neither can nor should "fix" it.

> **REPORT UPSTREAM.** shopify001 belongs to the same class as **chinook001**, already
> identified as auto-0 (its gold DuckDB contains only raw source tables — `album`,
> `artist`, `customer`, … `track` — and none of its three scored targets `dim_customer`,
> `fct_invoice`, `obt_invoice` were ever built). Both are fixture/gold defects rather than
> model failures. shopify001 is the more insidious of the two because it *degrades over
> time*: it would have passed near the gold-generation date and fails by more rows every
> day. Any published shopify001 number is a function of when the run happened.

---

## 5. Bucket 3 — WHY CONTENT FAIL (2 cells)

Both cells got the table name right (declaring yml read before the write) and failed on
values. **Both are invariant-shaped and gold-free checkable — exactly DFC's target class.**

### 5.1 haiku/recharge001 — discount amount (already-targeted invariant)

Read `models/recharge.yml` at **T5**, re-read at **T17**, first write **T18**. Name
correct, 8/8 rows, only `[6]amount` unmatched. The produced discount rows carry the raw
discount value instead of the derived amount:

```
(400000001, 'code01', -8.0)      expected  0.96  = round(8.0/100  * 11.95, 2)
(400000002, 'code02', -15.0)     expected  1.49  = round(15.0/100 * 9.95, 2)
```

Running the three existing checkers against this cell's DuckDB:

```
discount  violation  2/2 discount rows violate the amount invariant
idtype    pass       all 3 id columns satisfy the type invariant
enum      pass       all 8 rows carry a line_item_type inside the declared domain
```

The existing policy fires correctly; the two newer checkers correctly stay silent. This is
an independent reproduction of the defect the recharge001 DFC policy was built for.

### 5.2 haiku/recharge002 — `active_months_to_date` unclamped window (NEW invariant)

Read `models/recharge.yml` at **T6**, first write **T16**. Name correct, row count exact
(122 = gold 122), `customer_id` matched; only `[37]active_months_to_date` unmatched.

```
pred distinct: -2.00, -1.97, -1.93, -1.90, ...   max 2.03
gold distinct:  0.03,  0.07,  0.10,  0.13, ...   max 2.03
```

Same 1/30-day step, same maximum — only the pre-first-charge region diverges. The SQL:

```sql
case when customer_first_charge.first_charge_date is null then 0
    else round(cast(
        ({{ dbt.datediff('customer_first_charge.first_charge_date',
                         'daily_charges.date_day', 'day') }} + 1) / 30.0
        as {{ dbt.type_numeric() }}), 2)
end as active_months_to_date,
```

The datediff argument order and the `+ 1` are **correct**. The defect is that the guard
covers only a NULL `first_charge_date`, not `date_day < first_charge_date`. Spine days
before the customer's first charge produce a negative datediff and therefore negative
"months active".

**Proposed invariant: `active_months_to_date >= 0`.** A customer cannot have a negative
number of months active. Checkable on the produced DuckDB with no gold access, in the same
shape as the existing `dfc_check.py` / `dfc_check_idtype.py` / `dfc_check_enum.py`.

---

## 6. Bucket 4 — CROSS-MODEL

| task | Opus | Haiku | tag |
|---|---|---|---|
| shopify001 | FAIL — benchmark artifact (stale gold spine) | FAIL — wrong name (truncated read) | **different-failure** |
| shopify002 | **PASS** | FAIL — wrong name (never read yml) | **Haiku-only gap** |
| shopify_holistic_reporting001 | **PASS** | **PASS** | both pass |
| recharge001 | FAIL — wrong name (never read yml) | FAIL — content (discount amount) | **different-failure** |
| recharge002 | FAIL — wrong name (never read yml) | FAIL — content (negative months) | **different-failure** |

**Wrong-naming: Opus 2/5, Haiku 2/5 — exactly symmetric.**

Three of the four shared failures are *different-mechanism*. Only one is a clean
**Haiku-only gap** (shopify002, where Opus passed outright).

The inversion on both Recharge tasks is total and is the most striking cross-model result
in the set: **Opus never opened `models/recharge.yml` on either task and got both names
wrong; Haiku opened it on both (recharge001 T5/T17/T43, recharge002 T6) and got both names
right, failing instead on a value.**

**Implication: wrong-table-naming is a DFC-general error, not a small-model gap.** If
anything the polarity runs the other way on this set — Opus is worse at consulting the
schema yml and better at the SQL; Haiku is the reverse, consistent with Haiku's ~2.3x turn
count buying it more file reads. (n=1: five task-pairs, one draw each. The 2/5-vs-2/5
symmetry is the most load-bearing and least-confirmed number in this document.)

---

## 7. Synthesis: wrong-table-name is NOT the recharge001 column finding

The tempting generalization is that both failures are one pattern — "the agent prefers its
own convention over the spec." **The trajectories do not support that, and asserting it
would overclaim and mis-target the fix.**

**At the column level (recharge001), the failure is misuse of information that was read.**
Across those 5 wrong-column runs, 5/5 demonstrably read `models/recharge.yml` before
writing. In `haiku-on-r1` the correct token `'charge line'` appears verbatim in the agent's
own pre-write reasoning and is then snake_cased to `'charge_line'` in the emitted SQL. In
`haiku-on-r7` the substituted token `'line_item'` is traceable to a sibling fixture file
(`recharge__line_item_enhanced.sql:135`, `'line_item' as record_type`) that the agent read
at call 7. That is **consulted-but-misused**: the spec was in context and was overridden by
a competing convention.

**At the table-name level, the failure is that the information was never retrieved.** 3 of
4 wrong-name cells never opened the declaring file; the 4th read 100 lines of 1041 and
stopped above the declaration. There is no moment of preferring one convention over
another, because only one candidate was ever present. Opus's pre-write thinking contains no
naming deliberation whatsoever.

**These are inverse findings.** The "didn't read the file" hypothesis was *refuted* at the
column level and is *confirmed* at the table-name level. They share a surface — both end
with a self-generated convention in the emitted artifact — but not a root cause, and the
distinction is exactly what determines the right lever:

- A "you didn't read the source" nudge would have fired on **zero** of the 5 column-level
  instances; the information was already in context every time.
- A value-correction policy cannot address the naming failures; there is no wrong *value*
  to correct, and the checker's target table does not exist under the name the scorer looks
  for, so a post-materialization checker never even runs (it returns
  `status: "error", "target table not materialized"`).

The one honest bridge is narrow: haiku/holistic's T21 yml rename shows the self-convention
instinct operating inside a *passing* run. So the instinct is real and observable across
both levels — it is simply not the *cause* of the naming failures, which are failures of
retrieval, not of preference.

---

## 8. DFC implications: two distinct levers

### (a) Value-correction checkers — already built, confirmed firing

Post-materialization invariants on a correctly-named target. Existing:
`dfc_check.py` (discount amount), `dfc_check_idtype.py` (id columns must keep the source's
integer type), `dfc_check_enum.py` (`line_item_type` within the `recharge.yml` domain).
This baseline independently reproduced the discount defect (§5.1) and surfaced one new
member of the class: **`active_months_to_date >= 0`** (§5.2).

Scope limit, stated plainly: these fire only when the target table exists under its spec
name. In this baseline they were applicable to **2 of 7 failures**.

### (b) Pre-write name gate — NEW, and distinct from value correction

A check that runs *before or at* model creation: **"does this model name appear as a
`- name:` entry in a project schema yml?"** If not, surface the declared names near it.

- Cheap: a grep over `models/**/*.yml`, no gold access, no DuckDB.
- Would have caught **4 of the 7 failures** in this baseline (all of Bucket 2) — the
  single largest failure class here.
- Structurally different from every checker built so far: it is *pre-materialization* and
  *retrieval-oriented*, not post-materialization and value-oriented. The existing DFC loop
  cannot express it, because by the time the loop runs, the wrongly-named table has already
  been built and the checker errors out rather than steering.

The two levers are complementary and non-overlapping: (a) fixes what was built wrong,
(b) fixes what was built under the wrong name. Together they address 6 of the 7 failures.
The 7th — opus/shopify001 — is a benchmark artifact (§4) that neither lever should touch.

---

## 9. Artifacts

- Run records: `runs/pi/ecom-base-{opus,haiku}-<task>.stdout.json` (10)
- Trajectories: `runs/pi/ecom-base-{opus,haiku}-<task>/_pi_meta/<task>/trajectory.jsonl` (10, compacted)
- Produced projects + DuckDBs: `runs/pi/ecom-base-{opus,haiku}-<task>/<task>/`
- Matrix summary: `runs/pi/ecom-base-matrix.json`
- Driver: `pi_runner/run_matrix.sh` (resumable; skips cells whose stdout.json already scores)
- Checkers: `Spider2/methods/spider-agent-dbt/spider_agent/agent/dfc_check{,_idtype,_enum}.py`
  (mirrored in `dfc/spider_agent/agent/`)
- Related: `docs/qwen_arm_blocker.md` (third model arm, shelved at smoke test)
