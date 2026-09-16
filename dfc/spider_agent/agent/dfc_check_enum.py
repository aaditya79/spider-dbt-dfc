#!/usr/bin/env python
"""DFC post-materialization policy checker #3 -- recharge001 line_item_type ENUM invariant.

Invariant (grounded in the project's own spec file, models/recharge.yml):
every row of recharge__charge_line_item_history must carry a line_item_type drawn
from the domain that recharge.yml declares for that column:

    - name: line_item_type
      description:
        The type of the line item. Possible values include:'charge line' (product or
        service being charged), 'discount' (discount applied), 'shipping' (shipping
        costs), 'tax' (taxes applied), and 'refund' (refund applied).

    -> {'charge line', 'discount', 'shipping', 'tax', 'refund'}

The domain is hardcoded below (same scoped-proof style as dfc_check.py) but is
verifiable against the yml at any time via domain_from_yml() / the CLI's
--verify-domain flag, so the constant cannot silently drift from the spec.

WHY THIS MATTERS (the failure it catches): the evaluator compares this column
elementwise with exact string equality (eval_utils.py::compare_pandas_table falls
through to `elif a != b` for strings), so a single mis-spelled token unmatches the
whole column.

Observed failures, both from agents that HAD read recharge.yml first:
  * 'charge_line'  -- snake_cased the one domain value containing a space. This
                      token appears nowhere in the fixture; it was invented.
  * 'line_item'    -- borrowed from the sibling model
                      models/standardized_models/recharge__line_item_enhanced.sql,
                      which emits `'line_item' as record_type` for a DIFFERENT column.
In both cases the other four values ('discount', 'shipping', 'tax', 'refund') were
correct, so this is a single-token transcription slip, not a missing-information
failure.

MEMBERSHIP ONLY, NOT COVERAGE
-----------------------------
The check requires every emitted value to be IN the domain. It does not require all
five values to be present: the recharge001 fixture produces no refund rows, and the
gold table has none either. Requiring coverage would steer the agent to invent rows.

KNOWN, DELIBERATE DIVERGENCE FROM duckdb_match (1 of 48 saved runs)
-------------------------------------------------------------------
This checker is intentionally STRICTER than the scorer in one observed shape. In
haiku-off-r10 the agent emitted two type-ish columns: `line_item_type` holding
('type1','discount','tax','shipping','type2') and a second column
`line_item_category` holding the correct domain values. duckdb_match did NOT mark
line_item_type unmatched, because compare_pandas_table matches each gold column
against ANY predicted column -- the gold vector found its match in the sibling
`line_item_category`, so the genuinely wrong column was masked.

We flag it anyway. Mirroring the scorer's any-column leniency would mean declining
to report a column that literally violates the declared domain, and would bake an
evaluator quirk into a policy whose job is to encode the SPEC. The consequence is
accepted and bounded: in this shape the checker can fire a retry the scorer did not
strictly require. That retry steers toward correctness (fixing the actually-wrong
column), and it cannot change the reported result -- the final 0/1 still comes from
score_run.py/duckdb_match, never from this checker.

This checker STEERS the agent (feeds a retry message on violation). It NEVER reports
the final pass/fail -- that comes from duckdb_match. See check_recharge001_line_item_type().
"""
import os
import re

import duckdb

TARGET_TABLE = "recharge__charge_line_item_history"
COLUMN = "line_item_type"

# Domain declared by models/recharge.yml for line_item_type. Verify with domain_from_yml().
DOMAIN = ("charge line", "discount", "shipping", "tax", "refund")

# Near-miss -> correct value, used to make the RETRY message concrete about the fix.
# Keyed on the exact tokens observed in real runs plus their obvious variants.
_KNOWN_CONFUSIONS = {
    "charge_line": "charge line",
    "chargeline": "charge line",
    "charge-line": "charge line",
    "line_item": "charge line",
    "line item": "charge line",
    "lineitem": "charge line",
    "charge_line_item": "charge line",
    "product": "charge line",
    "refunds": "refund",
    "taxes": "tax",
    "discounts": "discount",
}


def domain_from_yml(models_dir_or_yml):
    """Re-derive the declared domain from models/recharge.yml.

    Accepts either the path to recharge.yml or a directory containing it. Returns a
    tuple of the quoted values in the line_item_type column's description, or None if
    the block cannot be located. Used to prove DOMAIN matches the spec; the checker
    itself does not depend on the file being present at run time.
    """
    path = models_dir_or_yml
    if os.path.isdir(path):
        path = os.path.join(path, "recharge.yml")
    if not os.path.isfile(path):
        return None
    with open(path) as f:
        text = f.read()

    m = re.search(r"-\s*name:\s*%s\b" % re.escape(COLUMN), text)
    if not m:
        return None
    block = text[m.end():]
    nxt = re.search(r"\n\s*-\s*name:\s", block)
    if nxt:
        block = block[: nxt.start()]

    values = re.findall(r"'([^']+)'", block)
    return tuple(values) or None


