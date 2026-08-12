"""Emit a harbor parity-experiments conforming trajectory JSON.

The team's GPT-5.5 runs (and our Opus/Haiku runs, for parity) store trajectories
in the schema used by the harborframework/parity-experiments dataset:

    {
      "Task":           str,          # the task prompt shown to the agent
      "system_message": str,          # the agent system prompt
      "trajectory": [                 # ReAct steps, in order
          {"observation": str,        #   result of the PREVIOUS action (step 0 = initial env msg)
           "thought":     str,        #   the model's reasoning for THIS step
           "action":      str,        #   parsed action, e.g. 'Bash(code="ls -la")' / 'Terminate(output="x.duckdb")'
           "response":    str},       #   raw model text: "Thought: ...\n\nAction: ..."
          ...
      ],
      "finished": bool,               # did the agent Terminate cleanly
      "result":   str                 # final Terminate output (e.g. the returned .duckdb filename)
    }

Verified against the reference file (read-only fetch, 2026-01-05 trial3
salesforce001): all 15 steps carry exactly the four per-step keys above, and the
top level carries exactly the five keys above — no more.

Our agent's get_trajectory() already produces {Task, system_message, trajectory}
with the identical per-step shape. run.py's native result.json additionally
carries `steps` and `result_files`, which are NOT part of the harbor schema, so
this converter drops them and fixes key order. The native result.json is left
untouched; this is written alongside it.
"""

import json
import os
from collections import OrderedDict

# The exact, ordered top-level and per-step key sets of the harbor schema.
HARBOR_TOP_KEYS = ("Task", "system_message", "trajectory", "finished", "result")
HARBOR_STEP_KEYS = ("observation", "thought", "action", "response")

TRAJECTORY_FILENAME = "spider-agent-dbt.trajectory.json"


def to_harbor_trajectory(trajectory_log, finished, result_output):
    """Build a harbor-conforming dict from our native trajectory pieces.

    trajectory_log: the dict returned by PromptAgent.get_trajectory(), i.e.
        {"Task", "system_message", "trajectory": [ {observation,thought,action,response}, ... ]}
    finished:      bool  (run.py's `done`)
    result_output: str   (run.py's `result_output`; coerced to str, "" if None)
    """
    steps = []
    for s in trajectory_log.get("trajectory", []):
        # Keep only the harbor per-step keys, in harbor order. Missing -> "".
        steps.append(OrderedDict((k, s.get(k, "")) for k in HARBOR_STEP_KEYS))

    out = OrderedDict()
    out["Task"] = trajectory_log.get("Task", "")
    out["system_message"] = trajectory_log.get("system_message", "")
    out["trajectory"] = steps
    out["finished"] = bool(finished)
    out["result"] = "" if result_output is None else str(result_output)
    return out


def write_harbor_trajectory(output_dir, trajectory_log, finished, result_output,
                            subdir="agent", filename=TRAJECTORY_FILENAME):
    """Write the conforming trajectory to <output_dir>/<subdir>/<filename>.

    Mirrors the harbor layout (.../<task>__<id>/agent/spider-agent-dbt.trajectory.json).
    Returns the path written.
    """
    harbor = to_harbor_trajectory(trajectory_log, finished, result_output)
    dest_dir = os.path.join(output_dir, subdir)
    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, filename)
    with open(path, "w") as f:
        json.dump(harbor, f, indent=2)
    return path
