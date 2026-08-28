#!/usr/bin/env bash
# Harness-feedback experiment, extended to the e-commerce family.
#
# Tests whether the SPECIFIC arm overfits to recharge001. Same three arms as the
# recharge001 run, now across tasks whose declared-unbuilt set is a different size:
#   recharge002  1 declared-unbuilt target  (clean parallel to recharge001)
#   shopify002   5 declared-unbuilt targets
#   shopify001   6 declared-unbuilt targets (official pass is uninformative here --
#                the fixture's gold is stale and time-dependent; naming still valid)
#
# The GENERIC arm's text is byte-identical across every task -- it lives as a single
# constant in nudge_bin/dbt and contains no names at all. The SPECIFIC arm's text is
# built per task from that task's OWN dbt output (the `Did not find matching node for
# patch with name 'X' in file 'Y'` warning), so it needs no per-task authoring and
# still leaks nothing the agent could not obtain itself.
#
# Every trial: fresh fixture copy, fresh Pi rpc session, --dfc-policy off. No trial
# carries information from any other. Validity-gated exactly as run_nudge_arms.sh.
set -u
N="${1:-3}"
TASKS="${TASKS:-shopify001 shopify002 recharge002}"
SPIDER=~/Desktop/DAPLab/spider
PY=~/miniconda3/envs/spider2/bin/python
RUNS="$SPIDER/runs/pi"
VOIDDIR="$RUNS/_void_nudge_retries"
MODEL="us.anthropic.claude-opus-4-8"
TIMEOUT="${TIMEOUT:-2400}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-3}"
BEDROCK_HOST="bedrock-runtime.us-east-1.amazonaws.com"

mkdir -p "$VOIDDIR"

status_of () {
  "$PY" - "$1" <<'PYX' 2>/dev/null || echo MISSING
import sys, os
sys.path.insert(0, os.path.expanduser("~/Desktop/DAPLab/spider/pi_runner"))
from nudge_validity import classify
print(classify(sys.argv[1])[0])
PYX
}
detail_of () { "$PY" "$SPIDER/pi_runner/nudge_validity.py" "$1" 2>/dev/null | head -1; }

wait_for_network () {
  local tries=0
  while ! nslookup "$BEDROCK_HOST" >/dev/null 2>&1; do
    tries=$((tries+1))
    if [ "$tries" -gt 60 ]; then echo "       NETWORK down >30min; abandoning attempt"; return 1; fi
    echo "       NETWORK down ($BEDROCK_HOST unresolvable) -- waiting 30s [$tries]"
    sleep 30
  done
  return 0
}

echo "===== NUDGE FAMILY  tasks=[$TASKS]  model=opus  N=$N per arm per task ====="
echo "===== start $(date '+%Y-%m-%d %H:%M:%S') ====="

for TASK in $TASKS; do
  echo "########## TASK: $TASK ##########"
  for MODE in off generic specific; do
    echo "--- ARM: harness-nudge=$MODE  task=$TASK ---"
    for i in $(seq 1 "$N"); do
      EXP="nudge-${TASK}-${MODE}-r${i}"; OUT="$RUNS/${EXP}.stdout.json"

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
        [ "$ST" = "VALID" ] && break

        DEST="$VOIDDIR/${EXP}-attempt${attempt}-${ST}"
        rm -rf "$DEST"; mkdir -p "$DEST"
        [ -e "$OUT" ] && mv "$OUT" "$DEST/"
        [ -e "$RUNS/${EXP}.stderr.log" ] && mv "$RUNS/${EXP}.stderr.log" "$DEST/"
        [ -d "$RUNS/${EXP}" ] && mv "$RUNS/${EXP}" "$DEST/"
        echo "       VOID -> archived to $DEST ; retrying"
      done

      FINAL="$(status_of "$OUT")"
      [ "$FINAL" = "VALID" ] || echo "  !! $EXP EXHAUSTED $MAX_ATTEMPTS attempts, last=$FINAL -- VOID, not a result"
    done
  done
done

echo "===== NUDGE FAMILY DONE $(date '+%Y-%m-%d %H:%M:%S') ====="
echo "--- final validity ---"
for TASK in $TASKS; do
  for MODE in off generic specific; do
    for i in $(seq 1 "$N"); do
      printf '  %-34s %s\n' "nudge-${TASK}-${MODE}-r${i}" \
        "$(status_of "$RUNS/nudge-${TASK}-${MODE}-r${i}.stdout.json")"
    done
  done
done
