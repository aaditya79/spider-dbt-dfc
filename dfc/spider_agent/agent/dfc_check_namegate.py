#!/usr/bin/env python
"""DFC policy checker #4 -- MODEL-NAME GATE (task-agnostic).

Invariant: a model the agent creates must carry a name the project's schema YAML
actually declares. The agent's job in spider2-dbt is to supply SQL for models that
the fixture already declares in `models/**/*.yml` but ships without a `.sql` file.
Inventing a new, undeclared name means the scorer's target table is never built.

Observed failure this targets (e-commerce baseline, 4 of 7 failures):
  opus/recharge001  built `recharge__charge_details`                 (spec: recharge__charge_line_item_history)
  opus/recharge002  built `recharge__customer_daily_transactions`    (spec: recharge__customer_daily_rollup)
  haiku/shopify001  built `shopify__daily_shop_performance`          (spec: shopify__daily_shop)
  haiku/shopify002  built `shopify__discount_codes_comprehensive`    (spec: shopify__discounts)
In 3 of those 4 the declaring YAML was never opened; the name was generated from the
agent's own prose description of what it was building.

NO GOLD ACCESS
--------------
The checker never reads the gold DuckDB or the eval spec. "What should have been
built" is derived purely from the project itself: models DECLARED in the pristine
fixture's schema YAML that ship with no `.sql` file. That set is exactly what a
developer reading the project would see as unfinished work. It is usually small
(1 for recharge001/002, 5-6 for the shopify tasks) and always contains the scored
target, but the checker does not know -- and does not need to know -- which member
is the graded one.

WHY THE PRISTINE FIXTURE, NOT THE RUN DIR
-----------------------------------------
Declarations are read from `spider2-dbt/examples/<instance_id>/`, not from the run
directory. Agents edit schema YAML: in the baseline, opus/recharge001 wrote its own
`- name: recharge__charge_details` entry into a YAML at turn 9 (tool call 12). Reading
declarations from the run dir would let an agent legalise its invented name by adding
it to the spec, defeating the check. The shipped fixture is the authority.

VIOLATION CONDITION (deliberately narrow, to avoid false positives)
-------------------------------------------------------------------
    offenders        = agent-created models that materialized and are NOT declared
    built_candidates = declared-but-unshipped models that DID materialize
    violation  <=>  offenders  AND  NOT built_candidates

Both halves matter. Requiring `not built_candidates` means an agent that built the
right model and *also* added an undeclared helper model is NOT flagged -- it engaged
with the spec. Only an agent that invented a name and built none of the project's
declared-but-unfinished models is steered.

This checker STEERS the agent (feeds a retry message on violation). It NEVER reports
the final pass/fail -- that comes from duckdb_match. See check_namegate().
"""
import glob
import os

import duckdb
import yaml

# spider2-dbt/examples/, resolved from this file's location inside the repo.
#   <...>/Spider2/methods/spider-agent-dbt/spider_agent/agent/dfc_check_namegate.py
_HERE = os.path.dirname(os.path.abspath(__file__))                       # .../spider_agent/agent
_METHODS_DBT = os.path.abspath(os.path.join(_HERE, "..", ".."))          # .../spider-agent-dbt
_SPIDER2 = os.path.abspath(os.path.join(_METHODS_DBT, "..", ".."))       # .../Spider2
# The derivation above assumes this file sits at its canonical depth inside the
# Spider2 clone. It does not when the file is loaded from the tracked `dfc/` mirror
# (dfc/spider_agent/agent/), where walking up four levels lands outside the repo and
# every check returns "pristine fixture not found". SPIDER2_EXAMPLES overrides it, so
# the mirror is usable without a Spider2 clone at the expected relative path.
EXAMPLES = (os.environ.get("SPIDER2_EXAMPLES")
            or os.path.join(_SPIDER2, "spider2-dbt", "examples"))


def _declared_models(project_dir):
    """Model names declared under `models:` in the project's schema YAML.

    Uses a real YAML parse, not a regex: `- name:` also appears under `columns:`,
    and a regex over the raw text picks up every column in the project (366 spurious
    'models' in shopify002, vs 28 real ones).
    """
    out = set()
    for path in glob.glob(os.path.join(project_dir, "models", "**", "*.yml"), recursive=True):
        try:
            doc = yaml.safe_load(open(path)) or {}
        except Exception:
            continue  # a malformed YAML is the agent's problem, not the gate's
        for entry in (doc.get("models") or []):
            if isinstance(entry, dict) and entry.get("name"):
                out.add(entry["name"])
    return out


# Helper models the agent legitimately writes to get a broken fixture to compile
# (recharge002's `order_data` blocker forces stg_recharge__* rebuilds). Flagging
# them as "undeclared names" told the agent to stop doing the one thing that
# unblocks the project, and it then ran out of budget with nothing built.
HELPER_PREFIXES = ("stg_", "int_", "tmp_", "base_")


def _is_helper(name):
    return name.lower().startswith(HELPER_PREFIXES)


def _sql_models(project_dir):
    """Model names that have a .sql file under models/."""
    return {os.path.basename(p)[:-4]
            for p in glob.glob(os.path.join(project_dir, "models", "**", "*.sql"), recursive=True)}


