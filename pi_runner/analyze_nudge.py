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
# The first batch (recharge001) predates the family extension and used un-prefixed
# ids; every later task carries its name. Keep both so old records stay readable.
LEGACY_TASK = "recharge001"


def exp_id(task, arm, i):
    return (f"nudge-{arm}-r{i}" if task == LEGACY_TASK
            else f"nudge-{task}-{arm}-r{i}")
NOTICE = "HARNESS NOTICE"
MISSING_NODE = "Did not find matching node for patch"
MODEL_RE = re.compile(r'models/(?:[A-Za-z0-9_]+/)*([A-Za-z0-9_]+)\.sql')
WRITE_TOOLS = ("write", "edit", "str_replace", "create_file")


GOLD_SPEC = os.path.join(SPIDER, "Spider2", "spider2-dbt", "evaluation_suite",
                         "gold", "spider2_eval.jsonl")


def scored_targets(instance_id):
    """The tables duckdb_match actually grades (condition_tabs), from the gold spec.

    Needed because the name-gate passes if ANY declared-but-unbuilt model is built.
    On a multi-target task (shopify002 declares 5, shopify001 6) that is a weaker
    claim than "built the graded table": an agent can satisfy the gate with a
    correctly-named but wrong member of the set. Reporting both keeps the
    wrong-name number from reading as more than it is. Single-target tasks
    (recharge001, recharge002) do not have this gap.
    """
    try:
        with open(GOLD_SPEC) as fh:
            for line in fh:
                o = json.loads(line)
                if o.get("instance_id") == instance_id:
                    return list(o["evaluation"]["parameters"]["condition_tabs"])
    except Exception:
        pass
    return []


def materialized_tables(db_path):
    if not db_path or not os.path.exists(db_path):
        return set()
    try:
        import duckdb
        con = duckdb.connect(db_path, read_only=True)
        try:
            return {r[0] for r in con.execute("SHOW TABLES").fetchall()}
        finally:
            con.close()
    except Exception:
        return set()


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

    scored = scored_targets(instance_id)
    mat = materialized_tables(rec.get("produced_db"))
    r["scored_targets"] = scored
    r["scored_built"] = sorted(t for t in scored if t in mat)
    r["scored_all_built"] = bool(scored) and all(t in mat for t in scored)
    r["multi_target"] = len(targets) > 1

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
    args = [a for a in sys.argv[1:]]
    n = 3
    tasks = []
    for a in args:
        if a.isdigit():
            n = int(a)
        else:
            tasks.append(a)
    if not tasks:
        tasks = ["recharge001", "shopify001", "shopify002", "recharge002"]

    all_rows = {}
    for task in tasks:
        rows = [row(exp_id(task, a, i), task, runs_root)
                for a in ARMS for i in range(1, n + 1)]
        if all(r["validity"] == "MISSING" for r in rows):
            continue
        all_rows[task] = rows

    for task, rows in all_rows.items():
        tgts = declared_unshipped(task)
        print("#" * 100)
        print(f"TASK {task}   declared-unbuilt targets: {len(tgts)}  {tgts}")
        print("#" * 100)
        hdr = (f"{'run':<32}{'valid':<9}{'wrongname':<11}{'tgtbuilt':<10}"
               f"{'SCORED':<8}{'wrongW':<8}{'tgtW':<6}{'notice':<8}{'trap':<6}"
               f"{'conv':<6}{'mnWarn':<8}{'score':<6}")
        print(hdr); print("-" * len(hdr))
        for r in rows:
            if r["validity"] == "MISSING":
                print(f"{r['exp']:<32}{'MISSING':<9}"); continue
            v = "yes" if r["validity"] == "VALID" else r["validity"]
            print(f"{r['exp']:<32}{v:<9}{str(r['wrong_name']):<11}"
                  f"{str(r['target_built']):<10}{str(r.get('scored_all_built')):<8}"
                  f"{str(r['first_wrong']):<8}"
                  f"{str(r['first_target']):<6}{str(r['first_notice']):<8}"
                  f"{str(r['entered_trap']):<6}{str(r['converted']):<6}"
                  f"{r['n_missing_node_warn']:<8}{str(r['score']):<6}")
        print()
        arm_table(rows, n)
        baseline_signal(rows)
        overfit_flag(task, rows)
        print()

    if len(all_rows) > 1:
        print("=" * 100)
        print("POOLED ACROSS TASKS")
        print("=" * 100)
        pooled = [r for rows in all_rows.values() for r in rows]
        arm_table(pooled, n * len(all_rows))
        baseline_signal(pooled)

    print("\nCAUSALITY: a conversion requires the wrong name written BEFORE the first "
          "notice and\nthe declared target AFTER it. Runs that never entered the trap "
          "are not credited.\nconv/trap and conv/trials are reported separately because "
          "the arms are not equally powered.")
    print("SCOPE: naming only. dbt emits no signal for content defects, so official pass "
          "is\nexpected to stay low and is not the quantity this intervention targets.")

    with open(os.path.join(runs_root, "nudge_family_report.json"), "w") as fh:
        json.dump(all_rows, fh, indent=2)
    print(f"\nrows -> {os.path.join(runs_root, 'nudge_family_report.json')}")


