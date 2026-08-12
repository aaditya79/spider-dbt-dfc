# E-commerce Spider 2.0-DBT Tasks: Ground-Truth Analysis

Aaditya Pai
Scope: shopify001, recharge001, recharge002.

This document explains how each of the three e-commerce tasks works, where grading
is hard, and where and why the models fail. Every claim traces to a file in this
repo or a run artifact. Paths are relative to the repo root
(`~/Desktop/DAPLab/spider/Spider2`). Where a fact cannot be verified from files, it
is marked as inferred.

## Data sources used

- Task instructions: `spider2-dbt/examples/spider2-dbt.jsonl`.
- Grading spec: `spider2-dbt/evaluation_suite/gold/spider2_eval.jsonl`.
- Start dbt projects and start DuckDBs: `spider2-dbt/examples/<task>/`.
- Gold DuckDBs: `spider2-dbt/evaluation_suite/gold/<task>/<db>.duckdb`.
- Primary result set: the `ecom-e3-mt8k` batch (max_steps=50, max_tokens=8192) under
  `methods/spider-agent-dbt/output/`. Earlier batches (`ecom-e1` at step-cap 30,
  `ecom-e2-s50` at step-cap 50) are referenced only where the progression matters.
- DFC section: the `claude-opus-4-8-dfc-off` and `claude-opus-4-8-dfc-on`
  recharge001 pair.
- Trajectories for the six primary runs are copied to `docs/trajectories/` and
  indexed at the end of this document.

All 60 run JSON files (20 runs x result.json + trajectory.json + audit_metadata.json)
parse cleanly. Numbers below were pulled by querying the DuckDBs directly with
duckdb 1.5.4.

## How grading works (applies to all three tasks)

The evaluator is `duckdb_match`. For each task the gold spec names one or more
target tables (`condition_tabs`), and for each table a fixed list of column indices
to check (`condition_cols`). `ignore_orders` is true for every table here, so rows
are compared as a set, not in order.

Two consequences matter for reading the results.

First, grading is 0/1 with no partial credit. A run scores 1 only if every checked
column of every target table matches. One wrong column fails the whole task.

Second, because rows are compared as a set with a fixed row list, a row-count
mismatch fails every checked column at once. If the predicted table has extra or
missing rows, the set of tuples cannot line up, so all checked columns report
UNMATCHED even when the per-row logic is correct on the overlapping rows. This shows
up directly in shopify001 below.

The scorer that produced our per-column diagnostics is
`methods/spider-agent-dbt/score_run.py`. It computes the authoritative 0/1 from the
official `duckdb_match` and also records which checked columns matched. Those
diagnostics are stored per run in `audit_metadata.json`.

One reporting note carried from the run logs: the agent's own `finished` flag is not
the score. Several runs report `finished=False` (hit the step cap) yet still built a
non-empty target, and one run reports `finished=True` yet scored 0. Score always
comes from the evaluator, never from the agent.

## Result summary (primary mt8k batch, plus the DFC pair)

| Run | Task | Score | Termination | Target built | What was wrong |
|---|---|---|---|---|---|
| opus-4.8 mt8k | shopify001 | 0 | clean-terminate @32 | both marts | daily_shop spine over-extends 6 days |
| opus-4.8 mt8k | recharge001 | 0 | clean-terminate @32 | yes (8 rows) | discount `amount` not percent-converted |
| opus-4.8 mt8k | recharge002 | 0 | hit-cap @50 | yes (122 rows) | `active_months_to_date` wrong units |
| haiku-4.5 mt8k | shopify001 | 0 | hit-cap @50 | no models written | wrote zero model files |
| haiku-4.5 mt8k | recharge001 | 0 | hit-cap @50 | no (compile error) | project did not compile |
| haiku-4.5 mt8k | recharge002 | 0 | hit-cap @50 | no (target absent) | never wrote the target mart |
| opus-4.8 dfc-off | recharge001 | 0 | clean-terminate @34 | yes (8 rows) | discount `amount` raw value |
| opus-4.8 dfc-on | recharge001 | **1** | clean-terminate @33 | yes (8 rows) | none (passes) |

