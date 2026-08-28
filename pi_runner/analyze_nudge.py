#!/usr/bin/env python3
"""Report the harness-feedback (nudge) experiment.

Three arms x N independent trials, recharge001 + Opus, all --dfc-policy off.
The only variable is --harness-nudge {off,generic,specific}.

Everything here is read-only measurement, after the fact. No steering happened.

Per trial:
  validity   -- nudge_validity.py. Only VALID counts as evidence.
  wrong-name -- the name-gate's own violation condition (dfc_check_namegate),
                applied as a MEASUREMENT, not as steering.
  causality  -- ordering matters, so we record call indices:
                  first_yml     first touch of a models/**/*.yml
                  first_wrong   first write of an undeclared model name
                  first_target  first write of the declared target
                  first_notice  first dbt output carrying a HARNESS NOTICE
                entered_trap := wrote an undeclared name BEFORE any notice
                converted    := entered_trap AND wrote the target AFTER the notice
                A run that named correctly before any notice fired is NOT a
                conversion -- the nudge cannot get credit for it.

SCOPE: naming only. dbt emits no signal for recharge001's content defect
([6]amount), so a name-correct run can still score 0. Official score is reported
but is not the quantity this intervention targets.
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SPIDER = os.path.dirname(HERE)
sys.path.insert(0, HERE)
# Canonical checker location -- the one pi_runner/run_task.py imports (METHODS_DBT).
# The dfc/spider_agent/agent/ mirror sits at a different depth, so its EXAMPLES
# constant resolves to a path that does not exist; do not import the mirror here.
sys.path.insert(0, os.path.join(SPIDER, "Spider2", "methods", "spider-agent-dbt"))

from nudge_validity import classify                                  # noqa: E402
from spider_agent.agent import dfc_check_namegate as ng              # noqa: E402

ARMS = ("off", "generic", "specific")
NOTICE = "HARNESS NOTICE"
MISSING_NODE = "Did not find matching node for patch"
MODEL_RE = re.compile(r'models/(?:[A-Za-z0-9_]+/)*([A-Za-z0-9_]+)\.sql')
WRITE_TOOLS = ("write", "edit", "str_replace", "create_file")


def declared_unshipped(instance_id):
    """Declared-but-no-SQL models in the pristine fixture (the name-gate's candidates)."""
    fixture = os.path.join(ng.EXAMPLES, instance_id)
    return sorted(ng._declared_models(fixture) - ng._sql_models(fixture))


def scan_trajectory(path, targets, declared):
    """Ordering-aware scan. Returns call indices and derived causal flags."""
    out = {"n_tool_calls": 0, "n_notice": 0, "n_missing_node_warn": 0,
           "first_yml": None, "first_wrong": None, "first_target": None,
           "first_notice": None, "wrong_names": [], "reread_yml_after_notice": 0}
    if not path or not os.path.isfile(path):
        return out
    wrong = []
    idx = 0
    with open(path, errors="replace") as fh:
        for line in fh:
            if '"tool_execution_' not in line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            t = e.get("type")
            if t == "tool_execution_start":
                tn = (e.get("toolName") or "").lower()
                args = json.dumps(e.get("args") or {})
                if ".yml" in args and "models" in args:
                    if out["first_yml"] is None:
                        out["first_yml"] = idx
                    if out["first_notice"] is not None:
                        out["reread_yml_after_notice"] += 1
                if tn in WRITE_TOOLS:
                    for m in MODEL_RE.finditer(args):
                        name = m.group(1)
                        if name.startswith("stg_") or name in declared and name not in targets:
                            continue          # pre-existing fixture models
                        if name in targets:
                            if out["first_target"] is None:
                                out["first_target"] = idx
                        else:
                            wrong.append(name)
                            if out["first_wrong"] is None:
                                out["first_wrong"] = idx
                idx += 1
            elif t == "tool_execution_end":
                txt = json.dumps(e.get("result") or {})
                if NOTICE in txt:
                    out["n_notice"] += 1
                    if out["first_notice"] is None:
                        out["first_notice"] = idx - 1
                if MISSING_NODE in txt:
                    out["n_missing_node_warn"] += 1
    out["n_tool_calls"] = idx
    out["wrong_names"] = sorted(set(wrong))
    return out


def row(exp, instance_id, runs_root):
    out_json = os.path.join(runs_root, f"{exp}.stdout.json")
    status, detail = classify(out_json)
    r = {"exp": exp, "validity": status, "score": detail.get("score"),
         "tokens": detail.get("tokens"), "tools": detail.get("tools"),
         "wall_s": detail.get("wall_s"), "net": detail.get("net_error")}
    if status == "MISSING":
        return r
    rec = json.load(open(out_json))
    fixture = os.path.join(ng.EXAMPLES, instance_id)
    targets = declared_unshipped(instance_id)
    declared = ng._declared_models(fixture)

    try:
        gate = ng.check_namegate(rec.get("produced_db"))
    except Exception as e:
        gate = {"status": "error", "message": f"{type(e).__name__}: {e}"}
    r["gate_status"] = gate.get("status")
    r["undeclared_built"] = gate.get("undeclared_built") or []
    r["built_candidates"] = gate.get("built_candidates") or []
    r["wrong_name"] = (gate.get("status") == "violation")
    r["target_built"] = bool(r["built_candidates"])

    s = scan_trajectory(rec.get("trajectory"), set(targets), declared)
    r.update(s)
    fn, ft, fw = s["first_notice"], s["first_target"], s["first_wrong"]
    r["nudge_fired"] = s["n_notice"] > 0
    r["entered_trap"] = fw is not None and (fn is None or fw < fn)
    r["converted"] = bool(r["entered_trap"] and fn is not None
                          and ft is not None and ft > fn)
    r["correct_before_notice"] = bool(ft is not None and (fn is None or ft < fn))
    return r


def main():
    runs_root = os.path.join(SPIDER, "runs", "pi")
    instance_id = "recharge001"
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    rows = [row(f"nudge-{a}-r{i}", instance_id, runs_root)
            for a in ARMS for i in range(1, n + 1)]

    print(f"task={instance_id}  model=opus  declared-but-unshipped target(s): "
          f"{declared_unshipped(instance_id)}\n")
    hdr = (f"{'run':<19}{'valid':<7}{'wrongname':<11}{'tgtbuilt':<10}{'yml':<5}"
           f"{'wrongW':<8}{'tgtW':<6}{'notice':<8}{'trap':<6}{'conv':<6}"
           f"{'mnWarn':<8}{'score':<6}")
    print(hdr); print("-" * len(hdr))
    for r in rows:
        if r["validity"] == "MISSING":
            print(f"{r['exp']:<19}{'MISSING':<7}"); continue
        v = "yes" if r["validity"] == "VALID" else r["validity"]
        print(f"{r['exp']:<19}{v:<7}{str(r['wrong_name']):<11}{str(r['target_built']):<10}"
              f"{str(r['first_yml']):<5}{str(r['first_wrong']):<8}{str(r['first_target']):<6}"
              f"{str(r['first_notice']):<8}{str(r['entered_trap']):<6}{str(r['converted']):<6}"
              f"{r['n_missing_node_warn']:<8}{str(r['score']):<6}")

    print("\n" + "=" * 92)
    print("PER-ARM SUMMARY (valid trials only)")
    print("=" * 92)
    print(f"{'arm':<11}{'valid/n':<9}{'wrong-name surviving':<22}{'entered trap':<14}"
          f"{'converted / trap':<18}{'official pass'}")
    for a in ARMS:
        ar = [r for r in rows if r["exp"].startswith(f"nudge-{a}-")]
        v = [r for r in ar if r["validity"] == "VALID"]
        if not v:
            print(f"{a:<11}{f'0/{len(ar)}':<9}NO VALID TRIALS -- cannot report"); continue
        wn = sum(1 for r in v if r["wrong_name"])
        tr = sum(1 for r in v if r["entered_trap"])
        cv = sum(1 for r in v if r["converted"])
        ps = sum(1 for r in v if r["score"] == 1)
        conv = f"{cv}/{tr}" if tr else "n/a (never entered)"
        print(f"{a:<11}{f'{len(v)}/{len(ar)}':<9}"
              f"{f'{wn}/{len(v)} ({wn/len(v):.0%})':<22}{f'{tr}/{len(v)}':<14}"
              f"{conv:<18}{ps}/{len(v)}")

    short = [a for a in ARMS
             if sum(1 for r in rows
                    if r["exp"].startswith(f"nudge-{a}-") and r["validity"] == "VALID") < n]
    print(f"\n!! ARMS WITH FEWER THAN {n} VALID TRIALS: {', '.join(short)}" if short
          else f"\nAll arms have {n}/{n} valid trials.")
    print("\nCAUSALITY: 'converted' requires the wrong name to be written BEFORE the first "
          "notice\nand the declared target AFTER it. Runs that named correctly before any "
          "notice fired\nare counted as 'correct_before_notice', NOT as conversions.")
    print("SCOPE: naming only. dbt emits no signal for the content defect ([6]amount), so "
          "name-correct\nruns can still score 0.")

    with open(os.path.join(runs_root, "nudge_report.json"), "w") as fh:
        json.dump(rows, fh, indent=2)
    print(f"\nrows -> {os.path.join(runs_root, 'nudge_report.json')}")


if __name__ == "__main__":
    main()
