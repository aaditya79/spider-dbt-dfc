#!/usr/bin/env bash
# Harness-feedback experiment: 3 arms x 3 independent trials, recharge001 + Opus.
# Each trial is a fresh fixture copy and a fresh Pi rpc session -- no trial carries
# another's context. No DFC policy in any arm; the only variable is --harness-nudge.
#
# VALIDITY GUARD (added after the Aug 25-26 batch was lost to a DNS outage that
# recorded nine `score: 0` records indistinguishable from real failures):
#   * "done" now means pi_runner/nudge_validity.py classifies the record VALID --
#     the agent reached the model AND settled naturally. A zero-token record is NOT
#     done, it is VOID, and it is retried rather than recorded.
#   * every VOID attempt is archived under _void_nudge_retries/ so nothing is silently
#     overwritten and the failure remains auditable.
#   * DNS is checked before each attempt; the driver waits rather than burning an
#     attempt into a dead network.
set -u
N="${1:-3}"
SPIDER=~/Desktop/DAPLab/spider
PY=~/miniconda3/envs/spider2/bin/python
RUNS="$SPIDER/runs/pi"
VALIDITY="$SPIDER/pi_runner/nudge_validity.py"
VOIDDIR="$RUNS/_void_nudge_retries"
TASK=recharge001
MODEL="us.anthropic.claude-opus-4-8"
TIMEOUT="${TIMEOUT:-2400}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-3}"
BEDROCK_HOST="bedrock-runtime.us-east-1.amazonaws.com"

mkdir -p "$VOIDDIR"

status_of () {  # -> VALID | VOID_* | MISSING
  "$PY" - "$1" <<'PYX' 2>/dev/null || echo MISSING
import sys, os
sys.path.insert(0, os.path.expanduser("~/Desktop/DAPLab/spider/pi_runner"))
from nudge_validity import classify
print(classify(sys.argv[1])[0])
PYX
}

detail_of () { "$PY" "$VALIDITY" "$1" 2>/dev/null | head -1; }

wait_for_network () {
  local tries=0
  while ! nslookup "$BEDROCK_HOST" >/dev/null 2>&1; do
    tries=$((tries+1))
    if [ "$tries" -gt 60 ]; then
      echo "       NETWORK still down after 30min of waiting; giving up on this attempt"
      return 1
    fi
    echo "       NETWORK down ($BEDROCK_HOST unresolvable) -- waiting 30s [$tries]"
    sleep 30
  done
  return 0
}

echo "===== NUDGE ARMS  task=$TASK model=opus  N=$N per arm  max_attempts=$MAX_ATTEMPTS ====="
echo "===== start $(date '+%Y-%m-%d %H:%M:%S') ====="

for MODE in off generic specific; do
  echo "--- ARM: harness-nudge=$MODE ---"
  for i in $(seq 1 "$N"); do
    EXP="nudge-${MODE}-r${i}"; OUT="$RUNS/${EXP}.stdout.json"

    ST="$(status_of "$OUT")"
    if [ "$ST" = "VALID" ]; then echo "  SKIP (valid)  $EXP"; continue; fi

    attempt=0
    while [ "$attempt" -lt "$MAX_ATTEMPTS" ]; do
      attempt=$((attempt+1))
      if ! wait_for_network; then break; fi
      echo "  RUN  $EXP  attempt $attempt/$MAX_ATTEMPTS  $(date +%H:%M:%S)"
      "$PY" "$SPIDER/pi_runner/run_task.py" \
        --instance_id "$TASK" --experiment_id "$EXP" --model "$MODEL" \
        --harness-nudge "$MODE" --dfc-policy off \
        --timeout "$TIMEOUT" --force --quiet \
        > "$OUT" 2> "$RUNS/${EXP}.stderr.log"
      rc=$?
      ST="$(status_of "$OUT")"
      echo "       exit=$rc  status=$ST"
      echo "       $(detail_of "$OUT")"
      if [ "$ST" = "VALID" ]; then break; fi

      # VOID: archive the attempt so it is never mistaken for a result, then retry.
      DEST="$VOIDDIR/${EXP}-attempt${attempt}-${ST}"
      rm -rf "$DEST"; mkdir -p "$DEST"
      [ -e "$OUT" ] && mv "$OUT" "$DEST/"
      [ -e "$RUNS/${EXP}.stderr.log" ] && mv "$RUNS/${EXP}.stderr.log" "$DEST/"
      [ -d "$RUNS/${EXP}" ] && mv "$RUNS/${EXP}" "$DEST/"
      echo "       VOID -> archived to $DEST ; retrying"
    done

    FINAL="$(status_of "$OUT")"
    [ "$FINAL" = "VALID" ] || echo "  !! $EXP EXHAUSTED $MAX_ATTEMPTS attempts, last=$FINAL -- recorded VOID, NOT a result"
  done
done

echo "===== NUDGE ARMS DONE $(date '+%Y-%m-%d %H:%M:%S') ====="
echo "--- final validity ---"
for MODE in off generic specific; do
  for i in $(seq 1 "$N"); do
    OUT="$RUNS/nudge-${MODE}-r${i}.stdout.json"
    printf '  %-20s %s\n' "nudge-${MODE}-r${i}" "$(status_of "$OUT")"
  done
done
