#!/usr/bin/env python
"""Summarize N repeat runs, including the DFC policy loop.

Usage: analyze_runs.py <runs_root> <tag> <n> [instance_id]
"""
import json
import os
import sys


def order_data_state(run_dir):
    """Did the agent resolve the order_data blocker in dbt_project.yml?

    There is MORE THAN ONE valid fix. Known routes:
      (a) comment out `order: "{{ ref('order_data') }}"` and uncomment
          `recharge_order_identifier: "order_data"`;
      (b) set `recharge__using_orders: true`, which makes
          stg_recharge__order_tmp.sql take the var('orders') branch so
          ref('order_data') is never evaluated.
    An earlier version of this function only knew about (a) and mislabelled a
    passing run (on-v2-r10) as NOT-FIXED. Check both.
    """
    p = os.path.join(run_dir, "dbt_project.yml")
    if not os.path.exists(p):
        return "no-yml"
    lines = [l.strip() for l in open(p)]
    blocker_active = any(l.startswith("order:") and "ref('order_data')" in l for l in lines)
    identifier_live = any(l.startswith("recharge_order_identifier:") for l in lines)
    using_orders = any(l.startswith("recharge__using_orders:")
                       and l.split(":", 1)[1].strip().lower().startswith("true") for l in lines)
    if using_orders:
        return "FIXED-vars" if blocker_active else "FIXED-both"
    if not blocker_active:
        return "FIXED" if identifier_live else "CHANGED"
    return "NOT-FIXED"


def scan_trajectory(path):
    """Counters across ALL rounds (one trajectory file per run)."""
    out = {"turns": 0, "tool_calls": 0, "dbt_invocations": 0, "cost_usd": 0.0,
           "in_tok": 0, "out_tok": 0, "rounds_seen": 0, "dfc_checks": []}
    if not os.path.exists(path):
        return out
    seen = set()
    with open(path, "rb") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                e = json.loads(raw.decode("utf-8"))
            except Exception:
                continue
            t = e.get("type")
            if t == "_runner_round_start":
                out["rounds_seen"] += 1
            elif t == "_runner_dfc_check":
                out["dfc_checks"].append({"after_round": e.get("after_round"),
                                          "status": e.get("status"),
                                          "message": e.get("message")})
            elif t == "turn_start":
                out["turns"] += 1
            elif t == "tool_execution_start":
                out["tool_calls"] += 1
                if e.get("toolName") == "bash":
                    if "dbt" in str((e.get("args") or {}).get("command", "")):
                        out["dbt_invocations"] += 1
            elif t == "message_end":
                m = e.get("message") or {}
                if m.get("role") != "assistant":
                    continue
                u = m.get("usage") or {}
                k = (m.get("timestamp"), json.dumps(u, sort_keys=True))
                if k in seen:
                    continue
                seen.add(k)
                out["in_tok"] += u.get("input", 0) or 0
                out["out_tok"] += u.get("output", 0) or 0
                out["cost_usd"] += ((u.get("cost") or {}).get("total") or 0.0)
    return out


def checker_fired(row):
    """Did the DFC checker have anything to check?

    It can only evaluate the discount invariant if the agent actually
    materialized the target table. Status 'error' means it never got that far
    (no target table, missing sources, or zero discount rows) -- the RETRY
    cannot trigger because there is no produced data flow to violate.
    Returns (fired: bool, reason: str).
    """
    checks = row.get("dfc_checks") or []
    if not checks:
        return False, "no-policy-or-no-settle"
    first = checks[0]
    if first["status"] in ("violation", "pass"):
        return True, first["status"]
    msg = (first.get("message") or "")
    if "not materialized" in msg:
        return False, "no-target-table"
    if "missing from produced DB" in msg:
        return False, "missing-source-table"
    if "no discount rows" in msg:
        return False, "no-discount-rows"
    return False, f"error: {msg[:40]}"


def classify_dfc(row):
    """How did this policy-on run behave?"""
    checks = row.get("dfc_checks") or []
    if not checks:
        return "no-policy"
    first = checks[0]["status"]
    last = checks[-1]["status"]
    retries = row.get("dfc_retries_used", 0)
    if first == "pass":
        return "passed-first-try"
    if first == "error":
        return f"never-fired({checker_fired(row)[1]})"
    if last == "pass":
        return f"fixed-after-{retries}-retry"
    return f"cap-hit-still-{last}"