Source: each run's `audit_metadata.json` under
`methods/spider-agent-dbt/output/<run>/<task>/`.

---

## shopify001

### What the task is

Instruction (`spider2-dbt/examples/spider2-dbt.jsonl`): "Create two tables: one that
pulls together product data like total sales, refunds, discounts, and taxes, and
another that tracks daily shop performance, including orders, abandoned checkouts,
and fulfillment statuses."

This is the Fivetran Shopify dbt project. The start DuckDB
(`spider2-dbt/examples/shopify001/shopify.duckdb`) has 116 tables. Raw Shopify data
lands as `shopify_*_data` tables (orders, order lines, transactions, products,
abandoned checkouts), each small in this fixture (3 to 5 rows). Those feed staging
models `stg_shopify__*`, then intermediate aggregates under
`models/intermediate/` (for example `int_shopify__daily_orders`,
`int_shopify__daily_abandoned_checkouts`), plus a date spine
`models/utils/shopify__calendar.sql`.

Both final marts are removed from the starter project. The agent must build them:
`shopify__products.sql` and `shopify__daily_shop.sql`. Confirmed by diffing the run's
`models/` against the pristine `examples/shopify001/models/`; in the Opus run both
files appear as agent-added.

Target grain and size, from the gold DuckDB:
- `shopify__products`: 3 rows, 28 columns. One row per product.
- `shopify__daily_shop`: 2077 rows, 59 columns. One row per shop per calendar day.

### Where grading is difficult

The gold spec (`spider2_eval.jsonl`) checks two tables.

`shopify__products` checks 13 of 28 columns: `product_id, handle, published_scope,
title, vendor, total_quantity_sold, subtotal_sold, quantity_sold_net_refunds,
subtotal_sold_net_refunds, avg_quantity_per_order_line, product_total_discount,
product_avg_discount_per_order_line, product_total_tax`. This mixes identity columns
with aggregates that require correct refund-netting and per-order-line averaging.

`shopify__daily_shop` checks 14 of 59 columns: `date_day, shop_id, name, domain,
currency, iana_timezone, count_orders, count_line_items, count_customers,
count_customer_emails, order_adjusted_total, refund_total_tax, total_discounts,
fixed_amount_discount_amount`.

The hard part of grading here is the row set of `shopify__daily_shop`. It is a
per-day table with 2077 rows. The date range is decided by the calendar spine. If the
spine is not bounded to the shop's activity window, the row set differs from gold and
every checked column fails on the set mismatch, regardless of the aggregate logic.

### Where and why the models fail

Opus (clean-terminate at step 32, both marts built). `shopify__products` is perfect:
all 3 rows present and all 13 checked columns match. That means the product
aggregation, refund netting, and per-order-line averages are correct.
`shopify__daily_shop` fails. The predicted table has 2083 rows against gold's 2077,
and all 14 checked columns report UNMATCHED.

The cause is the calendar spine.
`methods/spider-agent-dbt/output/claude-opus-4-8-ecom-e3-mt8k/shopify001/models/utils/shopify__calendar.sql`
builds a date spine with `end_date="current_date"`:

```sql
{{ dbt_utils.date_spine(datepart="day",
    start_date="cast('" ~ start_date ~ "' as date)",
    end_date="current_date") }}
```

`shopify__daily_shop.sql` then cross joins that calendar with the shop. Verified
against the DuckDBs: gold runs 2019-01-01 to 2024-09-07 (2077 days); the predicted
table runs 2019-01-01 to 2024-09-13 (2083 days). The start date matches. The tail
overshoots by 6 days. Gold clamps the spine to the last day with shop activity
(2024-09-07); the model ran the spine to the run date. Those 6 empty trailing days
make the row set unequal, which is why all 14 columns fail even though the per-day
aggregates on the overlapping days are computed the same way. This is a
clean-terminate run, so it is a capability failure, not a truncation or step-cap
artifact.

