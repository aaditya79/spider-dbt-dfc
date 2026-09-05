#!/usr/bin/env bash
# Stacked-policy sweep: recharge001 + Opus, N runs with BOTH namegate and the
# recharge001 discount/amount checker active, evaluated namegate-first.
# Matched control is the already-measured policy-off arm (ns-off-r1..10, 0/10).
set -u
N="${1:-10}"
# Repo root, derived from this script's own location -- never hardcoded. The tree
# moved once and every hardcoded copy of the old path broke silently.
SPIDER="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY=~/miniconda3/envs/spider2/bin/python
RUNS="$SPIDER/runs/pi"
TASK=recharge001
MODEL="us.anthropic.claude-opus-4-8"
TIMEOUT="${TIMEOUT:-2400}"
POLICY="namegate,recharge001"

done_already () {
  [ -s "$1" ] && "$PY" -c "
import json,sys
try: r=json.load(open('$1'))
except Exception: sys.exit(1)
sys.exit(0 if r.get('score') is not None else 1)" 2>/dev/null
}

echo "===== STACKED SWEEP  task=$TASK model=opus policy=$POLICY  N=$N ====="
for i in $(seq 1 "$N"); do
  EXP="st-r${i}"; OUT="$RUNS/${EXP}.stdout.json"
  if done_already "$OUT"; then echo "  SKIP (done)  $EXP"; continue; fi
  echo "  RUN  $EXP  $(date +%H:%M:%S)"
  "$PY" "$SPIDER/pi_runner/run_task.py" \
    --instance_id "$TASK" --experiment_id "$EXP" --model "$MODEL" \
    --dfc-policy "$POLICY" --timeout "$TIMEOUT" --force --quiet \
    > "$OUT" 2> "$RUNS/${EXP}.stderr.log"
  echo "       exit=$? score=$("$PY" -c "
import json
try: print(json.load(open('$OUT')).get('score'))
except Exception: print('ERR')" 2>/dev/null)"
done
echo "===== STACKED SWEEP DONE $(date +%H:%M:%S) ====="
