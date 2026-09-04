#!/usr/bin/env bash
# Matched seed sweep for the namegate policy: N=10 policy-OFF vs N=10 policy-ON.
# Task/model fixed at recharge001 + Opus (archived base rate 7/20 = 35% wrong-name).
# Fresh pristine fixture per run; official scoring via score_run.py inside run_task.py.
# RESUMABLE: a cell is skipped if its stdout.json parses with a non-null score.
#
# Usage: run_namegate_sweep.sh [N]
set -u

N="${1:-10}"
SPIDER=~/Desktop/DAPLab/spider
PY=~/miniconda3/envs/spider2/bin/python
RUNS="$SPIDER/runs/pi"
TASK=recharge001
MODEL="us.anthropic.claude-opus-4-8"
TIMEOUT="${TIMEOUT:-2400}"

done_already () {
  [ -s "$1" ] && "$PY" -c "
import json,sys
try: r=json.load(open('$1'))
except Exception: sys.exit(1)
sys.exit(0 if r.get('score') is not None else 1)" 2>/dev/null
}

run_cell () {   # $1=exp  shift -> extra args
  local EXP="$1"; shift
  local OUT="$RUNS/${EXP}.stdout.json"
  if done_already "$OUT"; then echo "  SKIP (done)  $EXP"; return; fi
  echo "  RUN  $EXP  $(date +%H:%M:%S)"
  "$PY" "$SPIDER/pi_runner/run_task.py" \
    --instance_id "$TASK" --experiment_id "$EXP" --model "$MODEL" \
    --timeout "$TIMEOUT" --force --quiet "$@" \
    > "$OUT" 2> "$RUNS/${EXP}.stderr.log"
  echo "       exit=$? score=$("$PY" -c "
import json
try: print(json.load(open('$OUT')).get('score'))
except Exception: print('ERR')" 2>/dev/null)"
}

echo "===== NAMEGATE SWEEP  task=$TASK model=opus  N=$N per arm ====="
echo "--- ARM: policy OFF ---"
for i in $(seq 1 "$N"); do run_cell "ns-off-r${i}" --dfc-policy off; done
echo "--- ARM: policy ON (namegate) ---"
for i in $(seq 1 "$N"); do run_cell "ns-on-r${i}" --dfc-policy namegate; done
echo "===== SWEEP DONE $(date +%H:%M:%S) ====="
