#!/usr/bin/env bash
# E-commerce 5-task x N-seed sweep under the spider_pi_2.0 scaffold (--scaffold dbt,
# the run_task.py default). No DFC. RESUMABLE: a cell is skipped if its stdout.json
# exists, parses, and has a non-null score.
#
# Usage: run_ecom_v2.sh <qwen|haiku|opus> [seeds=3]
#   experiment_id = ecom-v2-<model>-<task>-r<seed>
#   TASKS="recharge001 shopify002" restricts the task list (e.g. one process per task
#   for parallel sweeps); seeds for a task always run sequentially.
#   HARNESS_RETRIES=2 (default) re-runs a cell whose verdict is HARNESS_ERROR (bad
#   tool name -> Bedrock 400, network drop, tool-call cap) up to that many extra
#   times; a cell that still ends HARNESS_ERROR keeps score=null and is reported.
#   MAX_TOOL_CALLS=150 (default) passes --max-tool-calls to run_task.py; 0 disables.
#   EXP_PREFIX=ecom-v3 names the experiment series (default ecom-v2).
#   RUN_TASK_EXTRA="--no-pi-extension" (or "--tools read,bash,write,grep,find,ls",
#   "--scaffold minimal", ...) appends arbitrary run_task.py args, for A/B arms.
#
# Qwen runs through the tagged application inference profile in us-east-2 (the bare
# model ID is IAM-denied on this account). Its metadata (maxTokens 65536) comes from
# ~/.pi/agent/models.json -- see docs/qwen_arm_blocker.md for why that is required.
set -u

KEY="${1:?model key required: qwen|haiku|opus}"
SEEDS="${2:-3}"
SPIDER="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY=~/miniconda3/envs/spider2/bin/python
RUNS="$SPIDER/runs/pi"
PI="${PI:-$HOME/Desktop/Desktop - Aaditya’s MacBook Pro/DAPLab/pi/pi-test.sh}"
TIMEOUT="${TIMEOUT:-2400}"
HARNESS_RETRIES="${HARNESS_RETRIES:-2}"
# 6 of the 9 empty ecom-v4 runs hit the 40-min wall clock at 98-215 calls without
# materializing anything; a cap ends them ~20 min earlier with the same outcome and
# lets the DFC loop steer instead. 0 disables.
MAX_TOOL_CALLS="${MAX_TOOL_CALLS:-150}"
RUN_TASK_EXTRA="${RUN_TASK_EXTRA:-}"

case "$KEY" in
  qwen)  MODEL="arn:aws:bedrock:us-east-2:920736616554:application-inference-profile/xcoflhsehr9h"
         REGION="us-east-2" ;;
  haiku) MODEL="us.anthropic.claude-haiku-4-5-20251001-v1:0"; REGION="us-east-1" ;;
  # Opus goes through a tagged application inference profile for the same reason
  # Qwen does: RequiredTagsPolicy explicitly denies the untagged system profile
  # us.anthropic.claude-opus-4-8 (AccessDenied on InvokeModelWithResponseStream).
  opus)  MODEL="arn:aws:bedrock:us-east-1:920736616554:application-inference-profile/t65nyo4xtz4t"
         REGION="us-east-1" ;;
  *) echo "unknown model key: $KEY"; exit 2 ;;
esac

TASKS=(${TASKS:-shopify001 shopify002 shopify_holistic_reporting001 recharge001 recharge002})

# The run record on disk is authoritative: the driver's `>` redirection truncates
# the stdout file when a retry re-enters, which left a scored cell looking like ERR
# (ecom-v4 shopify002-r3). record_path echoes <run_dir>/_pi_meta/<instance>/run_record.json.
record_path () {   # $1 = experiment id  $2 = instance id
  echo "$RUNS/$1/_pi_meta/$2/run_record.json"
}

done_already () {   # $1 = run_record.json path
  [ -s "$1" ] && "$PY" -c "
import json,sys
try: r=json.load(open('$1'))
except Exception: sys.exit(1)
sys.exit(0 if r.get('score') is not None else 1)" 2>/dev/null
}

echo "===== ecom-v2 $KEY  ${#TASKS[@]} tasks x $SEEDS seeds  $(date +%H:%M:%S) ====="
for SEED in $(seq 1 "$SEEDS"); do
  for TASK in "${TASKS[@]}"; do
    EXP="${EXP_PREFIX:-ecom-v2}-${KEY}-${TASK}-r${SEED}"
    OUT="$RUNS/${EXP}.stdout.json"
    REC="$(record_path "$EXP" "$TASK")"
    if done_already "$REC"; then echo "  SKIP (done)  $EXP"; continue; fi
    for ATTEMPT in $(seq 0 "$HARNESS_RETRIES"); do
      [ "$ATTEMPT" -gt 0 ] && echo "  RETRY $ATTEMPT/$HARNESS_RETRIES (harness error)  $EXP"
      echo "  RUN  $EXP  $(date +%H:%M:%S)"
      "$PY" "$SPIDER/pi_runner/run_task.py" \
        --instance_id "$TASK" --experiment_id "$EXP" \
        --model "$MODEL" --aws_region "$REGION" --pi "$PI" \
        --timeout "$TIMEOUT" --max-tool-calls "$MAX_TOOL_CALLS" --force --quiet \
        $RUN_TASK_EXTRA \
        > "$OUT" 2> "$RUNS/${EXP}.stderr.log"
      RESULT=$("$PY" -c "
import json
try:
    r=json.load(open('$REC')); print(r.get('score'), r.get('verdict'), (r.get('harness_error') or {}).get('kind',''))
except Exception: print('ERR ERR')" 2>/dev/null)
      echo "       exit=$? score=${RESULT%% *} verdict=${RESULT#* }"
      case "$RESULT" in *HARNESS_ERROR*) continue ;; *) break ;; esac
    done
  done
done
echo "===== ecom-v2 $KEY DONE $(date +%H:%M:%S) ====="
