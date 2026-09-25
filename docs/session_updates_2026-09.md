# Spider2-dbt on Pi: what changed and what we know, Sep 16-24 2026

Branch `spider_pi_2.0`. Scope: get the five Spider2-dbt e-commerce tasks running
through the Pi coding agent with Qwen3-235B, establish a clean baseline, and work out
what actually blocks completion. DFC policies exist and are wired but are OFF in the
baseline by design.

Companion documents:
- `docs/harness_fixes_2026-09-17.md` -- per-fix Problem / Fix / Where / **Revert**, 20 items
- `docs/ecommerce_baseline_analysis.md` -- the earlier Opus/Haiku trace analysis
- `docs/qwen_arm_blocker.md` -- **stale**: Qwen is unblocked (see item 7 below)

---

## 1. Headline numbers

All Qwen3-235B, 5 e-comm tasks x 3 seeds unless noted, same scorer
(`score_run.py` / `duckdb_match`).

| batch | scaffold | policies | pass |
|---|---|---|---|
| ecom-v2 (Sep 16) | v1 | off | 2/15 |
| ecom-v3 | v5 | shape,recharge001 | 4/15 |
| ecom-v4 | v5 | namegate,shape,recharge001 | 4/15 |
| ecom-v5 | v5 | shape | 4/15 |
| ecom-v6 (partial, stopped) | v5 | namegate,shape,values,recharge001 | 4/6 |
| **base-v1 (the baseline)** | **v5** | **off** | **1/14** |
| ref-opus (1 seed/task) | v5 | off | 1/4 |

`base-v1` is the number to quote for "Qwen on this harness, unaided": **1/14 scored,
or 1/11 excluding shopify001** (see item 4). One cell was lost to API errors and is
unscored, not counted as a failure.

The raw pass count is flat across ecom-v3..v6, but the failure *composition* changed
completely, which is the real finding (item 2).

## 2. What actually blocks completion

Counted over `base-v1` (Qwen, unaided) and `ref-opus` (Opus, unaided, same harness):

| failure mode | Qwen | Opus |
|---|---|---|
| never built the target | 6 | **0** |
| wrong grain (daily model sparse) | 3 | 0 |
| exactly one wrong column | 1 | **2** |
| join fan-out (extra rows) | 1 | 0 |
| built but empty | 1 | 0 |
| benchmark defect (see item 4) | -- | 1 |
| PASS | 1 | 1 |

**The two models fail differently.** Qwen's dominant mode is not producing the table
at all; Opus always produces it, with the right name, columns and row count, and then
lands one wrong value. Neither completes these tasks reliably unaided -- the working
assumption that Opus succeeds without help does not hold.

Not a step-budget problem: every passing cell anywhere in this work used **21-90 tool
calls** against a 150 budget, and the only cells that hit the cap had produced nothing
in 150 calls.

Shared value defects, same column, both models:
- recharge001 `amount` -- Opus emits the raw discount values (8.00, 15.00 vs gold
  0.96, 1.49); Qwen derives correctly then inverts the sign (-0.96, -1.49).
- recharge002 `active_months_to_date` -- Opus and Qwen both wrong; Qwen counts whole
  months (2,2,2,2; max 4) where gold advances ~1/30 per day (0.03, 0.07; max 2.03).

## 3. Evidence that steering converts failures

Not a claim about pass rates -- a claim about specific cells, with traces.

- **recharge001 was 0/12** across ecom-v2/v3/v4/v5. In ecom-v6 it **passed**: amount
  violation -> retry -> still wrong -> retry with the corrected message -> **one edit**
  -> exact match. The decisive change was the message, not the checker (item 6c).
- **shopify002** passes in v3, v5 and v6 all followed a `shape` violation naming
  missing declared columns; the model added them and matched.
- **ecom-v6 DFC stats**: 14 checks, 6 violations, all 6 genuine, **0 false positives**,
  3 of 4 passes came after steering.