def arm_table(rows, n_per_arm):
    print(f"{'arm':<11}{'valid':<9}{'VOID':<7}{'wrong-name':<16}"
          f"{'SCORED built':<14}{'trap':<8}"
          f"{'conv/trap':<12}{'conv/trials':<13}{'official pass'}")
    for a in ARMS:
        ar = [r for r in rows if f"-{a}-r" in r["exp"]]
        v = [r for r in ar if r["validity"] == "VALID"]
        void = [r for r in ar if r["validity"] not in ("VALID", "MISSING")]
        if not v:
            print(f"{a:<11}{'0':<9}{len(void):<7}NO VALID TRIALS")
            continue
        wn = sum(1 for r in v if r["wrong_name"])
        tr = sum(1 for r in v if r["entered_trap"])
        cv = sum(1 for r in v if r["converted"])
        ps = sum(1 for r in v if r["score"] == 1)
        sc = sum(1 for r in v if r.get("scored_all_built"))
        print(f"{a:<11}{f'{len(v)}/{len(ar)}':<9}{len(void):<7}"
              f"{f'{wn}/{len(v)} ({wn/len(v):.0%})':<16}"
              f"{f'{sc}/{len(v)}':<14}{f'{tr}/{len(v)}':<8}"
              f"{(f'{cv}/{tr}' if tr else 'n/a'):<12}"
              f"{f'{cv}/{len(v)}':<13}{ps}/{len(v)}")


def baseline_signal(rows):
    """Did dbt's OWN missing-node warning appear in each baseline run, and convert?"""
    base = [r for r in rows if "-off-r" in r["exp"] and r["validity"] == "VALID"]
    if not base:
        print("  baseline: no valid trials"); return
    with_warn = [r for r in base if r["n_missing_node_warn"] > 0]
    conv = [r for r in with_warn if r["converted"]]
    print(f"  baseline control: dbt's own 'Did not find matching node' warning appeared "
          f"in {len(with_warn)}/{len(base)} baseline runs; "
          f"it converted {len(conv)}/{len(with_warn) if with_warn else 0}")


def overfit_flag(task, rows):
    def arm(a):
        v = [r for r in rows if f"-{a}-r" in r["exp"] and r["validity"] == "VALID"]
        if not v: return None
        tr = sum(1 for r in v if r["entered_trap"])
        cv = sum(1 for r in v if r["converted"])
        wn = sum(1 for r in v if r["wrong_name"])
        return {"n": len(v), "trap": tr, "conv": cv, "wrong": wn,
                "rate": (cv / tr) if tr else None, "wrong_rate": wn / len(v)}
    g, sp = arm("generic"), arm("specific")
    if not g or not sp:
        return
    base = [r for r in rows if "-off-r" in r["exp"] and r["validity"] == "VALID"]
    total_trap = sp["trap"] + g["trap"] + sum(1 for r in base if r["entered_trap"])
    if total_trap == 0:
        print(f"  (no run in ANY arm entered the wrong-name trap on {task} -- this task "
              f"cannot\n   discriminate between the arms; specific-vs-generic is not "
              f"reported for it)")
        return
    worse = []
    if g["rate"] is not None and sp["rate"] is not None and sp["rate"] < g["rate"]:
        worse.append(f"conversion {sp['conv']}/{sp['trap']} vs generic {g['conv']}/{g['trap']}")
    if sp["wrong_rate"] > g["wrong_rate"]:
        worse.append(f"wrong-name {sp['wrong']}/{sp['n']} vs generic {g['wrong']}/{g['n']}")
    if worse:
        print(f"  !! OVERFITTING SIGNAL on {task}: SPECIFIC does worse than GENERIC -- "
              + "; ".join(worse))
    else:
        print(f"  specific >= generic on {task} (no overfitting signal)")


if __name__ == "__main__":
    main()
