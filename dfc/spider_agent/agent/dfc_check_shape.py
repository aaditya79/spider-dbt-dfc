"""DFC policy `shape`: a built target must have the columns and the row grain its
schema YAML declares. Gold-free and task-agnostic.

Motivation (Qwen3-235B, scaffold v4, 2026-09-21): every e-commerce cell built the
right-named table and `dbt run` was green, yet 4/4 scored 0 -- two dropped declared
columns (recharge001 emitted 18 columns, none of the 9 declared; holistic lost four
`klaviyo_sum_revenue_*`), one had the wrong grain (daily_shop: 10 rows of activity
days instead of one row per calendar day, 2077), one fanned out a join (discounts:
6 rows, NULL keys, where the YAML declares the key unique). The model treats the
YAML as a name lookup, not a spec, and `terminate`s once dbt is green.

What is checked, per target model (declared in models/**/*.yml but shipped without
a .sql file in the PRISTINE fixture -- the same definition namegate uses):

  1. present   -- only tables that EXIST are checked. A declared-but-unbuilt
                  model is namegate's job, and the shopify fixtures declare models
                  for other tasks that are never meant to be built here, so absence
                  is not a shape violation and target names are never revealed.
  2. columns   -- every column the YAML declares exists in the table. Safe to
                  enforce: the scorer matches gold columns by value against any
                  pred column, so extra/renamed columns never cost a pass.
  3. key       -- the YAML's uniqueness test (column-level `unique`, or model-level
                  `dbt_utils.unique_combination_of_columns`) holds, with dbt's own
                  semantics: `group by key having count(*) > 1` is a failure; NULLs
                  are grouped, not failed (gold itself has NULL key parts).
  4. grain     -- for a DENSE daily model only: declared columns include `date_day`
                  and the declared unique key is absent or has <= 2 columns
                  including `date_day` (daily_shop: no key; customer_daily_rollup:
                  [customer_id, date_day]). A wide key such as the holistic model's
                  [date_day, email, campaign_id, flow_id, ...] is a sparse event
                  grain and is skipped. If the produced DB has a calendar table
                  (`*calendar*` / `*spine*` with `date_day`), the model must cover at
                  least MIN_SPINE_COVERAGE of its days.

Everything comes from the fixture YAML and the produced DB; gold is never opened.
Declarations are read from `spider2-dbt/examples/<instance_id>/` (SPIDER2_EXAMPLES
overrides the location, as for namegate).
"""

import glob
import os

import duckdb
import yaml

_HERE = os.path.dirname(os.path.abspath(__file__))
_SPIDER2 = os.path.abspath(os.path.join(_HERE, "..", "..", "..", ".."))
EXAMPLES = (os.environ.get("SPIDER2_EXAMPLES")
            or os.path.join(_SPIDER2, "spider2-dbt", "examples"))

MIN_SPINE_COVERAGE = 0.5


# ------------------------------------------------------------- YAML spec ----

def _declared_specs(project_dir):
    """{model_name: {"columns": [...], "keys": [[col, ...], ...]}} from models/**/*.yml."""
    out = {}
    for path in glob.glob(os.path.join(project_dir, "models", "**", "*.yml"), recursive=True):
        try:
            doc = yaml.safe_load(open(path)) or {}
        except Exception:
            continue
        for m in (doc.get("models") or []):
            if not isinstance(m, dict) or not m.get("name"):
                continue
            cols = [c["name"] for c in (m.get("columns") or []) if isinstance(c, dict) and c.get("name")]
            keys = []
            for c in (m.get("columns") or []):
                if not isinstance(c, dict):
                    continue
                for t in (c.get("tests") or c.get("data_tests") or []):
                    if t == "unique" or (isinstance(t, dict) and "unique" in t):
                        keys.append([c["name"]])
            for t in (m.get("tests") or m.get("data_tests") or []):
                if isinstance(t, dict) and "dbt_utils.unique_combination_of_columns" in t:
                    combo = (t["dbt_utils.unique_combination_of_columns"] or {}).get("combination_of_columns") or []
                    if combo:
                        keys.append(list(combo))
            out[m["name"]] = {"columns": cols, "keys": keys}
    return out


def _sql_models(project_dir):
    return {os.path.basename(p)[:-4]
            for p in glob.glob(os.path.join(project_dir, "models", "**", "*.sql"), recursive=True)}


# ------------------------------------------------------------ DB probing ----

def _q(con, sql):
    return con.execute(sql).fetchall()


def _table_columns(con, name):
    rows = _q(con, f"select table_schema, table_name from information_schema.tables where table_name = '{name}'")
    if not rows:
        return None, None
    schema, _ = rows[0]
    cols = [r[0] for r in _q(con, f'select column_name from information_schema.columns where table_schema = \'{schema}\' and table_name = \'{name}\' order by ordinal_position')]
    return f'"{schema}"."{name}"', cols