Scaling caveat, stated plainly: `shape`/`namegate`/`values` are **task-agnostic** --
derived mechanically from the project's YAML, so they apply to any task. The
`recharge001` amount invariant is **hand-written per task** and does not scale. Of the
v6 steered passes, 2 of 3 came from task-agnostic checks. These two rates should never
be reported as one number.

No checker reads a gold database. Invariants are recomputed from the project's own
source data.

## 4. Two benchmark defects, settled

**shopify001 is unreachable at today's date.** `models/utils/shopify__calendar.sql`
ships complete (not a target) and spines to `end_date="current_date"`. Gold was built
when that was 2024-09-07 (2077 rows). The same model now yields 2820/2822/2823 rows on
Sep 21/23/24 -- it gains a row per day. **Opus produced 2083 rows** and built
`shopify__products` exactly right; several Qwen cells did the same. No model behaviour
fixes this. Recorded in every run record as `known_defect`; exclude from pass rates.

**holistic is under-specified.** Its YAML declares 28 columns; gold has 47, and the
scorer checks 4 `klaviyo_sum_revenue_*` columns the YAML never mentions. They are not
unreachable -- the shipped `int__daily_klaviyo_user_metrics.sql` generates
`sum_revenue_<metric>` from `var('klaviyo__sum_revenue_metrics')`, so the target drops
columns its own upstream produces. A dropped-upstream-column check would catch it;
not built.

## 5. Harness: 20 fixes, all reversible

Full detail with per-item revert instructions in `docs/harness_fixes_2026-09-17.md`.
Summary of what mattered:

- **Malformed tool names killed runs silently.** A tool call named `"dbt deps"` makes
  Bedrock reject every later request in the session; 3 of 15 cells died this way and
  were scored 0. Now: a Pi extension rewrites such names before they enter history, and
  any surviving failure is recorded as `HARNESS_ERROR` with `score: null` instead of a
  fake zero. Verified live -- one v3 cell hit a bad name, was absorbed, and passed.
- **`duckdb_sql` and `terminate` tools** (ported from `codeboi07/Self-improving-Harness`,
  reference only -- nothing pushed there). The model kept hallucinating a `python` tool
  because it wanted a query tool; now it has one, read-only. Result: 0 hallucinated
  tool names and 0 shell-outs for queries in the following batch.
- **Stall watchdog** (`--stall-timeout`, 600s). Bedrock went silent mid-stream on
  multiple occasions, hanging cells for the full 40-minute wall clock; the worst
  incident took 4 of 5 parallel cells at once. Now aborted, classified, and re-queued.
- **Token/cost accounting was ~80x under-reported** (last turn only). A run recorded
  as $0.03 actually cost $2.56.
- **`shape` fires on an empty database.** The dominant v4 failure was runs that built
  nothing while every policy passed vacuously -- one ran `dbt run` on the project's
  existing models, saw PASS=34 ERROR=0, and terminated.
- **Three checker bugs of ours** cost cells: case-sensitive column comparison
  (`CHARGE_ID` read as a missing `charge_id`), `namegate` flagging the `stg_*` models
  the agent legitimately wrote to work around a broken fixture, and the amount message
  mis-diagnosing a sign error as a raw-value error.
- **Scorer patch** (ported, `d5cfe39`): `ignore_order` sorted numerics as strings, so
  float rounding tails could fail a correct table. Re-scored all 17 cells then on
  disk: no score changed.
- **Trajectories de-duplicated** at write time: Pi's stream repeats each assistant
  message 2-3x and streams per-token deltas. 1022 -> 264 lines on one cell.

## 6. Scaffold history

The task prompt is recorded verbatim in every run record as `system_prompt`, with
`scaffold_version` for filtering.

| v | change |
|---|---|
| 1 | Spider `DBT_SYSTEM` ported to Pi-native tools (the ACTION SPACE became a `# TOOLS` section) |
| 2 | exact tool names, `read` needs `path`, 50KB cap guidance, prefer `write` over `edit` |
| 3 | rule 8 names `dbt deps` as the one permitted network use (it contradicted rule 4) |
| 4 | `duckdb_sql` + `terminate`; rule 9: never copy `dbt_packages/` into `models/`, macros belong in `macros/`, build the target first |
| 5 | rule 10: verify declared columns / unique key / daily grain before terminate |
| 6 | **the blueprint** (item 7) |

