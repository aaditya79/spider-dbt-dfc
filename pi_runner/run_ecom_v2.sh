#!/usr/bin/env bash
# E-commerce 5-task x N-seed sweep under the spider_pi_2.0 scaffold (--scaffold dbt,
# the run_task.py default). No DFC. RESUMABLE: a cell is skipped if its stdout.json
# exists, parses, and has a non-null score.
#
# Usage: run_ecom_v2.sh <qwen|haiku|opus> [seeds=3]
#   experiment_id = ecom-v2-<model>-<task>-r<seed>
#   TASKS="recharge001 shopify002" restricts the task list (e.g. one process per task
#   for parallel sweeps); seeds for a task always run sequentially.
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

case "$KEY" in
  qwen)  MODEL="arn:aws:bedrock:us-east-2:920736616554:application-inference-profile/xcoflhsehr9h"
         REGION="us-east-2" ;;
  haiku) MODEL="us.anthropic.claude-haiku-4-5-20251001-v1:0"; REGION="us-east-1" ;;
  opus)  MODEL="us.anthropic.claude-opus-4-8";                REGION="us-east-1" ;;
  *) echo "unknown model key: $KEY"; exit 2 ;;
esac

TASKS=(${TASKS:-shopify001 shopify002 shopify_holistic_reporting001 recharge001 recharge002})

done_already () {   # $1 = stdout.json path
  [ -s "$1" ] && "$PY" -c "
import json,sys
try: r=json.load(open('$1'))
except Exception: sys.exit(1)
sys.exit(0 if r.get('score') is not None else 1)" 2>/dev/null
}

echo "===== ecom-v2 $KEY  ${#TASKS[@]} tasks x $SEEDS seeds  $(date +%H:%M:%S) ====="
for SEED in $(seq 1 "$SEEDS"); do
  for TASK in "${TASKS[@]}"; do
    EXP="ecom-v2-${KEY}-${TASK}-r${SEED}"
    OUT="$RUNS/${EXP}.stdout.json"
    if done_already "$OUT"; then echo "  SKIP (done)  $EXP"; continue; fi
    echo "  RUN  $EXP  $(date +%H:%M:%S)"
    "$PY" "$SPIDER/pi_runner/run_task.py" \
      --instance_id "$TASK" --experiment_id "$EXP" \
      --model "$MODEL" --aws_region "$REGION" --pi "$PI" \
      --timeout "$TIMEOUT" --force --quiet \
      > "$OUT" 2> "$RUNS/${EXP}.stderr.log"
    echo "       exit=$? score=$("$PY" -c "
import json
try: print(json.load(open('$OUT')).get('score'))
except Exception: print('ERR')" 2>/dev/null)"
  done
done
echo "===== ecom-v2 $KEY DONE $(date +%H:%M:%S) ====="
