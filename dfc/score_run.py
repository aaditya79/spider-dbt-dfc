#!/usr/bin/env python
"""Independent scorer for Phase E runs — does NOT trust the agent's self-report.

For one (experiment_id, instance_id):
  1. Locate the DuckDB the run actually produced (searched under the run output dir).
  2. Load the gold spec (condition_tabs / condition_cols / ignore_orders) from
     evaluation_suite/gold/spider2_eval.jsonl.
  3. Authoritative 0/1 = evaluation_suite.eval_utils.duckdb_match(pred, gold, ...).
  4. PASS integrity: confirm each target table exists in the produced DB and has >0 rows
     (so a silent no-op / empty build can never be counted as success).
  5. FAIL diagnostic: for each checked gold column index, report whether it is matched by
     ANY produced column (duckdb_match's own semantics), plus pred/gold row counts. That is
     the grain / fan-out / temporal-window signal.

Prints a JSON blob to stdout.
"""
import argparse, json, os, sys, glob

DBT_ROOT = os.path.dirname(os.path.abspath(__file__))
EVAL_DIR = os.path.abspath(os.path.join(DBT_ROOT, "../../spider2-dbt/evaluation_suite"))
GOLD_DIR = os.path.join(EVAL_DIR, "gold")
sys.path.insert(0, EVAL_DIR)
import duckdb
# eval_utils imports `from google.cloud import bigquery` at module top for the
# (unused-here) BigQuery path. The trimmed spider2 env has no bigquery. Stub it so
# we can call the REAL, official duckdb_match/compare_pandas_table for scoring.
import types as _types
if "google.cloud.bigquery" not in sys.modules:
    _g = sys.modules.setdefault("google", _types.ModuleType("google"))
    _gc = sys.modules.setdefault("google.cloud", _types.ModuleType("google.cloud"))
    _bq = _types.ModuleType("google.cloud.bigquery")
    sys.modules["google.cloud.bigquery"] = _bq
    setattr(_gc, "bigquery", _bq)
from eval_utils import duckdb_match, compare_pandas_table  # authoritative scorer


def load_gold_spec(instance_id):
    spec = None
    with open(os.path.join(GOLD_DIR, "spider2_eval.jsonl")) as f:
        for line in f:
            o = json.loads(line)
            if o.get("instance_id") == instance_id:
                spec = o
                break
    if spec is None:
        raise SystemExit(f"no gold spec for {instance_id}")
    ev = spec["evaluation"]
    p = ev["parameters"]
    return (ev["func"], p["gold"], p["condition_tabs"], p["condition_cols"], p.get("ignore_orders"))


def find_pred_db(run_out_dir, gold_db_name):
    """Find the produced duckdb (matching the gold db filename) under the run output dir."""
    hits = glob.glob(os.path.join(run_out_dir, "**", gold_db_name), recursive=True)
    # exclude anything inside a gold/ path just in case
    hits = [h for h in hits if "/gold/" not in h]
    hits.sort(key=lambda p: len(p))  # shallowest first
    return hits[0] if hits else None


def col_names(db, tab):
    con = duckdb.connect(db, read_only=True)
    try:
        return [r[1] for r in con.execute(f"PRAGMA table_info('{tab}')").fetchall()]
    finally:
        con.close()


def nrows(db, tab):
    con = duckdb.connect(db, read_only=True)
    try:
        return con.execute(f'SELECT COUNT(*) FROM "{tab}"').fetchone()[0]
    finally:
        con.close()


def get_df(db, tab):
    con = duckdb.connect(db, read_only=True)
    try:
        return con.execute(f'SELECT * FROM "{tab}"').fetchdf()
    finally:
        con.close()