Note for context: the start DuckDB already contains a `shopify__calendar` table with
2083 rows, so the unclamped spine length is present in the fixture. The task is to
recognize that the shop mart must be bounded to the activity window; Opus did not.

Haiku (hit step cap at 50, no models written). The diff against the starter shows
the agent added no model files and changed none. Neither `shopify__products` nor
`shopify__daily_shop` exists in the built database. The evaluator reports both tables
missing. This is a structural failure: the agent spent all 50 steps exploring the
staging DAG and never wrote a target model. This is step-cap-bounded, but even the
earlier s30 and s50 batches show the same zero-materialization pattern, so the cap is
not the limiting factor.

### Failure mode

Opus: temporal-window. The one thing separating the run from a pass is that the daily
mart spine is not clamped to the activity window. Haiku: navigational/structural. It
never produced a target model.

---

## recharge001

### What the task is

Instruction: "Create a model to combine charge data, including line items, discounts,
taxes, shipping, and refunds, while ensuring each item is uniquely identified and
linked to its charge?"

This is the Fivetran Recharge dbt project. The start DuckDB
(`spider2-dbt/examples/recharge001/recharge.duckdb`) has 51 tables. Raw data is in
`*_data` tables: `charge_data`, `charge_line_item_data`, `charge_discount_data`,
`charge_tax_line_data`, `charge_shipping_line_data` (2 rows each in this fixture).
These feed `stg_recharge__*` staging models and a standardized model
`recharge__line_item_enhanced.sql`, plus `int_recharge__calendar_spine.sql`.

The target `recharge__charge_line_item_history.sql` is removed from the starter. The
agent must build it. Confirmed by the starter diff: it appears as agent-added in every
recharge001 run.

Target grain and size, from the gold DuckDB: `recharge__charge_line_item_history` has
8 rows, 9 columns. One row per charge line item, where line items are the union of
charge lines, discounts, shipping, taxes, and refunds across the 2 charges.

### Where grading is difficult

The gold spec checks 7 of 9 columns: `charge_id, charge_created_at, customer_id,
address_id, amount, title, line_item_type`. The two unchecked columns are
`charge_row_num` and `source_index` (indices 1 and 2). Because those are unchecked and
rows are compared as a set, the row-numbering scheme does not affect the score. What
must be right is the set of 7-tuples.

Six of the seven checked columns are pass-through identity or label columns that come
straight from the source join. The one column with real computation is `amount`. That
makes recharge001 an almost pure test of one financial transform: whether each line
item's amount is computed correctly, especially the discount rows.

### Where and why the models fail

Opus (clean-terminate at step 32, target built with 8 rows). Six of seven checked
columns match. Only `amount` is wrong. Verified against the DuckDBs, the discount rows
diverge:

- Gold: charge 400000001 discount `code01` amount 0.96; charge 400000002 discount
  `code02` amount 1.49.
- Opus produced: 8.00 and 15.00.

The produced model
(`.../claude-opus-4-8-ecom-e3-mt8k/recharge001/models/recharge__charge_line_item_history.sql`)
emits the raw discount value:

```sql
), discount_records as (
    select
        charge_id,
        index as source_index,
        discount_value as amount,
        code as title,
        'discount' as line_item_type
    from charge_discounts
),
```

The gold discounts are percentage discounts. 8 percent of a line-items total of about
12.00 is 0.96; 15 percent of about 9.93 is 1.49. The model used the raw percentage
number (8, 15) as a currency amount. The charge-line, shipping, tax, and refund
amounts are correct; only the percentage-to-amount conversion is missing. This is a
clean-terminate run, so it is a capability failure, not truncation.

