# Qwen arm: blocked at smoke test (shelved 2026-08-21)

**Status: shelved.** Qwen3-235B cannot currently be run through the Pi harness. The
blocker is harness-level, not per-user configuration, and needs a shared decision.
The Opus 4.8 / Haiku 4.5 e-commerce matrix is unaffected — nothing about those two
arms was changed.

## What we were trying to do

Add a third model arm (Qwen3-235B) alongside Opus 4.8 and Haiku 4.5, to run the same
5 e-commerce tasks (shopify001, shopify002, shopify_holistic_reporting001,
recharge001, recharge002) as a plain no-DFC baseline, for comparison with the Sales
Qwen numbers.

## Facts established (all verified, not assumed)

- **The model exists and is the right one.** `qwen.qwen3-235b-a22b-2507-v1:0`
  ("Qwen3 235B A22B 2507"), `ON_DEMAND`, `ACTIVE`, streaming supported.
- **Region: us-east-2 is required.** The plain 235B text model is offered in
  us-east-2 but NOT in us-east-1 (us-east-1 only has `qwen.qwen3-vl-235b-a22b`, the
  vision-language variant). This independently confirms the long-standing
  "Qwen must be called from us-east-2" note.
- **No Qwen inference profile pre-existed.** Enumerated via boto3: 0 Qwen-matching
  inference profiles out of 69 in us-east-2 and 71 in us-east-1. `BEDROCK_QWEN_ARN`
  was never set; the path was genuinely dormant.
- **Per-model region routing works.** `pi --list-models` with `AWS_REGION=us-east-2`
  returns 118 Bedrock models including the 235B, and `run_task.py --aws_region`
  passes straight into the Pi subprocess env. Opus/Haiku on us-east-1 + Qwen on
  us-east-2 in one matrix run is not the problem.
- **Pi has no step/turn cap** (separately confirmed): only wall-clock `--timeout`.

## The blocker chain

Three paths were tried. Each fails for a different, independent reason.

### 1. Bare model ID -> explicit IAM deny

Invoking `qwen.qwen3-235b-a22b-2507-v1:0` directly:

```
AccessDeniedException: User: arn:aws:iam::920736616554:user/aup2005 is not authorized
to perform: bedrock:InvokeModelWithResponseStream on resource:
arn:aws:bedrock:us-east-2::foundation-model/qwen.qwen3-235b-a22b-2507-v1:0
with an explicit deny in an identity-based policy:
arn:aws:iam::920736616554:policy/RequiredTagsPolicy
```

An untagged foundation-model invocation carries no tags, so `RequiredTagsPolicy`
explicitly denies it. **On this account an inference profile is mandatory, not
optional.**

### 2. Application inference profile ARN -> Pi cannot infer the token ceiling

An application inference profile was created (tagged `project=data-flow-control`,
`billing-tag1=aup2005`):

```
arn:aws:bedrock:us-east-2:920736616554:application-inference-profile/xcoflhsehr9h
```

This **authenticates correctly** — requests reach the model and are rejected by the
model, not by IAM. But:

```
Validation error: The model returned the following errors:
{"error":{"code":"validation_error",
  "message":"'max_completion_tokens' (128000) exceeds model maximum (65536)",
  "param":null,"type":"invalid_request_error"}}
```

An application-inference-profile ARN is opaque — it does not contain the model name —
so Pi cannot match it to a catalog entry and falls back to a ceiling of 128000.
(128000 is Opus 4.8's max-out, suggesting Pi falls back to a default/first catalog
entry rather than a generic constant.) Qwen's real maximum is 65536.

### 3. `~/.pi/agent/models.json` -> not loaded by this Pi build in rpc mode

Pi's documented fix for exactly this case is a custom model entry in
`~/.pi/agent/models.json` (its "topmost user-config layer", per
`packages/coding-agent/src/core/provider-composer.ts`). A correct entry was written
registering the ARN with `maxTokens: 65536`, `contextWindow: 262144`,
`api: bedrock-converse-stream`, and Pi's published Qwen pricing.

**It had no effect.** Pi still sent `max_completion_tokens: 128000`. The custom model
never entered the model list at all: `--list-models` for us-east-2 returned 118
Bedrock models both before and after, with an empty `diff`. The path was verified
correct (`packages/coding-agent/package.json` -> `piConfig.configDir = ".pi"`, and
`getModelsPath()` = `~/.pi/agent/models.json`), so this is not a misplaced file.

### Secondary finding: Pi's Qwen catalog metadata is also wrong

Pi's built-in catalog entry for `qwen.qwen3-235b-a22b-2507-v1:0` declares
`maxTokens: 131072`, but the model rejects anything above `65536`. So **even if the
IAM deny in (1) were lifted, the bare-ID path would likely hit the same ceiling
error.** Any fix has to address the token ceiling, not just authentication.

Also note: Qwen does **not** support prompt caching. Setting
`AWS_BEDROCK_FORCE_CACHE=1` (which the Bedrock notes recommend for
application-inference-profile ARNs, since their ARNs lack the model name) causes:
`AccessDeniedException: You invoked an unsupported model or your request did not
allow prompt caching.` That env var must NOT be set for Qwen.

## What Qwen needs before it can run

A harness-level fix — this is a shared-harness decision, not a per-user one:

1. **Pi honors `models.json` in rpc mode**, so opaque profile ARNs can be given
   correct metadata; and/or
2. **Pi clamps `max_completion_tokens`** to the model's actual maximum instead of
   falling back to an unrelated catalog entry's ceiling.

Deliberately NOT done, as both are above the level of a smoke test:

- Patching Pi (a shared dependency).
- Amending `RequiredTagsPolicy` (an account-wide IAM change). Note this alone is
  probably insufficient anyway — see the secondary finding above.

## State left behind

- The inference profile `arn:aws:bedrock:us-east-2:920736616554:application-inference-profile/xcoflhsehr9h`
  still exists in AWS (tagged). It is correct and reusable if/when the harness fix lands.
- All throwaway `qwen-smoke*` run dirs were deleted (~66 MB).
- `~/.pi/agent/models.json` was **deleted**. It was provably inert for Opus/Haiku
  (empty `diff` of the full amazon-bedrock listing before/after; no provider-level
  `baseUrl`/`headers`/`compat`, which are the only fields that apply to all models of
  a provider). But it was inert *because it was never loaded* — so if a future Pi
  upgrade started honoring it, a latent config change would silently activate. Since
  it provided no working functionality, removing it was preferred over keeping a
  dormant no-op.
- Opus/Haiku re-verified after cleanup: both still resolve on us-east-1
  (`us.anthropic.claude-opus-4-8` 1M/128K, `us.anthropic.claude-haiku-4-5-20251001-v1:0`
  200K/64K), and all 10 `ecom-base` scored results are intact.

## Cost, for planning

Qwen is cheap: $0.22/M input, $0.88/M output (vs Haiku $1.00/$5.00). Scaling from the
measured Haiku total of $1.47 for these 5 tasks, the Qwen arm should cost roughly
$0.30-0.60 — well under $1 even at 2x the turn count. **Cost is not a reason to defer
this arm; only the harness blocker is.**
