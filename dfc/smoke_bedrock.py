#!/usr/bin/env python
"""Standalone smoke test for the Bedrock Converse adapter.

Sends ONE hello-world prompt to each CONFIGURED model through bedrock_llm and
prints the region, inference-profile ARN, the exact inferenceConfig that was
sent, the resolved sampling condition (honest about temperature being dropped
for models that reject it), and the reply. Run this BEFORE touching the
benchmark to confirm the endpoints work.

Models whose ARN env var is unset are SKIPPED, not failed — this keeps the
dormant Qwen (us-east-2) path out of the way unless BEDROCK_QWEN_ARN is set.

Prereqs (provided by the user; never hardcoded):
  - AWS creds in the standard chain (env vars or ~/.aws/credentials).
  - export BEDROCK_OPUS_ARN=<Opus inference-profile ARN, us-east-1>
  - export BEDROCK_HAIKU_ARN=<Haiku inference-profile ARN, us-east-1>
  - (optional) export BEDROCK_QWEN_ARN=<Qwen inference-profile ARN, us-east-2>

Usage:
  python smoke_bedrock.py
"""

import os
import sys

from spider_agent.agent.bedrock_llm import (
    BEDROCK_MODELS, converse, sampling_metadata,
    BedrockAccessDenied, BedrockValidation, BedrockError,
)

SYSTEM = "You are a terse assistant. Answer in a single short sentence."
PROMPT = "Say hello and name which model family you belong to."
REQUESTED_TEMP = 0.0


def _has_arn(cfg):
    return bool(os.environ.get(cfg["arn_env"]))


def main():
    print("=" * 72)
    print("Bedrock Converse smoke test (requested temperature = %.1f)" % REQUESTED_TEMP)
    print("=" * 72)
    any_fail = False
    for model_key, cfg in BEDROCK_MODELS.items():
        if not _has_arn(cfg):
            print(f"\n### {model_key}\n  SKIPPED (no ARN; {cfg['arn_env']} unset — dormant)")
            continue
        samp = sampling_metadata(model_key, REQUESTED_TEMP)
        print(f"\n### {model_key}")
        print(f"  sampling: {samp['sampling']}")
        try:
            conv = [{"role": "user", "content": [{"text": PROMPT}]}]
            text = converse(
                model_key, SYSTEM, conv,
                max_tokens=256, temperature=REQUESTED_TEMP,
            )
            # Recompute the inferenceConfig the adapter actually sent, for display.
            inf = {"maxTokens": 256}
            if samp["temperature_param_supported"]:
                inf["temperature"] = REQUESTED_TEMP
            print(f"  inferenceConfig sent: {inf}")
            print(f"  reply : {text.strip()}")
        except BedrockAccessDenied as e:
            any_fail = True
            print(f"  ACCESS DENIED: {e}")
        except BedrockValidation as e:
            any_fail = True
            print(f"  VALIDATION ERROR: {e}")
        except BedrockError as e:
            any_fail = True
            print(f"  BEDROCK ERROR: {e}")
        except Exception as e:
            any_fail = True
            print(f"  UNEXPECTED ERROR: {type(e).__name__}: {e}")

    print("\n" + "=" * 72)
    print("DONE" + ("  (one or more models failed — see above)" if any_fail else "  (all configured models OK)"))
    return 1 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main())