## 7. The blueprint (scaffold v6, untested)

Harvested from behaviour, not invented. Measured over `base-v1` and `ecom-v6`
trajectories:

| | PASS (n=5) | FAIL (n=11) |
|---|---|---|
| ran `describe` before terminate | **100%** | 64% |
| ran `count(*)` before terminate | **100%** | 55% |
| ran a distinct-key check | 0% | 9% |

The rushers always failed: `base-v1` shopify001-r2/r3 terminated at call 23 with 2 SQL
calls. Passes took 36-90 calls with 4-22 verification queries. Nobody, passing or
failing, ever checked the declared unique key -- which is exactly what `shape` does
mechanically.

Scaffold v6 adds a `# BEFORE YOU CALL terminate #` section with four checks: declared
columns present, row count matches the described grain, the declared unique key has no
duplicates, derived values have the implied sign and magnitude. Gold-free and
task-agnostic -- it names properties, never a value or a table name.

**Caveats on record:** n=5 passes, correlational, and 3 of those were already
DFC-steered (the steering told them to verify), so the signal is partly circular.
`base-v1` shopify002-r2 is the one uncontaminated passing example. v6 has not been run.

## 8. Infrastructure notes

- **Qwen3-235B** runs through a tagged application inference profile
  (`dfc-qwen3-235b`, us-east-2). The bare model id is IAM-denied by
  `RequiredTagsPolicy`, and Pi needs `~/.pi/agent/models.json` to know the real 65536
  max-out. `docs/qwen_arm_blocker.md` is stale; the fix was the models.json format.
- **Opus 4.8 needed the same treatment**, discovered this session: the untagged system
  profile `us.anthropic.claude-opus-4-8` is explicitly denied by the same policy.
  Created `dfc-opus-4-8`
  (`arn:aws:bedrock:us-east-1:920736616554:application-inference-profile/t65nyo4xtz4t`,
  tagged `project=data-flow-control` / `billing-tag1=aup2005`).
- `pi_runner/pi_models.json` is the tracked copy of both entries;
  `check_models_json()` refuses to start an ARN-model run without it.
- The Pi clone is **pristine upstream** (`a0bb4a48`); all customisation is in
  `pi_runner/` plus the loadable extension. `--no-pi-extension` gives stock Pi.
- Bedrock transient failures are frequent enough to matter: `base-v1` alone absorbed
  ~10 stalls/API errors across 15 cells. Without the watchdog and retry those would
  have been silent zeros.

## 9. Open work, in the order I would do it

1. **Re-run the two cells lost to infrastructure**: Qwen holistic-r2 (0 calls in 50
   min), Opus shopify002 (stall at call 17).
2. **`base-v2`**: scaffold v6, policies off. Single variable -- does the blueprint
   move the number on its own? This is the cleanest available test.
3. **Agnostic-policy arm**: `namegate,shape,values` only, no per-task invariant. This
   is the rate that scales and the one worth publishing.
4. **Opus + amount checker, one cell.** The cheapest possible DFC demonstration: if one
   retry converts a frontier model's `amount` error, the claim is not about weak models.
5. **Dropped-upstream-column check** for the holistic case (item 4).
6. **Decide two policy questions**: whether editing `dbt_packages/` is a legitimate
   repair of a broken fixture (Qwen's holistic pass did it extensively; rule 9 forbids
   copying but not editing), and whether to tag each policy agnostic vs task-specific in
   the run record so the two rates cannot be merged by accident.

**Not recommended:** hashing gold values as hints. Even hashed it is a grader oracle --
the agent passes by guess-and-check (the recharge001 sign error has a candidate space of
two), the number stops being comparable to any published Spider2 result, and it scales
worse than invariants since every value needs a gold extraction. Worth building only as
an explicitly labelled ceiling arm, never as the headline.