def check_namegate(produced_db_path):
    """Evaluate the model-name gate against a produced DuckDB.

    The run directory and the instance id are derived from the DuckDB path
    (<runs_root>/<experiment_id>/<instance_id>/<db>.duckdb), which keeps the
    single-argument checker contract shared with dfc_check.py.

    Returns dict:
      {"status": "pass"|"violation"|"error",
       "violations": [ {built, why}, ... ],
       "checked_rows": int,               # number of agent-created models inspected
       "undeclared_built": [...], "declared_unbuilt": [...], "built_candidates": [...],
       "message": str}
    """
    run_dir = os.path.dirname(os.path.abspath(produced_db_path))
    instance_id = os.path.basename(run_dir)
    fixture = os.path.join(EXAMPLES, instance_id)

    empty = {"violations": [], "checked_rows": 0, "undeclared_built": [],
             "declared_unbuilt": [], "built_candidates": []}
    if not os.path.isdir(fixture):
        return {"status": "error", **empty,
                "message": f"pristine fixture not found for instance {instance_id!r} at {fixture}"}
    if not os.path.exists(produced_db_path):
        return {"status": "error", **empty,
                "message": f"produced DuckDB not found: {produced_db_path}"}

    spec_declared = _declared_models(fixture)
    if not spec_declared:
        return {"status": "error", **empty,
                "message": f"no models declared in any schema YAML under {fixture}/models"}
    fixture_sql = _sql_models(fixture)
    run_sql = _sql_models(run_dir)

    try:
        con = duckdb.connect(produced_db_path, read_only=True)
        try:
            materialized = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
        finally:
            con.close()
    except Exception as e:
        return {"status": "error", **empty,
                "message": f"could not read produced DuckDB: {type(e).__name__}: {e}"}

    candidates = sorted(spec_declared - fixture_sql)          # declared, shipped without SQL
    agent_created = run_sql - fixture_sql                      # models the agent added
    built_new = sorted(m for m in agent_created if m in materialized)
    offenders = sorted(m for m in built_new
                       if m not in spec_declared and not _is_helper(m))
    built_candidates = sorted(c for c in candidates if c in materialized)
    unmet = sorted(c for c in candidates if c not in materialized)

    common = {"checked_rows": len(built_new), "undeclared_built": offenders,
              "declared_unbuilt": unmet, "built_candidates": built_candidates}

    if not candidates:
        return {"status": "pass", "violations": [], **common,
                "message": "project declares no unbuilt models; name gate not applicable"}
    if not built_new:
        return {"status": "error", "violations": [], **common,
                "message": "no agent-created model materialized; nothing to name-check"}

    if offenders and not built_candidates:
        violations = [{
            "built": m,
            "why": (f"model {m!r} was created and materialized but is not declared in any "
                    f"schema YAML of the shipped project; declared-but-unbuilt models are "
                    f"{unmet}"),
        } for m in offenders]
        return {"status": "violation", "violations": violations, **common,
                "message": (f"{len(offenders)} model(s) built under undeclared name(s) "
                            f"({', '.join(offenders)}) while {len(unmet)} declared model(s) "
                            f"remain unbuilt ({', '.join(unmet)})")}

    if offenders:
        return {"status": "pass", "violations": [], **common,
                "message": (f"built declared model(s) {built_candidates}; "
                            f"also created undeclared helper(s) {offenders} -- not steered")}
    return {"status": "pass", "violations": [], **common,
            "message": f"all {len(built_new)} agent-created model(s) carry declared names"}


def retry_message(verdict):
    """Violation-specific RETRY feedback fed back into the SAME Pi session.

    Names the offending model, names the declared-but-unbuilt models the project is
    actually asking for, and tells the agent to rebuild under a declared name. It does
    NOT say which candidate is the graded one -- that stays the agent's job, so the
    discovery step remains measurable.
    """
    built = ", ".join(f"`{v['built']}`" for v in verdict["violations"])
    n_built = len(verdict["violations"])
    that_name = "that name appears" if n_built == 1 else "those names appear"
    unmet = verdict.get("declared_unbuilt") or []
    unmet_list = ", ".join(f"`{m}`" for m in unmet)
    if len(unmet) == 1:
        unmet_clause = (f"The following declared model still has no materialized table: "
                        f"{unmet_list}. Open the schema YAML that declares it, read its "
                        f"column list and description to confirm it is what this task asks "
                        f"for, then rebuild your model under that exact declared name")
    else:
        unmet_clause = (f"The following declared models still have no materialized table: "
                        f"{unmet_list}. Open the schema YAML that declares them, read their "
                        f"column lists and descriptions to work out which one this task is "
                        f"asking for, then rebuild your model under that exact declared name")
    return (
        f"DFC policy violation -- model name not declared in the project spec. You created "
        f"and materialized {built}, but {that_name} in no schema YAML in this project. "
        f"dbt models in this project are declared in `models/**/*.yml` first; your job is to "
        f"supply the SQL for models that are declared but ship without a `.sql` file. "
        f"{unmet_clause} (matching its declared columns), and re-run `dbt run`. Do not add a "
        f"new entry to the YAML for your own name -- use the name the project already "
        f"declares. You may delete the incorrectly-named model file."
    )


if __name__ == "__main__":
    import argparse, json
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("db", help="path to a produced (or gold) recharge/shopify .duckdb")
    ap.add_argument("--retry-message", action="store_true",
                    help="also print the RETRY message that would be sent on violation")
    a = ap.parse_args()
    v = check_namegate(a.db)
    print(json.dumps(v, indent=2, default=str))
    if a.retry_message and v["status"] == "violation":
        print("\n--- RETRY MESSAGE ---\n" + retry_message(v))