Haiku (hit step cap at 50, target did not materialize). Unlike shopify001, Haiku here
did write the target model file
(`.../claude-haiku-4-5-ecom-e3-mt8k/recharge001/models/recharge__charge_line_item_history.sql`,
2869 bytes). The file is structurally complete. But the table does not exist in the
built database, and the evaluator reports it missing. The dbt log
(`.../recharge001/logs/dbt.log`) shows why: `dbt run` failed with a Compilation Error:

```
Compilation Error
  Model 'model.recharge_source.stg_recharge__order_tmp'
  (models/tmp/stg_recharge__order_tmp.sql) depends on a node named 'order_data'
  which was not found
```

This is a project-wide compile failure. A dbt compile error halts the whole graph,
so the target never compiles no matter how correct its own SQL is. The cause is a var
in `dbt_project.yml`. The starter ships with
`order: "{{ ref('order_data') }}"` active and the alternative
`recharge_order_identifier: "order_data"` commented out, and `order_data` is not a
model or seed that exists. Opus resolved this: its `dbt_project.yml` comments out the
broken `order:` line and switches to `recharge_order_identifier: "order_data"`, the
source-identifier path the starter's own comments describe. Haiku left the starter
line unchanged (the starter and Haiku both have three `order_data` references in
`dbt_project.yml`; Opus has two). So Haiku's whole project failed to compile and its
otherwise-complete target model was never built. This is step-cap-bounded in that
Haiku ran out of steps still debugging, but the blocking issue was a project
configuration fix it never made.

### Failure mode

Opus: financial-transform. A single derived column, the percentage discount
conversion, is the only error. Haiku: navigational/structural. It failed to get the
dbt project into a compiling state, so nothing materialized.

### DFC ablation on recharge001

The dfc-off and dfc-on pair isolate the discount transform. Both runs are Opus,
clean-terminate, both build the 8-row target.

dfc-off scores 0. Its discount branch uses the raw value, the same error as the mt8k
run:

```sql
cast(discount_value as {{ dbt.type_numeric() }}) as amount
```
(`.../claude-opus-4-8-dfc-off/recharge001/models/recharge__charge_line_item_history.sql`, line 21)

dfc-on scores 1. Its discount branch converts percentage discounts:

```sql
case
    when charge_discounts.value_type = 'percentage'
        then round(cast(charge_discounts.discount_value as {{ dbt.type_float() }}) / 100.0
            * cast(charges.total_line_items_price as {{ dbt.type_float() }}), 2)
    else cast(charge_discounts.discount_value as {{ dbt.type_float() }})
end as amount
```
(`.../claude-opus-4-8-dfc-on/recharge001/models/recharge__charge_line_item_history.sql`, lines 44-49)

The two produced models are otherwise the same. The discount `amount` expression is
the only functional difference, and it flips the score from 0 to 1. In the dfc-on
build the evaluator reports all seven checked columns matched, including `amount`.

---

## recharge002

### What the task is

Instruction: "Calculate daily and running totals for customer transactions, including
charges, discounts, taxes, refunds, and order quantities, and determine the number of
active months for each customer?"

Same Recharge project, a later mart. The start DuckDB
(`spider2-dbt/examples/recharge002/recharge.duckdb`) has 56 tables. This project ships
more built marts than recharge001: `recharge__billing_history`,
`recharge__customer_details`, `recharge__subscription_overview`,
`recharge__charge_line_item_history` (the recharge001 target, present here as a
starter file), and the intermediate `int_recharge__customer_daily_rollup.sql`.

The final mart `recharge__customer_daily_rollup.sql` is removed. The agent must build
it on top of the intermediate rollup, adding running totals and the active-months
metric. Confirmed by the starter diff: in the Opus run the target is agent-added and
the intermediate `int_recharge__customer_daily_rollup.sql` is agent-changed.

Target grain and size, from the gold DuckDB: `recharge__customer_daily_rollup` has 122
rows, 38 columns. One row per customer per active day.

### Where grading is difficult

The gold spec checks only 2 of 38 columns: `customer_id` (index 0) and
`active_months_to_date` (index 37). Every running-total column is unchecked.