def main():
    runs_root, tag, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
    instance = sys.argv[4] if len(sys.argv) > 4 else "recharge001"

    rows = []
    for i in range(1, n + 1):
        exp = f"{tag}-r{i}"
        run_dir = os.path.join(runs_root, exp, instance)
        meta = os.path.join(runs_root, exp, "_pi_meta", instance)
        rec_path = os.path.join(meta, "run_record.json")
        traj = os.path.join(meta, "trajectory.jsonl")
        row = {"run": i, "experiment_id": exp, "run_dir": run_dir, "trajectory": traj}
        if os.path.exists(rec_path):
            rec = json.load(open(rec_path))
            row.update({
                "score": rec.get("score"), "verdict": rec.get("verdict"),
                "wall_s": rec.get("wall_clock_s"), "settled": rec.get("settled"),
                "timed_out": rec.get("timed_out"), "pi_exit": rec.get("pi_exit_code"),
                "dfc_policy": rec.get("dfc_policy"), "n_rounds": rec.get("n_rounds"),
                "dfc_retries_used": rec.get("dfc_retries_used", 0),
                "dfc_events": rec.get("dfc_events") or [],
                "final_text": (rec.get("agent_final_text") or "")[:300],
            })
            sr = rec.get("score_report") or {}
            row["materialized_ok"] = sr.get("all_targets_materialized_nonempty")
            row["diag"] = sr.get("diagnostics") or []
        else:
            row["score"] = None
            row["verdict"] = "NO_RUN_RECORD"
            row["dfc_retries_used"] = 0
        row["order_data"] = order_data_state(run_dir)
        row.update(scan_trajectory(traj))
        row["dfc_class"] = classify_dfc(row)
        row["checker_fired"], row["checker_fired_reason"] = checker_fired(row)
        rows.append(row)

    scored = [r for r in rows if r.get("score") is not None]
    k = sum(1 for r in scored if r["score"] == 1)

    print("=" * 112)
    print(f"{instance}  tag={tag}  |  PASS RATE: {k}/{len(scored)}")
    print("=" * 112)
    hdr = (f"{'run':>3} {'score':>5} {'ord_data':<9} {'mat':<6} {'rnds':>4} {'rtry':>4} "
           f"{'dfc_class':<26} {'turns':>5} {'tools':>5} {'wall_s':>7} {'cost$':>8}")
    print(hdr); print("-" * len(hdr))
    for r in rows:
        print(f"{r['run']:>3} {str(r.get('score')):>5} {r['order_data']:<9} "
              f"{str(r.get('materialized_ok')):<6} {str(r.get('n_rounds')):>4} "
              f"{r.get('dfc_retries_used',0):>4} {r['dfc_class'][:26]:<26} "
              f"{r['turns']:>5} {r['tool_calls']:>5} {str(r.get('wall_s')):>7} "
              f"{r['cost_usd']:>8.4f}")
    tot = sum(r["cost_usd"] for r in rows)
    wall = [r["wall_s"] for r in rows if r.get("wall_s")]
    print()
    print(f"pass rate : {k}/{len(scored)}")
    print(f"cost      : total ${tot:.4f}   mean/run ${tot/max(1,len(rows)):.4f}")
    if wall:
        print(f"wall clock: total {sum(wall):.0f}s   mean/run {sum(wall)/len(wall):.1f}s")
    from collections import Counter
    print(f"dfc classes: {dict(Counter(r['dfc_class'][:26] for r in rows))}")

    policy_runs = [r for r in rows if (r.get("dfc_checks") or [])]
    if policy_runs:
        fired = [r for r in policy_runs if r["checker_fired"]]
        print()
        print(f"CHECKER FIRED (target materialized, invariant evaluable): "
              f"{len(fired)}/{len(policy_runs)}")
        print(f"  never fired: {dict(Counter(r['checker_fired_reason'] for r in policy_runs if not r['checker_fired']))}")
        if fired:
            helped = sum(1 for r in fired if r.get("score") == 1)
            print(f"  of the {len(fired)} that fired: {helped} scored 1, "
                  f"{len(fired)-helped} scored 0")
            print(f"  retry counts among fired: "
                  f"{dict(Counter(r.get('dfc_retries_used',0) for r in fired))}")

    json.dump(rows, open(os.path.join(runs_root, f"{tag}-summary.json"), "w"),
              indent=2, default=str)
    print(f"wrote {os.path.join(runs_root, f'{tag}-summary.json')}")


if __name__ == "__main__":
    main()
