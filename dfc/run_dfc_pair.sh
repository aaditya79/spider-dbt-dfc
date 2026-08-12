#!/bin/bash
set -u
cd "$(dirname "$0")"
# Bedrock inference-profile ID comes from the environment -- never hardcoded,
# because the ARN embeds the AWS account number.
: "${BEDROCK_OPUS_ARN:?set BEDROCK_OPUS_ARN to the Opus inference-profile ARN}"
PY=~/miniconda3/envs/spider2/bin/python
TEST=../../spider2-dbt/examples/ecommerce3.jsonl
# 1) PROOF: WITH DFC policy
echo ">>> DFC-ON  (proof)"
docker rm -f claude-opus-4-8-dfc-on-recharge001 >/dev/null 2>&1 || true
$PY run.py --model bedrock/claude-opus-4-8 --suffix dfc-on --temperature 0 \
  --max_steps 50 --max_tokens 8192 --test_path "$TEST" --example_index 1 \
  --dfc_policy recharge001 --output_dir output > logs/run_dfc_on.log 2>&1
echo "    exit=$?"
# 2) CONTROL: WITHOUT DFC policy
echo ">>> DFC-OFF (control)"
docker rm -f claude-opus-4-8-dfc-off-recharge001 >/dev/null 2>&1 || true
$PY run.py --model bedrock/claude-opus-4-8 --suffix dfc-off --temperature 0 \
  --max_steps 50 --max_tokens 8192 --test_path "$TEST" --example_index 1 \
  --output_dir output > logs/run_dfc_off.log 2>&1
echo "    exit=$?"
echo ">>> PAIR DONE"
