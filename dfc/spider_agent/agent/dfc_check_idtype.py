#!/usr/bin/env python
"""DFC post-materialization policy checker #2 -- recharge001 id-column TYPE invariant.

Invariant (grounded in the produced DuckDB itself, no gold access):
for the target table recharge__charge_line_item_history, the identifier columns

    charge_id    <- charge_data.ID
    customer_id  <- charge_data.CUSTOMER_ID
    address_id   <- charge_data.ADDRESS_ID

must carry the same TYPE CATEGORY as the source column they are derived from.
Those source columns are BIGINT in the recharge001 fixture, so the materialized
columns must stay numeric. Emitting them as VARCHAR/TEXT is a violation.

WHY THIS MATTERS (the failure it catches): the official evaluator compares
column vectors elementwise in evaluation_suite/eval_utils.py::compare_pandas_table.
Its numeric branch requires BOTH sides to be numeric:

    elif isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if not math.isclose(float(a), float(b), abs_tol=tol): return False
    elif a != b: return False

so '400000001' (str) vs 400000001 (int) falls through to `a != b` and the whole
column is scored unmatched even though every value is correct.

Observed source of the defect: models/standardized_models/recharge__line_item_enhanced.sql
(a sibling model in the fixture) wraps its identifiers in
`cast(... as {{ dbt.type_string() }})`. Agents that read that file sometimes copy the
idiom onto the scored id columns. The fix is to NOT cast these three to string.

DELIBERATELY NOT STRICTER THAN THE EVALUATOR
--------------------------------------------
This checker flags ONLY string-typed id columns, not every deviation from BIGINT.
A DOUBLE/DECIMAL id would still satisfy the evaluator (both sides numeric ->
math.isclose), so flagging it would steer the agent on something that does not
affect the score. Same philosophy as the tolerance note in dfc_check.py: mirror
duckdb_match, do not tighten.

SCOPE NOTE: source_index is intentionally NOT checked. duckdb_match for this task
compares column indices [0,3,4,5,6,7,8]; source_index (index 2) and charge_row_num
(index 1) are unscored, and a passing Opus run casts source_index to string.

This checker STEERS the agent (feeds a retry message on violation). It NEVER reports
the final pass/fail -- that comes from duckdb_match. See check_recharge001_id_types().
"""
import duckdb

TARGET_TABLE = "recharge__charge_line_item_history"
SOURCE_TABLE = "charge_data"

# target column -> source column in charge_data that supplies it
ID_COLUMNS = {
    "charge_id": "ID",
    "customer_id": "CUSTOMER_ID",
    "address_id": "ADDRESS_ID",
}

# DuckDB type names treated as string-like. Anything here fails the evaluator's
# numeric branch when the gold side is an integer.
_STRING_TYPES = {"VARCHAR", "CHAR", "BPCHAR", "TEXT", "STRING", "UUID", "BLOB"}

_INTEGRAL_TYPES = {
    "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT",
    "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT",
}
_NUMERIC_TYPES = _INTEGRAL_TYPES | {"FLOAT", "REAL", "DOUBLE", "DECIMAL", "NUMERIC"}


def _base_type(duckdb_type):
    """'DECIMAL(18,6)' -> 'DECIMAL'; 'VARCHAR' -> 'VARCHAR'."""
    return str(duckdb_type).split("(")[0].strip().upper()


def _category(duckdb_type):
    t = _base_type(duckdb_type)
    if t in _STRING_TYPES:
        return "string"
    if t in _INTEGRAL_TYPES:
        return "integral"
    if t in _NUMERIC_TYPES:
        return "numeric"
    return "other"


def _columns(con, table):
    """{lowercased column name: declared type} for a table in the connected DB."""
    return {r[0].lower(): r[1] for r in con.execute(f'DESCRIBE "{table}"').fetchall()}


