#!/usr/bin/env python3
"""Read-only DuckDB query helper used by the `duckdb_sql` tool in dbt_harness.ts.

Ported verbatim (2026-09-21) from codeboi07/Self-improving-Harness ext/duckdb_query.py
(commit e587bef). Only this docstring differs.

Usage: duckdb_query.py --db FILE --sql SQL [--output direct|path.csv]
Exit codes: 0 ok, 1 SQL/DuckDB error or blocked statement, 2 database file missing,
64 bad usage.

The connection is opened read-only, but a read-only connection still lets SQL write
outside the database: `ATTACH ... (READ_WRITE)` makes a second database file, `COPY ... TO`
writes any file, `INSTALL`/`LOAD` pulls in an extension, and `PREPARE`/`EXECUTE` runs any
of those behind a statement name. So every request is parsed first and rejected when any
statement type is in DENIED_TYPES. The agent builds tables with dbt, never here.
"""

import argparse
import csv
import os
import sys
from typing import NoReturn

import duckdb
import pandas as pd

MAX_ROWS = 200
MAX_CELL_CHARS = 200

# duckdb.StatementType names that never run through this tool. Names vary by duckdb
# version, so they are matched as plain strings; a name this duckdb does not define
# simply never matches. On duckdb 1.5.5 `INSTALL x` and `LOAD x` both parse as LOAD,
# EXTENSION is unused, and `PREPARE`/`EXECUTE` keep their own names.
DENIED_TYPES = frozenset(
    {
        "ATTACH",
        "DETACH",
        "COPY",
        "COPY_DATABASE",
        "EXPORT",
        "LOAD",
        "EXTENSION",
        "INSERT",
        "UPDATE",
        "DELETE",
        "MERGE_INTO",
        "CREATE",
        "DROP",
        "ALTER",
        "VACUUM",
        "TRANSACTION",
        "PREPARE",
        "EXECUTE",
    }
)

BLOCK_MESSAGE = (
    "Blocked: {kind} statements are not allowed through duckdb_sql "
    "(read-only tool). Use dbt to build tables."
)


def denied_statement_type(sql: str) -> str | None:
    """Name of the first denied statement in `sql`, else None.

    Unparseable SQL returns None on purpose: the normal execution path then reports
    the DuckDB parser error as `SQL error:`, which is the message the agent needs.
    """
    try:
        statements = duckdb.extract_statements(sql)
    except Exception:  # noqa: BLE001 - parser error: not our call to block on
        return None
    for stmt in statements:
        name = getattr(getattr(stmt, "type", None), "name", None)
        if name is None:
            continue  # unknown enum shape on this duckdb build: let it through
        if str(name).upper() in DENIED_TYPES:
            return str(name).upper()
    return None


def denied_by_text(sql: str) -> str | None:
    """Text guard for anything the parser reclassifies. Belt and braces, not primary."""
    upper = sql.upper()
    if "ATTACH " in upper:
        return "ATTACH"
    if upper.lstrip().startswith("COPY ") or " COPY " in upper:
        return "COPY"
    if "EXPORT DATABASE" in upper:
        return "EXPORT"
    return None


class _Parser(argparse.ArgumentParser):
    """ArgumentParser that exits 64 (EX_USAGE), not argparse's own 2.

    Exit 2 stays reserved for a missing database file.
    """

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        sys.exit(64)


def _cell(v: object) -> str:
    """Render one value for the direct table: NULL marker, capped width."""
    if v is None:
        return "NULL"
    s = str(v)
    if len(s) > MAX_CELL_CHARS:
        return s[:MAX_CELL_CHARS] + "\u2026"
    return s


def main() -> int:
    ap = _Parser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--sql", required=True)
    ap.add_argument("--output", default="direct")
    a = ap.parse_args()

    if not os.path.exists(a.db):
        print(f"Database file not found: {a.db}", file=sys.stderr)
        return 2

    kind = denied_statement_type(a.sql) or denied_by_text(a.sql)
    if kind:
        print(BLOCK_MESSAGE.format(kind=kind), file=sys.stderr)
        return 1

    try:
        con = duckdb.connect(a.db, read_only=True)
    except Exception as e:  # noqa: BLE001
        print(f"Could not open {a.db} read-only: {e}", file=sys.stderr)
        return 1

    try:
        cur = con.execute(a.sql)
        cols = [d[0] for d in (cur.description or [])]
        rows = cur.fetchall()
    except Exception as e:  # noqa: BLE001
        print(f"SQL error: {e}", file=sys.stderr)
        con.close()
        return 1
    con.close()

    if a.output == "direct":
        shown = [[_cell(v) for v in row] for row in rows[:MAX_ROWS]]
        if shown:
            df = pd.DataFrame(shown, columns=cols, dtype=object)
            print(df.to_string(index=False))
        else:
            print(" | ".join(cols))
        if len(rows) > MAX_ROWS:
            print(f"\n{len(rows)} rows total, showing first {MAX_ROWS}.")
        else:
            print(f"\n{len(rows)} rows.")
        return 0

    out_dir = os.path.dirname(a.output)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(a.output, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)
    print(f"Saved {len(rows)} rows to {a.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