def check_recharge001_line_item_type(produced_db_path):
    """Evaluate the line_item_type domain invariant against a produced DuckDB.

    Returns dict:
      {"status": "pass"|"violation"|"error",
       "violations": [ {value, row_count, suggestion, why}, ... ],
       "checked_rows": int,
       "distinct_values": [str, ...],
       "domain": [str, ...],
       "message": str}
    """
    con = duckdb.connect(produced_db_path, read_only=True)
    try:
        tabs = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
        if TARGET_TABLE not in tabs:
            return {"status": "error", "violations": [], "checked_rows": 0,
                    "distinct_values": [], "domain": list(DOMAIN),
                    "message": f"target table {TARGET_TABLE} not materialized"}

        cols = {r[0].lower() for r in con.execute(f'DESCRIBE "{TARGET_TABLE}"').fetchall()}
        if COLUMN not in cols:
            return {"status": "error", "violations": [], "checked_rows": 0,
                    "distinct_values": [], "domain": list(DOMAIN),
                    "message": f"{TARGET_TABLE} has no column {COLUMN!r} "
                               f"(found: {sorted(cols)})"}

        rows = con.execute(f"""
            SELECT CAST({COLUMN} AS VARCHAR) AS value, COUNT(*) AS n
            FROM "{TARGET_TABLE}"
            GROUP BY 1
            ORDER BY 1
        """).fetchall()
    finally:
        con.close()

    total = sum(n for _, n in rows)
    if total == 0:
        return {"status": "error", "violations": [], "checked_rows": 0,
                "distinct_values": [], "domain": list(DOMAIN),
                "message": f"{TARGET_TABLE} is empty; no {COLUMN} values to check"}

    distinct = [v for v, _ in rows]
    violations = []
    for value, n in rows:
        if value in DOMAIN:
            continue
        key = (value or "").strip().lower()
        suggestion = _KNOWN_CONFUSIONS.get(key)
        if suggestion is None and key.replace("_", " ") in DOMAIN:
            suggestion = key.replace("_", " ")
        violations.append({
            "value": value,
            "row_count": n,
            "suggestion": suggestion,
            "why": (f"{value!r} is not in the {COLUMN} domain declared by "
                    f"models/recharge.yml {list(DOMAIN)}"
                    + (f"; expected {suggestion!r}" if suggestion else "")),
        })

    if violations:
        bad_rows = sum(v["row_count"] for v in violations)
        return {"status": "violation", "violations": violations, "checked_rows": total,
                "distinct_values": distinct, "domain": list(DOMAIN),
                "message": f"{bad_rows}/{total} rows carry a {COLUMN} outside the "
                           f"declared domain ("
                           + ", ".join(repr(v["value"]) for v in violations) + ")"}
    return {"status": "pass", "violations": [], "checked_rows": total,
            "distinct_values": distinct, "domain": list(DOMAIN),
            "message": f"all {total} rows carry a {COLUMN} inside the declared domain"}


def retry_message(verdict):
    """Violation-specific RETRY feedback fed back into the SAME Pi session.

    Names the offending value, the exact replacement, and the full declared domain
    with its source file, since the observed defect is a token slip against a spec
    the agent already read.
    """
    parts = []
    for v in verdict["violations"]:
        if v["suggestion"]:
            parts.append(f"{v['value']!r} ({v['row_count']} rows) must be "
                         f"{v['suggestion']!r}")
        else:
            parts.append(f"{v['value']!r} ({v['row_count']} rows) is not a declared value")
    bad = "; ".join(parts)
    domain = ", ".join(repr(d) for d in DOMAIN)
    return (
        f"DFC policy violation on {TARGET_TABLE}: the `{COLUMN}` column contains values "
        f"outside the domain declared for it in models/recharge.yml -> {bad}. "
        f"The declared domain is exactly: {domain}. Note that 'charge line' is two words "
        f"separated by a space -- it is not 'charge_line', and it is not 'line_item' "
        f"(that token belongs to `record_type` in the unrelated model "
        f"recharge__line_item_enhanced.sql). These are exact string literals and are "
        f"compared verbatim, so re-read the `{COLUMN}` description in models/recharge.yml "
        f"and use its values character-for-character in the model SQL. Then rebuild with "
        f"dbt run and confirm the rebuilt table's distinct {COLUMN} values."
    )


if __name__ == "__main__":
    import argparse, json
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("db", help="path to a produced (or gold) recharge.duckdb")
    ap.add_argument("--retry-message", action="store_true",
                    help="also print the RETRY message that would be sent on violation")
    ap.add_argument("--verify-domain", metavar="MODELS_DIR_OR_YML",
                    help="re-derive the domain from models/recharge.yml and compare to DOMAIN")
    a = ap.parse_args()

    if a.verify_domain:
        derived = domain_from_yml(a.verify_domain)
        print(json.dumps({"hardcoded": list(DOMAIN), "from_yml": list(derived) if derived else None,
                          "match": derived is not None and set(derived) == set(DOMAIN)}, indent=2))

    v = check_recharge001_line_item_type(a.db)
    print(json.dumps(v, indent=2, default=str))
    if a.retry_message and v["status"] == "violation":
        print("\n--- RETRY MESSAGE ---\n" + retry_message(v))