This makes the task narrow but sharp. The row set (customer by day) must be exactly
right, and the single derived metric `active_months_to_date` must be computed with the
exact definition gold uses. There is no credit for the many running-total columns the
instruction describes. The whole grade rests on the row grain plus one column.

### Where and why the models fail

Opus (hit step cap at 50, target built with 122 rows). The row grain is right:
`customer_id` matches and the predicted table has 122 rows, the same as gold. Only
`active_months_to_date` is wrong.

The produced target
(`.../claude-opus-4-8-ecom-e3-mt8k/recharge002/models/recharge__customer_daily_rollup.sql`)
computes it as an integer month difference (line 63):

```sql
{{ dbt.datediff("first_charge_processed_at", "date_day", "month") }} as active_months_to_date
```

Verified against the DuckDBs, this produces a BIGINT taking values {0, 1, 2}. Gold is
DECIMAL(28,2) and takes fractional values that increment by about 0.03 per day: 0.03,
0.07, 0.10, 0.13, 0.17, 0.20, and so on. For customer 90000001 the first active days
read 0.03, 0.07, 0.10 in gold against 0, 0, 0 in the prediction.

The gold pattern is a fractional month of tenure that advances each day (1/30 = 0.033,
3/30 = 0.10, 6/30 = 0.20). This divisor is confirmed from the reference model, not
inferred. The sibling model
`spider2-dbt/examples/recharge002/models/recharge__customer_details.sql:12-13` computes
`round(cast({{ dbt.datediff("first_charge_processed_at", <endpoint>, "day") }} / 30 as
numeric), 2)` for its `active_months` column, and `recharge.yml:225` documents
`active_months_to_date` as "the number of months the customer has been active up to the
given day, calculated from their first charge." So the /30 day-count conversion and the
`first_charge_processed_at` anchor are stated in the project; the daily variant
substitutes `date_day` as the endpoint. Opus instead used whole calendar-month
`datediff`, so the one scored derived column is wrong across the table. This run hit the step cap, but the target was built and
the miss is a definition mismatch, not a truncation.

Progression note. In the earlier s50 batch (`ecom-e2-s50`) Opus recharge002 did not
build the target at all. It aborted at step 19 on an action-parse failure: at
max_tokens=2500 a large CreateFile SQL body was truncated mid-content so no closing
fence was emitted and the ReAct parser could not form an action. Raising max_tokens to
8192 in the mt8k batch removed that failure class. The mt8k run built all 122 rows and
failed on the single `active_months_to_date` column. So the max_tokens fix converted
this task from a truncation abort into a clean, single-column capability miss. This is
the one place where the batch progression changes the nature of the failure.

Haiku (hit step cap at 50, target not written). The diff against the starter shows
Haiku added no model file and only changed the intermediate
`int_recharge__customer_daily_rollup.sql`. The target `recharge__customer_daily_rollup`
is absent from `models/` and does not exist in the built database; the evaluator
reports it missing. So Haiku edited an upstream intermediate but never created the
final mart the task asks for.

### Failure mode

Opus: financial/temporal-transform on a single derived column. The grain is right and
one metric definition is wrong. Haiku: navigational/structural. It never wrote the
target mart.

---

## Cross-task synthesis

The three tasks share a shape. In each, the agent must build one or two removed marts
on top of a large pre-existing dbt project, and grading checks a small fixed set of
columns on those marts with a set comparison and no partial credit.

Opus and Haiku fail in two clearly separated ways.

Opus builds the target tables and gets the grain right. Its failures are localized to
specific transforms or bounds:
- shopify001: the daily mart spine is not clamped to the activity window (6 extra
  days), a temporal-window error, and it fails on the row-set mismatch even though the
  per-day aggregates are right and `shopify__products` is perfect.
- recharge001: one financial transform, the percentage-to-amount discount conversion,
  is missing.
- recharge002: one derived metric, `active_months_to_date`, uses the wrong units.