def diagnose(pred_db, gold_db, condition_tabs, condition_cols, ignore_orders):
    """Per-table, per-checked-column matched/unmatched using duckdb_match's own logic."""
    out = []
    for i, tab in enumerate(condition_tabs):
        idxs = condition_cols[i] if condition_cols else []
        ign = ignore_orders[i] if ignore_orders else False
        entry = {"table": tab, "checked_col_indices": idxs}
        try:
            gcols = col_names(gold_db, tab)
            entry["gold_rows"] = nrows(gold_db, tab)
        except Exception as e:
            entry["error"] = f"gold table unreadable: {e}"
            out.append(entry); continue
        try:
            pdf = get_df(pred_db, tab)
            entry["pred_rows"] = len(pdf)
            entry["pred_exists"] = True
        except Exception as e:
            entry["pred_exists"] = False
            entry["error"] = f"pred table missing/unreadable: {e}"
            out.append(entry); continue

        gdf = get_df(gold_db, tab)
        # For each checked gold column, does ANY pred column match it? (duckdb_match semantics)
        matched, unmatched = [], []
        t_pred = pdf.transpose().values.tolist()
        for ci in idxs:
            gname = gcols[ci] if ci < len(gcols) else f"<idx{ci}>"
            gvec = gdf.iloc[:, ci].values.tolist()
            single = compare_pandas_table_single(gvec, t_pred, ign)
            (matched if single else unmatched).append(f"[{ci}]{gname}")
        entry["cols_matched"] = matched
        entry["cols_UNMATCHED"] = unmatched
        out.append(entry)
    return out


def compare_pandas_table_single(gold_vec, t_pred_list, ignore_order):
    """Replicate compare_pandas_table's per-column any-match, for one gold column."""
    import math
    import pandas as pd
    tol = 1e-2
    def vmatch(v1, v2):
        try:
            a1, b1 = v1, v2
            if ignore_order:
                a1 = sorted(v1, key=lambda x: (x is None, str(x), isinstance(x, (int, float))))
                b1 = sorted(v2, key=lambda x: (x is None, str(x), isinstance(x, (int, float))))
            if len(a1) != len(b1):
                return False
            for a, b in zip(a1, b1):
                if pd.isna(a) and pd.isna(b):
                    continue
                elif isinstance(a, (int, float)) and isinstance(b, (int, float)):
                    if not math.isclose(float(a), float(b), abs_tol=tol):
                        return False
                elif a != b:
                    return False
            return True
        except Exception:
            return False
    return any(vmatch(gold_vec, p) for p in t_pred_list)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--experiment_id", required=True)   # e.g. claude-opus-4-8-ecom-e1
    ap.add_argument("--instance_id", required=True)     # e.g. recharge001
    ap.add_argument("--output_dir", default=os.path.join(DBT_ROOT, "output"))
    args = ap.parse_args()

    func, gold_db_name, condition_tabs, condition_cols, ignore_orders = load_gold_spec(args.instance_id)
    gold_db = os.path.join(GOLD_DIR, args.instance_id, gold_db_name)
    run_out_dir = os.path.join(args.output_dir, args.experiment_id, args.instance_id)
    pred_db = find_pred_db(run_out_dir, gold_db_name)

    report = {
        "experiment_id": args.experiment_id, "instance_id": args.instance_id,
        "gold_db": gold_db, "pred_db": pred_db, "func": func,
        "condition_tabs": condition_tabs,
    }
    if pred_db is None:
        report["score"] = 0
        report["verdict"] = "FAIL"
        report["reason"] = f"no produced {gold_db_name} found under {run_out_dir}"
        print(json.dumps(report, indent=2, default=str)); return

    # authoritative score
    score = duckdb_match(pred_db, gold_db, condition_tabs=condition_tabs,
                         condition_cols=condition_cols, ignore_orders=ignore_orders)
    report["score"] = int(score)

    diag = diagnose(pred_db, gold_db, condition_tabs, condition_cols, ignore_orders)
    report["diagnostics"] = diag

    # PASS integrity: every target table must exist with >0 rows
    materialized_ok = all(d.get("pred_exists") and d.get("pred_rows", 0) > 0 for d in diag)
    report["all_targets_materialized_nonempty"] = materialized_ok
    if score == 1 and not materialized_ok:
        report["verdict"] = "SUSPECT_PASS"  # scorer said 1 but a target is empty/missing
    else:
        report["verdict"] = "PASS" if score == 1 else "FAIL"

    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
