#!/usr/bin/env python
"""Aggregate Phase E: per-run audit sidecar + results table.

For each (model, task): re-score independently (duckdb_match), read steps/finished from
result.json, attach the EXACT inference-profile ARN + sampling_metadata (auditable, per the
Haiku us./global. requirement), verify the harbor trajectory exists, and write
<run>/audit_metadata.json. Prints a summary table.
"""
import json, os, subprocess, sys

DBT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, DBT_ROOT)
from spider_agent.agent.bedrock_llm import BEDROCK_MODELS, sampling_metadata

# Inference-profile IDs are read from the environment with NO literal fallback:
# the ARN embeds the AWS account number, so it must never live in the source.
# Missing env -> KeyError at import, which is the intended loud failure.
ARNS = {
    "bedrock/claude-opus-4-8": os.environ["BEDROCK_OPUS_ARN"],
    "bedrock/claude-haiku-4-5": os.environ["BEDROCK_HAIKU_ARN"],
}
SUFFIX = os.environ.get("SUFFIX", "ecom-e1")
MAXSTEPS = int(os.environ.get("MAXSTEPS", "30"))
MODELS = [("bedrock/claude-opus-4-8", f"claude-opus-4-8-{SUFFIX}"),
          ("bedrock/claude-haiku-4-5", f"claude-haiku-4-5-{SUFFIX}")]
TASKS = ["shopify001", "recharge001", "recharge002"]


def termination(finished, steps):
    if finished is True:
        return "clean-terminate"
    if steps is not None and steps >= MAXSTEPS:
        return "hit-cap"
    return "error/other"


def score(exp, inst):
    out = subprocess.run([sys.executable, os.path.join(DBT_ROOT, "score_run.py"),
                          "--experiment_id", exp, "--instance_id", inst],
                         capture_output=True, text=True)
    try:
        return json.loads(out.stdout)
    except Exception:
        return {"score": 0, "verdict": "ERROR", "diagnostics": [], "_stderr": out.stderr[-300:]}


rows = []
for model, exp in MODELS:
    for inst in TASKS:
        run_dir = os.path.join(DBT_ROOT, "output", exp, inst)
        rj = os.path.join(run_dir, "spider", "result.json")
        steps = finished = None
        if os.path.exists(rj):
            d = json.load(open(rj))
            steps, finished = d.get("steps"), d.get("finished")
        sc = score(exp, inst)
        harbor = os.path.join(run_dir, "agent", "spider-agent-dbt.trajectory.json")
        audit = {
            "model_flag": model,
            "inference_profile_arn": ARNS[model],
            "region": BEDROCK_MODELS[model]["region"],
            "sampling": sampling_metadata(model, 0.0),
            "max_steps": MAXSTEPS, "steps_taken": steps, "agent_self_reported_finished": finished,
            "termination": termination(finished, steps),
            "evaluator": "duckdb_match (official eval_utils)",
            "score": sc.get("score"), "verdict": sc.get("verdict"),
            "all_targets_materialized_nonempty": sc.get("all_targets_materialized_nonempty"),
            "diagnostics": sc.get("diagnostics"),
            "harbor_trajectory_present": os.path.exists(harbor),
        }
        if os.path.isdir(run_dir):
            with open(os.path.join(run_dir, "audit_metadata.json"), "w") as f:
                json.dump(audit, f, indent=2, default=str)
        # short per-table note
        notes = []
        for x in sc.get("diagnostics", []):
            if x.get("pred_rows") is None:
                notes.append(f"{x['table']}: NOT BUILT")
            elif x.get("cols_UNMATCHED"):
                notes.append(f"{x['table']}: {x.get('pred_rows')}/{x.get('gold_rows')} rows, {len(x['cols_UNMATCHED'])} col(s) off")
            else:
                notes.append(f"{x['table']}: MATCH")
        rows.append((model.split('/')[-1], inst, sc.get("score"), steps,
                     termination(finished, steps), audit["harbor_trajectory_present"],
                     "; ".join(notes)))

print(f"\nPhase E rerun: SUFFIX={SUFFIX}  MAXSTEPS={MAXSTEPS}")
print(f"{'model':<17}{'task':<13}{'score':<6}{'steps':<6}{'termination':<17}{'harbor':<7}note")
print("-" * 120)
for r in rows:
    print(f"{r[0]:<17}{r[1]:<13}{str(r[2]):<6}{str(r[3]):<6}{r[4]:<17}{str(r[5]):<7}{r[6]}")
print(f"\nPASS total: {sum(1 for r in rows if r[2]==1)}/6")
