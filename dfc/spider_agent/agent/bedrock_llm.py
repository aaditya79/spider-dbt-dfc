"""AWS Bedrock Converse adapter for spider-agent-dbt.

Adds three model backends, selectable by the agent's --model flag:
    bedrock/claude-opus-4-8    (frontier arm, us-east-1)
    bedrock/claude-haiku-4-5   (smaller arm,  us-east-1)
    bedrock/qwen3-235b         (dormant open model, us-east-2 — kept, not called)

Design notes:
- Models are reached via cross-region INFERENCE PROFILES, so the modelId we pass
  to converse() is a profile ARN, NOT a bare model name. The ARN is read from
  config/env (BEDROCK_OPUS_ARN / BEDROCK_HAIKU_ARN / BEDROCK_QWEN_ARN) and never
  hardcoded.
- Per-model region routing: Opus + Haiku -> us-east-1, Qwen3-235B -> us-east-2
  (Qwen's profile MUST be created in and called from us-east-2 or Bedrock raises
  a ValidationException).
- Per-model sampling policy (see BEDROCK_MODELS.supports_temperature): Opus 4.8
  REJECTS the `temperature` param, so we omit it and run at model-default
  sampling; Haiku 4.5 and Qwen accept temperature. sampling_metadata() reports
  the effective condition so the Opus/Haiku/GPT-5.5 comparison stays honest.
- AWS credentials come from the standard boto3 chain (env vars or ~/.aws). This
  module never reads, prints, logs, or writes credentials.
- maxTokens 4096 by default; exponential backoff on throttling; AccessDenied /
  Validation surfaced clearly WITH the region in the message.

This file is adapted to mirror the DFC bedrock_converse_llm.py wrapper.
"""

import json
import logging
import os
import time

logger = logging.getLogger("bedrock-llm")

# --- Model registry -----------------------------------------------------------
# Logical flag name -> region + the ENV VAR that holds the inference-profile ARN.
# ARNs are NOT literals here on purpose (see module docstring).
#
# supports_temperature: PER-MODEL, verified empirically against Bedrock Converse
# (hello-world probe on 2026-07-13), NOT from docs:
#   - Opus 4.8  -> False. Sending `temperature` raises ValidationException
#     "`temperature` is deprecated for this model." Sampling is model-default.
#   - Haiku 4.5 -> True. Accepts temperature:0 cleanly.
#   - Qwen3-235B-> True (dormant path; keep its temperature:0 if/when used).
# When False, converse() OMITS temperature from inferenceConfig entirely and
# sampling_metadata() records that honestly so cross-model comparisons don't
# misreport the sampling condition.
BEDROCK_MODELS = {
    "bedrock/claude-opus-4-8": {
        "region": "us-east-1", "arn_env": "BEDROCK_OPUS_ARN",
        "supports_temperature": False,
    },
    "bedrock/claude-haiku-4-5": {
        "region": "us-east-1", "arn_env": "BEDROCK_HAIKU_ARN",
        "supports_temperature": True,
    },
    "bedrock/qwen3-235b": {
        "region": "us-east-2", "arn_env": "BEDROCK_QWEN_ARN",
        "supports_temperature": True,
    },
}

# Optional fallback config file (gitignored): {"bedrock/claude-opus-4-8": "<arn>", ...}
_CONFIG_FILENAME = "bedrock_config.json"

DEFAULT_MAX_TOKENS = 4096
DEFAULT_TEMPERATURE = 0.0
DEFAULT_MAX_RETRIES = 6
_RETRYABLE = {
    "ThrottlingException", "TooManyRequestsException",
    "ServiceUnavailableException", "InternalServerException",
    "ModelTimeoutException",
}


class BedrockError(Exception):
    """Generic Bedrock adapter error."""


class BedrockAccessDenied(BedrockError):
    """Model access not granted for this model in this region (fix in console)."""


class BedrockValidation(BedrockError):
    """Validation error — usually wrong region for the inference-profile ARN."""


def _load_config_file():
    """Read optional bedrock_config.json from the agent root, if present."""
    here = os.path.dirname(os.path.abspath(__file__))
    # walk up to the spider-agent-dbt root
    for _ in range(4):
        candidate = os.path.join(here, _CONFIG_FILENAME)
        if os.path.exists(candidate):
            try:
                with open(candidate) as f:
                    return json.load(f)
            except Exception as e:  # pragma: no cover - defensive
                logger.warning("Failed to read %s: %s", candidate, e)
                return {}
        here = os.path.dirname(here)
    return {}


def _resolve(model_key):
    """Return (region, profile_arn) for a logical model name."""
    cfg = _model_cfg(model_key)
    arn = os.environ.get(cfg["arn_env"])
    if not arn:
        arn = _load_config_file().get(model_key)
    if not arn:
        raise BedrockError(
            f"No inference-profile ARN for '{model_key}'. Set env {cfg['arn_env']} "
            f"(or add it to {_CONFIG_FILENAME})."
        )
    return cfg["region"], arn


def _model_cfg(model_key):
    cfg = BEDROCK_MODELS.get(model_key)
    if cfg is None:
        raise BedrockError(
            f"Unknown bedrock model '{model_key}'. Known: {list(BEDROCK_MODELS)}"
        )
    return cfg