Each Opus failure is one fix away from a pass, and in each case the miss is a
transform or an invariant, not an inability to produce the artifact. The DFC ablation
on recharge001 demonstrates this directly: adding the percentage-conversion invariant
flips the score from 0 to 1 with no other change.

Haiku fails earlier, at materialization. Across all three mt8k tasks it produces zero
scored targets:
- shopify001: wrote no model files.
- recharge001: wrote a complete target model, but the project did not compile because
  it never fixed the `order_data` var, so nothing materialized.
- recharge002: edited an intermediate but never wrote the target mart.

The cleanest Opus-versus-Haiku split: Opus fails on the content of tables it
successfully builds; Haiku fails to build the tables. Raising the step cap from 30 to
50 and max_tokens from 2500 to 8192 did not change this split. The max_tokens fix
helped Opus recharge002 cross from a truncation abort to a built-but-wrong table, but
no Haiku target newly materialized under the higher limits. That points at capability
rather than budget for Opus; for Haiku it shows, at minimum, a failure to materialize
within 50 steps, with recharge001 showing a clear capability gap independent of the cap
(a complete target model left uncompiled by a project fix it never made).

## Run artifacts and trajectories

The six primary (mt8k) run trajectories, plus the two DFC-pair trajectories, are
copied to `docs/trajectories/`. Each file has the fields `Task`, `system_message`,
`trajectory` (a list of steps, each with `observation`, `thought`, `action`,
`response`), `finished`, and `result`. Open any of these to trace a claim to the
actual run.

| Trajectory file (`docs/trajectories/`) | Source run |
|---|---|
| `opus4.8_shopify001.trajectory.json` | `output/claude-opus-4-8-ecom-e3-mt8k/shopify001/` |
| `opus4.8_recharge001.trajectory.json` | `output/claude-opus-4-8-ecom-e3-mt8k/recharge001/` |
| `opus4.8_recharge002.trajectory.json` | `output/claude-opus-4-8-ecom-e3-mt8k/recharge002/` |
| `haiku4.5_shopify001.trajectory.json` | `output/claude-haiku-4-5-ecom-e3-mt8k/shopify001/` |
| `haiku4.5_recharge001.trajectory.json` | `output/claude-haiku-4-5-ecom-e3-mt8k/recharge001/` |
| `haiku4.5_recharge002.trajectory.json` | `output/claude-haiku-4-5-ecom-e3-mt8k/recharge002/` |
| `opus4.8_recharge001_dfc-off.trajectory.json` | `output/claude-opus-4-8-dfc-off/recharge001/` |
| `opus4.8_recharge001_dfc-on.trajectory.json` | `output/claude-opus-4-8-dfc-on/recharge001/` |

Source-run paths above are under `methods/spider-agent-dbt/`. For any run, the
per-column grading diagnostics are in `<run>/<task>/audit_metadata.json`, the produced
models in `<run>/<task>/models/`, the built database in `<run>/<task>/<db>.duckdb`,
and the dbt log in `<run>/<task>/logs/dbt.log`.

## Provenance and caveats

- Scores and per-column matched/unmatched come from each run's `audit_metadata.json`,
  which `score_run.py` produced from the official `duckdb_match`.
- Row counts, date ranges, and cell values were read directly from the start and gold
  DuckDBs with duckdb 1.5.4.
- Which files each agent authored was determined by hashing each run's `models/`
  against the pristine `examples/<task>/models/`, not from the agent's self-report.
- The gold model SQL is not present in the repo. Gold logic is described from gold
  table values and from the upstream models that ship in the project. The recharge002
  `active_months_to_date` divisor (day count over 30) is confirmed from the reference
  model `examples/recharge002/models/recharge__customer_details.sql:12-13` and the
  `recharge.yml:225` column doc, not only from the value pattern.
- Docker was down at the time of this writeup. No runs were executed and no code was
  changed. This analysis is read-only over existing files.
