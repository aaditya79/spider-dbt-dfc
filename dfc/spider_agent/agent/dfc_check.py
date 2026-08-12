#!/usr/bin/env python
"""DFC post-materialization policy checker — recharge001 ONLY (hardcoded, scoped proof).

Invariant (approved in Phase 1, grounded in the reference model
examples/recharge002/models/recharge__charge_line_item_history.sql and the real
start DuckDB): for every materialized row with line_item_type = 'discount',
joined back to its source charge_discount_data row and charge_data:

    if VALUE_TYPE = 'percentage':  amount == round(VALUE/100 * charge_data.TOTAL_LINE_ITEMS_PRICE, 2)
    else (fixed_amount / other):   amount == VALUE

Compared with tolerance 1e-2 to mirror the official duckdb_match evaluator (NOT stricter).

This checker STEERS the agent (feeds a retry message on violation). It NEVER reports the
final pass/fail — that comes from duckdb_match. See check_recharge001_discounts().
"""
import duckdb

TARGET_TABLE = "recharge__charge_line_item_history"
TOL = 1e-2  # mirror duckdb_match tolerance; do not tighten


def check_recharge001_discounts(produced_db_path):
    """Evaluate the discount-amount invariant against a produced DuckDB.

    Returns dict:
      {"status": "pass"|"violation"|"error",
       "violations": [ {charge_id, title, value_type, amount_found, amount_expected, why}, ... ],
       "checked_rows": int, "message": str}
    The agent's dbt run leaves the raw source tables (charge_discount_data / charge_data)
    in the same DuckDB alongside the materialized target, so we can join back to source.
    """
    con = duckdb.connect(produced_db_path, read_only=True)
    try:
        # target must exist and be materialized
        tabs = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
        if TARGET_TABLE not in tabs:
            return {"status": "error", "violations": [], "checked_rows": 0,
                    "message": f"target table {TARGET_TABLE} not materialized"}
        for src in ("charge_discount_data", "charge_data"):
            if src not in tabs:
                return {"status": "error", "violations": [], "checked_rows": 0,
                        "message": f"source table {src} missing from produced DB"}

        # Join materialized discount rows back to source.
        # Join key: charge_id = CHARGE_ID and title = CODE.
        #   -> title=CODE is unique per charge in the recharge001 fixture (one discount/charge).
        #      If this ever generalizes to multiple discounts per charge sharing a CODE, switch
        #      the join to (charge_id, source INDEX) instead.
        # charge_data.ID = CHARGE_ID supplies TOTAL_LINE_ITEMS_PRICE (the % base, Phase-1 confirmed).
        rows = con.execute(f"""
            SELECT
                t.charge_id                              AS charge_id,
                t.title                                  AS title,
                lower(d.VALUE_TYPE)                      AS value_type,
                CAST(t.amount AS DOUBLE)                 AS amount_found,
                CAST(d.VALUE AS DOUBLE)                  AS raw_value,
                CAST(ch.TOTAL_LINE_ITEMS_PRICE AS DOUBLE) AS total_line_items_price
            FROM {TARGET_TABLE} t
            JOIN charge_discount_data d
              ON CAST(t.charge_id AS VARCHAR) = CAST(d.CHARGE_ID AS VARCHAR)
             AND t.title = d.CODE
            LEFT JOIN charge_data ch
              ON CAST(ch.ID AS VARCHAR) = CAST(d.CHARGE_ID AS VARCHAR)
            WHERE t.line_item_type = 'discount'
            ORDER BY t.charge_id
        """).fetchall()
    finally:
        con.close()

    violations = []
    for charge_id, title, value_type, amount_found, raw_value, tlip in rows:
        if value_type == "percentage":
            amount_expected = round(raw_value / 100.0 * tlip, 2)
            basis = f"round({raw_value}/100 * {tlip}, 2)"
        else:
            # fixed_amount branch — from the reference SQL's `else` case.
            # UNTESTED AGAINST DATA: the recharge001 fixture is percentage-only, so this
            # branch has not been exercised by real rows. Do not treat as data-confirmed.
            amount_expected = raw_value
            basis = f"raw value {raw_value} (fixed_amount; untested against data)"
        if abs(float(amount_found) - float(amount_expected)) > TOL:
            violations.append({
                "charge_id": charge_id, "title": title, "value_type": value_type,
                "amount_found": round(float(amount_found), 4),
                "amount_expected": round(float(amount_expected), 2),
                "why": f"expected {basis} = {amount_expected}, found {amount_found}",
            })

    if not rows:
        return {"status": "error", "violations": [], "checked_rows": 0,
                "message": "no discount rows found to check (join produced 0 rows)"}
    if violations:
        return {"status": "violation", "violations": violations, "checked_rows": len(rows),
                "message": f"{len(violations)}/{len(rows)} discount rows violate the amount invariant"}
    return {"status": "pass", "violations": [], "checked_rows": len(rows),
            "message": f"all {len(rows)} discount rows satisfy the amount invariant"}


if __name__ == "__main__":
    import argparse, json
    ap = argparse.ArgumentParser()
    ap.add_argument("db", help="path to a produced (or gold) recharge.duckdb")
    a = ap.parse_args()
    print(json.dumps(check_recharge001_discounts(a.db), indent=2, default=str))
