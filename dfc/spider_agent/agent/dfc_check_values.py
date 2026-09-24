"""DFC policy `values`: gold-free value invariants on a built target.

Three checks, each derived from a failure observed in the ecom-v5 batch
(2026-09-24) and each decidable from the produced DuckDB plus the fixture YAML:

  1. null_key      -- a declared *_id column that is 100% NULL across every row.
                      Restricted to keys on purpose: gold itself carries all-NULL
                      MEASURE columns (shopify002 `allocation_limit` is NULL in
                      gold's own 3 rows), so emptiness alone proves nothing. A
                      foreign key that is NULL on every existing row does.
                      shopify002-r1 selected `price_rule_id` but never joined
                      discount_code -> price_rule, so the column existed and was
                      entirely NULL (gold: 12543, 12543, 32543). A column the
                      project declares is not meant to be uniformly empty, and an
                      unjoined key is the usual cause.

  2. nonneg        -- a running "to date" / "months" metric must not be negative.
                      Documented in docs/ecommerce_baseline_analysis.md 5.2: the
                      recharge002 guard covers a NULL first_charge_date but not
                      `date_day < first_charge_date`, so spine days before a
                      customer's first charge produce negative months.

  3. daily_step    -- a running metric on a daily grain must move in daily-sized
                      steps. recharge002-r3 emitted whole integers (2, 2, 2, 2;
                      max 4) where the metric advances ~1/30 per day (gold: 0.03,
                      0.07, 0.10, 0.13; max 2.03). Consecutive days differing by
                      >= DAILY_STEP_MAX means the metric counts calendar units
                      instead of elapsed time.

Checks 2 and 3 apply only to columns whose name marks them as cumulative
(`_to_date`, `months_active`, ...) on a table that has `date_day`; nothing here
is task-specific and gold is never opened.
"""

import glob
import os

import duckdb
import yaml

_HERE = os.path.dirname(os.path.abspath(__file__))
_SPIDER2 = os.path.abspath(os.path.join(_HERE, "..", "..", "..", ".."))
EXAMPLES = (os.environ.get("SPIDER2_EXAMPLES")
            or os.path.join(_SPIDER2, "spider2-dbt", "examples"))

RUNNING_SUFFIXES = ("_to_date", "_todate", "_running", "_cumulative")
DAILY_STEP_MAX = 0.5
MIN_ROWS_FOR_NULL_CHECK = 2


def _declared(project_dir):
    """{model: [column, ...]} for every model declared in the fixture YAML."""
    out = {}
    for path in glob.glob(os.path.join(project_dir, "models", "**", "*.yml"), recursive=True):
        try:
            doc = yaml.safe_load(open(path)) or {}
        except Exception:
            continue
        for m in (doc.get("models") or []):
            if isinstance(m, dict) and m.get("name"):
                out[m["name"]] = [c["name"] for c in (m.get("columns") or [])
                                  if isinstance(c, dict) and c.get("name")]
    return out


def _sql_models(project_dir):
    return {os.path.basename(p)[:-4]
            for p in glob.glob(os.path.join(project_dir, "models", "**", "*.sql"), recursive=True)}


def _resolve(con, name):
    """(quoted_name, {lowercase_column: actual_column}) or (None, None)."""
    rows = con.execute("select table_schema, table_name from information_schema.tables "
                       f"where lower(table_name) = lower('{name}')").fetchall()
    if not rows:
        return None, None
    schema, actual = rows[0]
    cols = [r[0] for r in con.execute(
        "select column_name from information_schema.columns where table_schema = "
        f"'{schema}' and table_name = '{actual}' order by ordinal_position").fetchall()]
    return f'"{schema}"."{actual}"', {c.lower(): c for c in cols}


def _is_numeric(con, qname, col):
    t = con.execute(f"select data_type from information_schema.columns where "
                    f"lower(column_name) = lower('{col}')").fetchall()
    return bool(t) and any(k in str(t[0][0]).upper()
                           for k in ("INT", "DECIMAL", "NUMERIC", "DOUBLE", "FLOAT", "REAL"))


