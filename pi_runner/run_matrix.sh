#!/usr/bin/env bash
# Run a task x model matrix through Pi, plain baseline (no DFC), RESUMABLE.
#
# Resume semantics: a cell is SKIPPED if its stdout.json already exists, is
# non-empty, and parses as JSON with a non-null "score". Anything else (missing,
# empty, truncated, unscored) is re-run with a fresh fixture copy. So re-invoking
# after a crash/interrupt only redoes the cells that did not complete.
#
# Usage: run_matrix.sh <tag> [task ...]
#   tag        experiment prefix; cells land at <tag>-<modelkey>-<task>
set -u

TAG="${1:?tag required}"
shift || true
TASKS=("$@")
if [ ${#TASKS[@]} -eq 0 ]; then
  TASKS=(shopify001 shopify002 shopify_holistic_reporting001 recharge001 recharge002)
fi

# Repo root, derived from this script's own location -- never hardcoded. The tree
# moved once and every hardcoded copy of the old path broke silently.
SPIDER="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY=~/miniconda3/envs/spider2/bin/python
RUNS="$SPIDER/runs/pi"
TIMEOUT="${TIMEOUT:-2400}"

# modelkey:model-id  -- keys are used in experiment_id, so keep them short/stable
MODELS=(
  "opus:us.anthropic.claude-opus-4-8"
  "haiku:us.anthropic.claude-haiku-4-5-20251001-v1:0"
)

total=$(( ${#TASKS[@]} * ${#MODELS[@]} ))
i=0
ran=0
skipped=0
echo "===== MATRIX $TAG : ${#TASKS[@]} tasks x ${#MODELS[@]} models = $total cells ====="

for entry in "${MODELS[@]}"; do
  KEY="${entry%%:*}"
  MODEL="${entry#*:}"
  for TASK in "${TASKS[@]}"; do
    i=$((i+1))
    EXP="${TAG}-${KEY}-${TASK}"
    OUT="$RUNS/${EXP}.stdout.json"

    if [ -s "$OUT" ] && "$PY" -c "
import json,sys
try:
    r=json.load(open('$OUT'))
except Exception:
    sys.exit(1)
sys.exit(0 if r.get('score') is not None else 1)
" 2>/dev/null; then
      echo "[$i/$total] SKIP (done)  $EXP"
      skipped=$((skipped+1))
      continue
    fi

    echo "[$i/$total] RUN  $EXP  model=$MODEL  $(date +%H:%M:%S)"
    "$PY" "$SPIDER/pi_runner/run_task.py" \
      --instance_id "$TASK" \
      --experiment_id "$EXP" \
      --model "$MODEL" \
      --timeout "$TIMEOUT" \
      --force \
      --quiet \
      > "$OUT" 2> "$RUNS/${EXP}.stderr.log"
    echo "        exit=$? score=$("$PY" -c "
import json
try: print(json.load(open('$OUT')).get('score'))
except Exception: print('ERR')
" 2>/dev/null)"
    ran=$((ran+1))
  done
done

echo "===== MATRIX $TAG DONE: ran=$ran skipped=$skipped  $(date +%H:%M:%S) ====="
