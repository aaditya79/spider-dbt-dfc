#!/usr/bin/env bash
# Run one spider2-dbt task through Pi N times, fresh fixture copy each run.
# Usage: repeat_runs.sh <tag> <n> <instance_id> [extra run_task.py args...]
set -u

TAG="${1:?tag required}"
N="${2:?n required}"
INSTANCE="${3:-recharge001}"
shift 3 || true
EXTRA=("$@")

# Repo root, derived from this script's own location -- never hardcoded. The tree
# moved once and every hardcoded copy of the old path broke silently.
SPIDER="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY=~/miniconda3/envs/spider2/bin/python

for i in $(seq 1 "$N"); do
  EXP="${TAG}-r${i}"
  echo "===== RUN $i/$N  experiment_id=$EXP  $(date +%H:%M:%S) ====="
  "$PY" "$SPIDER/pi_runner/run_task.py" \
    --instance_id "$INSTANCE" \
    --experiment_id "$EXP" \
    --timeout 2400 \
    --force \
    --quiet \
    ${EXTRA[@]+"${EXTRA[@]}"} \
    > "$SPIDER/runs/pi/${EXP}.stdout.json" 2> "$SPIDER/runs/pi/${EXP}.stderr.log"
  echo "run $i exit=$?"
done
echo "===== ALL $N RUNS DONE ($TAG) $(date +%H:%M:%S) ====="