def check_recharge001_id_types(produced_db_path):
    """Evaluate the id-column type invariant against a produced DuckDB.

    Returns dict:
      {"status": "pass"|"violation"|"error",
       "violations": [ {column, found_type, expected_type, source_column, why}, ... ],
       "checked_rows": int,          # number of id columns inspected (contract compat)
       "checked_columns": [str, ...],
       "message": str}
    """
    con = duckdb.connect(produced_db_path, read_only=True)
    try:
        tabs = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
        if TARGET_TABLE not in tabs:
            return {"status": "error", "violations": [], "checked_rows": 0,
                    "checked_columns": [],
                    "message": f"target table {TARGET_TABLE} not materialized"}
        if SOURCE_TABLE not in tabs:
            return {"status": "error", "violations": [], "checked_rows": 0,
                    "checked_columns": [],
                    "message": f"source table {SOURCE_TABLE} missing from produced DB "
                               f"(needed to derive the expected id type)"}
        target_cols = _columns(con, TARGET_TABLE)
        source_cols = _columns(con, SOURCE_TABLE)
    finally:
        con.close()

    violations, checked = [], []
    for tgt_col, src_col in ID_COLUMNS.items():
        if tgt_col not in target_cols:
            return {"status": "error", "violations": [], "checked_rows": len(checked),
                    "checked_columns": checked,
                    "message": f"{TARGET_TABLE} has no column {tgt_col!r} "
                               f"(found: {sorted(target_cols)})"}
        if src_col.lower() not in source_cols:
            return {"status": "error", "violations": [], "checked_rows": len(checked),
                    "checked_columns": checked,
                    "message": f"{SOURCE_TABLE} has no column {src_col!r}; "
                               f"cannot derive expected type for {tgt_col}"}

        found_type = _base_type(target_cols[tgt_col])
        expected_type = _base_type(source_cols[src_col.lower()])
        checked.append(tgt_col)

        # Only steer when the source is numeric and the produced column went string.
        # See "DELIBERATELY NOT STRICTER THAN THE EVALUATOR" in the module docstring.
        if _category(expected_type) in ("integral", "numeric") and _category(found_type) == "string":
            violations.append({
                "column": tgt_col,
                "found_type": found_type,
                "expected_type": expected_type,
                "source_column": f"{SOURCE_TABLE}.{src_col}",
                "why": (f"{tgt_col} is {found_type} but {SOURCE_TABLE}.{src_col} is "
                        f"{expected_type}; string ids fail the elementwise column "
                        f"comparison against the integer gold column"),
            })

    if violations:
        return {"status": "violation", "violations": violations,
                "checked_rows": len(checked), "checked_columns": checked,
                "message": f"{len(violations)}/{len(checked)} id columns violate the "
                           f"type invariant ("
                           + ", ".join(f"{v['column']}={v['found_type']}" for v in violations)
                           + ")"}
    return {"status": "pass", "violations": [], "checked_rows": len(checked),
            "checked_columns": checked,
            "message": f"all {len(checked)} id columns satisfy the type invariant"}


def retry_message(verdict):
    """Violation-specific RETRY feedback fed back into the SAME Pi session.

    Names each offending column, its found vs expected type, and states the fix
    explicitly (drop the string cast) -- the observed defect is a copied
    `cast(... as {{ dbt.type_string() }})` idiom, not a missing join.
    """
    cols = "; ".join(
        f"`{v['column']}` is {v['found_type']} but must be {v['expected_type']} "
        f"(as in {v['source_column']})"
        for v in verdict["violations"]
    )
    names = ", ".join(f"`{v['column']}`" for v in verdict["violations"])
    return (
        f"DFC policy violation on {TARGET_TABLE}: the identifier columns must keep the "
        f"integer type of the source columns they come from -> {cols}. "
        f"The row values are correct; only the column type is wrong. "
        f"Fix it by NOT casting these columns to string: remove the "
        f"`cast(... as {{{{ dbt.type_string() }}}})` (or any ::varchar / cast-to-text) "
        f"wrapper from {names} in the model SQL so they are selected as the underlying "
        f"integer values. If a string cast is only needed to make a join key line up, keep "
        f"the cast inside the join condition but select the untouched integer column in the "
        f"final SELECT list. Then rebuild with dbt run and confirm the rebuilt table's "
        f"column types."
    )


if __name__ == "__main__":
    import argparse, json
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("db", help="path to a produced (or gold) recharge.duckdb")
    ap.add_argument("--retry-message", action="store_true",
                    help="also print the RETRY message that would be sent on violation")
    a = ap.parse_args()
    v = check_recharge001_id_types(a.db)
    print(json.dumps(v, indent=2, default=str))
    if a.retry_message and v["status"] == "violation":
        print("\n--- RETRY MESSAGE ---\n" + retry_message(v))
