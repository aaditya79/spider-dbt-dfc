#!/usr/bin/env bash
# Arm A (scaffold) / Arm B (namegate policy) experiment driver. RESUMABLE.
#
# Arms are deliberately independent so any improvement is attributable to one lever:
#   Arm A: no DFC, scaffold varied           (cur = current ORIENTATION, sf = schema-first)
#   Arm B: current scaffold, --dfc-policy namegate
# Never both at once.
#
# Resume: a cell is SKIPPED if its stdout.json exists, parses, and has a non-null score.
#
# Usage: run_arms.sh <A|B>
set -u

ARM="${1:?arm required: A or B}"
# Repo root, derived from this script's own location -- never hardcoded. The tree
# moved once and every hardcoded copy of the old path broke silently.
SPIDER="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY=~/miniconda3/envs/spider2/bin/python
RUNS="$SPIDER/runs/pi"
export SCAFFOLD="/private/tmp/claude-501/-Users-aadityapai-Desktop-DAPLab-spider/748bea9e-8047-461d-819d-df2245cf9bf8/scratchpad/schema_first_scaffold.txt"
TIMEOUT="${TIMEOUT:-2400}"

OPUS="us.anthropic.claude-opus-4-8"
HAIKU="us.anthropic.claude-haiku-4-5-20251001-v1:0"

# task|modelkey|model-id  -- the confirmed wrong-name failures and the model that failed them
CELLS=(
  "recharge001|opus|$OPUS"
  "recharge002|opus|$OPUS"
  "shopify002|haiku|$HAIKU"
)

done_already () {   # $1 = stdout.json path
  [ -s "$1" ] && "$PY" -c "
import json,sys
try: r=json.load(open('$1'))
except Exception: sys.exit(1)
sys.exit(0 if r.get('score') is not None else 1)" 2>/dev/null
}

run_cell () {       # $1=exp  $2=task  $3=model  shift 3 -> extra args
  local EXP="$1" TASK="$2" MODEL="$3"; shift 3
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

if [ "$ARM" = "A" ]; then
  echo "===== ARM A : scaffold test (no DFC) ====="
  for c in "${CELLS[@]}"; do
    TASK="${c%%|*}"; REST="${c#*|}"; KEY="${REST%%|*}"; MODEL="${REST#*|}"
    # (a) current scaffold: default ORIENTATION, no override
    run_cell "armA-cur-${KEY}-${TASK}" "$TASK" "$MODEL"
    # (b) schema-first scaffold: full prompt override, instruction substituted verbatim
    PROMPT=$("$PY" - "$TASK" "$SPIDER" <<'EOF'
import json, sys, os
task = sys.argv[1]
scaffold = open(os.environ["SCAFFOLD"]).read()
# argv[2] is $SPIDER: the heredoc is quoted, so the shell cannot expand it here.
tasks = os.path.join(sys.argv[2], "Spider2", "spider2-dbt", "examples",
                     "spider2-dbt.jsonl")
for line in open(tasks):
    line = line.strip()
    if not line: continue
    rec = json.loads(line)
    if rec.get("instance_id") == task:
        sys.stdout.write(scaffold.replace("{instruction}", rec["instruction"]))
        break
else:
    sys.exit(f"no instruction for {task}")
EOF
)
    run_cell "armA-sf-${KEY}-${TASK}" "$TASK" "$MODEL" --prompt "$PROMPT"
  done
  echo "===== ARM A DONE $(date +%H:%M:%S) ====="

elif [ "$ARM" = "B" ]; then
  echo "===== ARM B : namegate policy (current scaffold) ====="
  for c in "${CELLS[@]}"; do
    TASK="${c%%|*}"; REST="${c#*|}"; KEY="${REST%%|*}"; MODEL="${REST#*|}"
    run_cell "armB-namegate-${KEY}-${TASK}" "$TASK" "$MODEL" --dfc-policy namegate
  done
  echo "===== ARM B DONE $(date +%H:%M:%S) ====="
else
  echo "unknown arm: $ARM (expected A or B)"; exit 2
fi