def check_values(produced_db_path):
    """Return {"status", "violations", "checked_rows", "message"}."""
    run_dir = os.path.dirname(os.path.abspath(produced_db_path))
    instance_id = os.path.basename(run_dir)
    fixture = os.path.join(EXAMPLES, instance_id)
    empty = {"violations": [], "checked_rows": 0}
    if not os.path.isdir(fixture):
        return {"status": "error", **empty,
                "message": f"pristine fixture not found for instance {instance_id!r}"}
    if not os.path.exists(produced_db_path):
        return {"status": "error", **empty, "message": "produced DuckDB not found"}

    declared = _declared(fixture)
    targets = sorted(set(declared) - _sql_models(fixture))
    violations, checked = [], 0
    try:
        con = duckdb.connect(produced_db_path, read_only=True)
    except Exception as e:
        return {"status": "error", **empty, "message": f"cannot open produced DuckDB: {e}"}
    try:
        for name in targets:
            qname, cols = _resolve(con, name)
            if qname is None:
                continue                      # shape's nothing_built covers absence
            checked += 1
            n_rows = con.execute(f"select count(*) from {qname}").fetchone()[0]
            if not n_rows:
                continue

            # 1. all-NULL declared join key
            if n_rows >= MIN_ROWS_FOR_NULL_CHECK:
                for want in dict.fromkeys(declared[name]):
                    actual = cols.get(want.lower())
                    if actual is None or not want.lower().endswith("_id"):
                        continue              # shape reports missing columns; measures may be NULL
                    nn = con.execute(f'select count("{actual}") from {qname}').fetchone()[0]
                    if nn == 0:
                        violations.append({
                            "table": name, "kind": "null_key", "column": want,
                            "why": (f"`{name}`.`{want}` is NULL in all {n_rows} rows. A declared "
                                    f"identifier column that is empty on every row means the join "
                                    f"that should supply it never matched -- check the join key and "
                                    f"direction")})

            # 2/3. running metrics
            day = cols.get("date_day")
            for want in dict.fromkeys(declared[name]):
                actual = cols.get(want.lower())
                if actual is None:
                    continue
                low = want.lower()
                if not (low.endswith(RUNNING_SUFFIXES) or "months" in low):
                    continue
                try:
                    neg = con.execute(f'select count(*) from {qname} where "{actual}" < 0').fetchone()[0]
                except Exception:
                    continue                  # not numeric
                if neg:
                    violations.append({
                        "table": name, "kind": "negative_running", "column": want, "n": neg,
                        "why": (f"`{name}`.`{want}` is negative in {neg} of {n_rows} rows. A "
                                f"cumulative 'to date' metric cannot go below zero -- guard the "
                                f"rows before the entity's first event, not only NULL ones")})
                if day is None:
                    continue
                keys = [c for c in cols.values() if c.lower().endswith("_id")]
                part = f'partition by "{keys[0]}" ' if keys else ""
                try:
                    jump = con.execute(
                        f'select count(*) from (select "{actual}" - lag("{actual}") over '
                        f'({part}order by "{day}") as d from {qname}) where abs(d) >= {DAILY_STEP_MAX}'
                    ).fetchone()[0]
                except Exception:
                    continue
                if jump:
                    violations.append({
                        "table": name, "kind": "daily_step", "column": want, "n": jump,
                        "why": (f"`{name}`.`{want}` changes by {DAILY_STEP_MAX} or more between "
                                f"consecutive days in {jump} place(s). On a daily grain this metric "
                                f"should advance by roughly one day's worth each row (about 1/30 of "
                                f"a month), not in whole units -- measure elapsed time, do not count "
                                f"calendar months")})
    finally:
        con.close()

    status = "violation" if violations else "pass"
    return {"status": status, "violations": violations, "checked_rows": checked,
            "message": ("; ".join(v["why"] for v in violations) if violations
                        else f"{checked} built target(s) pass the value invariants")}


def retry_message(verdict):
    lines = "\n".join(f"- {v['why']}" for v in verdict["violations"])
    return (
        "DFC policy violation -- the table is built but some values cannot be right.\n"
        f"{lines}\n\n"
        "Fix the model SQL and re-run `dbt run --profiles-dir .`, then check with duckdb_sql "
        "(for an all-NULL column, confirm the join key and that the join actually matches rows; "
        "for a cumulative metric, check the guard on rows before the first event and that the "
        "metric measures elapsed time rather than counting whole periods). Only call terminate "
        "once the corrected values are in the table."
    )


if __name__ == "__main__":
    import argparse, json
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("db")
    ap.add_argument("--retry-message", action="store_true")
    a = ap.parse_args()
    v = check_values(a.db)
    print(json.dumps(v, indent=2, default=str))
    if a.retry_message and v["status"] == "violation":
        print("\n" + retry_message(v))
