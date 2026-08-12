#!/bin/bash
# Phase E driver: run the 6 (task x model) combos sequentially, resumable.
# Skips any run whose spider/result.json already exists (run.py also skips).
set -u
cd "$(dirname "$0")"
# Bedrock inference-profile IDs come from the environment -- never hardcoded,
# because the ARN embeds the AWS account number. Export both before running:
#   export BEDROCK_OPUS_ARN=...   export BEDROCK_HAIKU_ARN=...
: "${BEDROCK_OPUS_ARN:?set BEDROCK_OPUS_ARN to the Opus inference-profile ARN}"
: "${BEDROCK_HAIKU_ARN:?set BEDROCK_HAIKU_ARN to the Haiku inference-profile ARN}"
PY=~/miniconda3/envs/spider2/bin/python
TEST=../../spider2-dbt/examples/ecommerce3.jsonl
SUFFIX=${SUFFIX:-ecom-e1}
MAXSTEPS=${MAXSTEPS:-30}
MAXTOKENS=${MAXTOKENS:-8192}

# combos: model | index | instance_id
COMBOS=(
  "bedrock/claude-opus-4-8|1|recharge001"
  "bedrock/claude-opus-4-8|2|recharge002"
  "bedrock/claude-opus-4-8|0|shopify001"
  "bedrock/claude-haiku-4-5|1|recharge001"
  "bedrock/claude-haiku-4-5|2|recharge002"
  "bedrock/claude-haiku-4-5|0|shopify001"
)

for c in "${COMBOS[@]}"; do
  IFS='|' read -r MODEL IDX INST <<< "$c"
  EXP="${MODEL##*/}-${SUFFIX}"
  RJ="output/${EXP}/${INST}/spider/result.json"
  LOG="logs/run_${INST}_${MODEL##*/}_${SUFFIX}.log"
  if [ -f "$RJ" ]; then
    echo ">>> SKIP ${EXP}/${INST} (result.json exists)"
    continue
  fi
  # clean any stale container with this exact name
  docker rm -f "${EXP}-${INST}" >/dev/null 2>&1 || true
  echo ">>> RUN  ${EXP}/${INST}  (log: ${LOG})"
  $PY run.py --model "$MODEL" --suffix "$SUFFIX" \
    --temperature 0 --max_steps "$MAXSTEPS" --max_tokens "$MAXTOKENS" \
    --test_path "$TEST" --example_index "$IDX" --output_dir output > "$LOG" 2>&1
  echo "    exit=$? finished=$($PY -c "import json;print(json.load(open('$RJ')).get('finished')) " 2>/dev/null || echo '?')"
done
echo ">>> DRIVER DONE"