def sampling_metadata(model_key, requested_temperature=DEFAULT_TEMPERATURE):
    """Describe the sampling condition actually applied, for run metadata.

    Returns a dict so callers can log honestly whether the requested temperature
    was applied or dropped (Opus 4.8 rejects the param -> model-default sampling).
    """
    cfg = _model_cfg(model_key)
    top_p_applied = cfg.get("supports_top_p", False)
    if cfg.get("supports_temperature", True):
        return {
            "temperature": float(requested_temperature),
            "temperature_param_supported": True,
            "top_p_applied": top_p_applied,
            "sampling": f"temperature={float(requested_temperature)}"
                        + ("" if top_p_applied else "; top_p param not sent (model rejects temperature+top_p together)"),
        }
    return {
        "temperature": None,
        "temperature_param_supported": False,
        "top_p_applied": top_p_applied,
        "sampling": "model-default; temperature/top_p params unsupported by this model",
    }


def _client(region):
    import boto3  # lazy import so module load never requires boto3
    return boto3.client("bedrock-runtime", region_name=region)


def split_messages(messages):
    """Convert the agent's OpenAI-ish message list into Converse shape.

    Input items look like: {"role": r, "content": [{"type": "text", "text": t}, ...]}
    Returns (system_text, converse_messages) where converse_messages is
    [{"role": r, "content": [{"text": t}]}] with system turns pulled out.
    """
    system_parts = []
    conv = []
    for m in messages:
        role = m.get("role")
        parts = m.get("content", [])
        if isinstance(parts, str):
            text = parts
        else:
            text = "\n".join(
                p["text"] for p in parts
                if isinstance(p, dict) and "text" in p and p.get("type", "text") == "text"
            )
        if role == "system":
            if text:
                system_parts.append(text)
            continue
        # Bedrock only accepts roles "user" and "assistant"
        conv.append({"role": role, "content": [{"text": text}]})
    return "\n\n".join(system_parts).strip(), conv


def converse(model_key, system_text, conv_messages, *, max_tokens=DEFAULT_MAX_TOKENS,
             temperature=DEFAULT_TEMPERATURE, top_p=None, stop=None,
             max_retries=DEFAULT_MAX_RETRIES):
    """Low-level Converse call with retry/backoff. Returns response text."""
    from botocore.exceptions import ClientError, BotoCoreError

    region, arn = _resolve(model_key)
    cfg = _model_cfg(model_key)
    client = _client(region)

    # Per-model sampling policy (all verified empirically against Bedrock Converse):
    #   - Opus 4.8  rejects `temperature` (deprecated) -> omit -> model-default sampling.
    #   - Haiku 4.5 accepts `temperature` but rejects `temperature`+`topP` TOGETHER
    #     ("cannot both be specified") -> send temperature only, never topP.
    # So topP is opt-in via supports_top_p (default False): neither of our live arms sends it.
    inf = {"maxTokens": int(max_tokens)}
    if cfg.get("supports_temperature", True):
        inf["temperature"] = float(temperature)
    if top_p is not None and cfg.get("supports_top_p", False):
        inf["topP"] = float(top_p)
    # Bedrock rejects whitespace-only stop sequences; keep meaningful ones (max 4).
    stops = [s for s in (stop or []) if isinstance(s, str) and s.strip()]
    if stops:
        inf["stopSequences"] = stops[:4]

    kwargs = {"modelId": arn, "messages": conv_messages, "inferenceConfig": inf}
    if system_text:
        kwargs["system"] = [{"text": system_text}]

    delay = 2.0
    last_err = None
    for attempt in range(max_retries):
        try:
            resp = client.converse(**kwargs)
            return resp["output"]["message"]["content"][0]["text"]
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "AccessDeniedException":
                raise BedrockAccessDenied(
                    f"AccessDeniedException for '{model_key}' in {region}: model "
                    f"access is likely not granted yet in the Bedrock console for "
                    f"this model/region. (Not a code bug.)"
                ) from e
            if code == "ValidationException":
                raise BedrockValidation(
                    f"ValidationException for '{model_key}' in {region}: check the "
                    f"inference-profile ARN was created in and is called from {region}. "
                    f"Detail: {e}"
                ) from e
            if code in _RETRYABLE:
                last_err = e
                logger.warning(
                    "Bedrock %s for '%s' in %s (attempt %d/%d); backing off %.1fs",
                    code, model_key, region, attempt + 1, max_retries, delay,
                )
                time.sleep(delay)
                delay = min(delay * 2, 60.0)
                continue
            raise BedrockError(
                f"Bedrock ClientError '{code}' for '{model_key}' in {region}: {e}"
            ) from e
        except BotoCoreError as e:
            last_err = e
            logger.warning(
                "Bedrock transport error for '%s' in %s (attempt %d/%d): %s; backing off %.1fs",
                model_key, region, attempt + 1, max_retries, e, delay,
            )
            time.sleep(delay)
            delay = min(delay * 2, 60.0)

    raise BedrockError(
        f"Bedrock call for '{model_key}' in {region} failed after {max_retries} "
        f"retries. Last error: {last_err}"
    )


def converse_from_payload(payload):
    """Adapt the agent's call_llm payload to a Converse call. Returns text."""
    system_text, conv = split_messages(payload["messages"])
    return converse(
        payload["model"],
        system_text,
        conv,
        max_tokens=payload.get("max_tokens", DEFAULT_MAX_TOKENS),
        temperature=payload.get("temperature", DEFAULT_TEMPERATURE),
        top_p=payload.get("top_p"),
        stop=payload.get("stop"),
    )


def call(model_key, system, messages, **kw):
    """Convenience entrypoint for the smoke test.

    `messages` is a list of (role, text) tuples. Returns (region, arn, text).
    """
    region, arn = _resolve(model_key)
    conv = [{"role": r, "content": [{"text": t}]} for r, t in messages]
    text = converse(model_key, system, conv, **kw)
    return region, arn, text