def _spine_days(con):
    """Number of distinct date_day in the first calendar/spine table found, else None."""
    rows = _q(con, "select table_schema, table_name from information_schema.tables "
                   "where (table_name ilike '%calendar%' or table_name ilike '%spine%') "
                   "order by length(table_name)")
    for schema, name in rows:
        cols = [r[0] for r in _q(con, f"select column_name from information_schema.columns where table_schema = '{schema}' and table_name = '{name}'")]
        if "date_day" in cols:
            n = _q(con, f'select count(distinct date_day) from "{schema}"."{name}"')[0][0]
            if n:
                return n, f"{schema}.{name}"
    return None, None


# -------------------------------------------------------------- the check ----

def check_shape(produced_db_path):
    """Return {"status", "violations": [...], "checked_rows", "targets", "message"}."""
    run_dir = os.path.dirname(os.path.abspath(produced_db_path))
    instance_id = os.path.basename(run_dir)
    fixture = os.path.join(EXAMPLES, instance_id)
    empty = {"violations": [], "checked_rows": 0, "targets": []}
    if not os.path.isdir(fixture):
        return {"status": "error", **empty,
                "message": f"pristine fixture not found for instance {instance_id!r} at {fixture}"}
    if not os.path.exists(produced_db_path):
        return {"status": "error", **empty, "message": f"produced DuckDB not found: {produced_db_path}"}

    specs = _declared_specs(fixture)
    targets = sorted(set(specs) - _sql_models(fixture))
    if not targets:
        return {"status": "error", **empty, "message": "no declared-but-unbuilt model in the pristine fixture"}

    violations = []
    checked = 0
    try:
        con = duckdb.connect(produced_db_path, read_only=True)
    except Exception as e:
        return {"status": "error", **empty, "targets": targets, "message": f"cannot open produced DuckDB: {e}"}
    try:
        spine_n, spine_name = _spine_days(con)
        for name in targets:
            spec = specs[name]
            qname, cols = _table_columns(con, name)
            if qname is None:
                continue  # namegate's job; see docstring
            checked += 1
            missing = [c for c in spec["columns"] if c not in cols]
            if missing:
                violations.append({"table": name, "kind": "columns", "missing": missing,
                                   "why": f"`{name}` lacks {len(missing)} of its {len(spec['columns'])} declared columns: {', '.join(missing)}"})
            n_rows = _q(con, f"select count(*) from {qname}")[0][0]
            for key in spec["keys"]:
                if any(k not in cols for k in key):
                    continue  # reported under `columns` already
                klist = ", ".join(f'"{k}"' for k in key)
                dups = _q(con, f"select count(*) from (select {klist} from {qname} group by {klist} having count(*) > 1)")[0][0]
                if dups:
                    violations.append({"table": name, "kind": "key_duplicates", "key": key, "n": dups,
                                       "why": f"`{name}` violates its declared unique key ({', '.join(key)}): {dups} key value(s) appear more than once across {n_rows} rows"})
            dense = (not spec["keys"]) or any(len(k) <= 2 and "date_day" in k for k in spec["keys"])
            if dense and "date_day" in spec["columns"] and "date_day" in cols and spine_n:
                days = _q(con, f'select count(distinct "date_day") from {qname}')[0][0]
                if days < MIN_SPINE_COVERAGE * spine_n:
                    violations.append({"table": name, "kind": "grain", "days": days, "spine_days": spine_n,
                                       "spine": spine_name,
                                       "why": f"`{name}` covers only {days} distinct date_day values but the project's calendar ({spine_name}) has {spine_n} days; a daily model is one row per calendar day (per entity), with zeros/nulls on days without activity"})
    finally:
        con.close()

    if checked == 0:
        return {"status": "pass", "violations": [], "checked_rows": 0, "targets": targets,
                "message": "no declared-but-unbuilt model has a table yet (nothing to shape-check; namegate covers absence)"}
    status = "violation" if violations else "pass"
    return {"status": status, "violations": violations, "checked_rows": checked, "targets": targets,
            "message": ("; ".join(v["why"] for v in violations) if violations
                        else f"{checked} built target model(s) match their declared columns, keys and grain")}


def retry_message(verdict):
    """Feedback for the SAME Pi session: what the built table gets wrong vs its YAML."""
    lines = []
    for v in verdict["violations"]:
        lines.append(f"- {v['why']}")
    return (
        "DFC policy violation -- the model you built does not match its declaration in the "
        "project's schema YAML. `dbt run` succeeding is not the finish line; the YAML is the "
        "spec. Problems found:\n" + "\n".join(lines) + "\n\n"
        "Fix the model SQL so that: every declared column is present with that exact name; the "
        "declared unique key has no duplicate values (check your joins -- a fan-out or an outer "
        "join that adds extra rows is wrong); and a daily model is built on the "
        "project's calendar spine, one row per day (per entity), not only on days with activity. "
        "Then re-run `dbt run --profiles-dir .`, verify with duckdb_sql (`describe <model>`, "
        "`select count(*), count(distinct <key>) from <model>`), and only then call terminate."
    )


if __name__ == "__main__":
    import argparse, json
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("db", help="path to a produced .duckdb under <runs_root>/<exp>/<instance_id>/")
    ap.add_argument("--retry-message", action="store_true")
    a = ap.parse_args()
    v = check_shape(a.db)
    print(json.dumps(v, indent=2, default=str))
    if a.retry_message and v["status"] == "violation":
        print("\n" + retry_message(v))
