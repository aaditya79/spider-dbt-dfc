# Pi ↔ dbt Integration Plan

**Status:** design note. No implementation.
**Scope:** drive one spider2-dbt task end to end through the Pi harness to a scored 0/1.
**Based on:** Phase 1 (repo scout) and Phase 2 (dbt-lens tool audit) of the Pi scouting pass.
**Pi version audited:** `earendil-works/pi` @ `a0bb4a4` (2026-07-31), cloned to `~/Desktop/DAPLab/pi/`.

> All file:line references are to the audited commit. Pi moves fast — re-verify before
> relying on any specific line number.

---

## 0. The finding this plan rests on

The Phase 2 audit concluded that **no dbt-specific tool needs to be built**. Pi's generic
`read` / `write` / `edit` / `bash` (+ `grep` / `find` / `ls`) surface covers every action a
spider2-dbt task requires. The only real gap was environmental: `dbt-duckdb` is installed
only inside the old harness's Docker image, not anywhere Pi's `bash` tool can reach.

This plan therefore contains **zero new TypeScript**. Everything below is a Python
orchestrator, a `pip install`, and CLI flags.

---

## 1. Architecture

```
┌──────────────────────────────── host (macOS) ────────────────────────────────┐
│                                                                              │
│  Python orchestrator (spider2 conda env)                                     │
│    │                                                                         │
│    │ 1. spawn                                                                │
│    ├──────────────►  pi --mode rpc   (Node/TypeScript)                       │
│    │  JSONL/stdin        │                                                   │
│    │                     │ read / write / edit / bash / grep / find / ls     │
│    │  JSONL/stdout       ▼                                                   │
│    │◄──────────────  cwd = spider2-dbt task dir                              │
│    │  events             │                                                   │
│    │                     └── bash: `dbt run` ──► ./recharge.duckdb           │
│    │                                                (materialized in place)  │
│    │ 2. wait for agent_settled                                               │
│    │                                                                         │
│    │ 3. score  ──►  score_run.py ──► eval_utils.duckdb_match ──► 0 / 1       │
│    │                (Python, reads the .duckdb directly)                     │
└──────────────────────────────────────────────────────────────────────────────┘
```

**The invariant: TypeScript never touches DuckDB.** Pi's only relationship to the database
is that it shells out to `dbt`, which happens to write one. Every DuckDB read — inspection
during the run, and scoring after it — happens in Python. This keeps the language boundary
at exactly one place (process spawn + JSONL) instead of smearing it across the eval path.

### The RPC seam, concretely

`pi --mode rpc` speaks strict LF-delimited JSONL over stdin/stdout. Protocol reference:
`packages/coding-agent/docs/rpc.md`.

**Orchestrator → Pi (stdin), one JSON object per line:**

| Command | Shape | Use |
|---|---|---|
| `prompt` | `{"id":"req-1","type":"prompt","message":"<task instruction>"}` | Deliver the task. This is the only command the milestone needs. |
| `steer` | `{"type":"steer","message":"..."}` | Optional mid-run nudge; not used in v1. |

**Pi → orchestrator (stdout), one JSON object per line:**

| Line | Shape | Orchestrator does |
|---|---|---|
| Command ack | `{"id":"req-1","type":"response","command":"prompt","success":true}` | Confirm the prompt was accepted. `success:false` = rejected before acceptance → abort. |
| `agent_start` | `{"type":"agent_start"}` | Mark run start. |
| `tool_execution_start` / `_end` | includes tool name + args | Log to the trajectory. This is where dbt invocations become visible. |
| `turn_end` | assistant message + tool results | Step accounting for the trajectory record. |
| `compaction_start` / `_end` | — | Log; useful signal that context pressure was hit. |
| **`agent_settled`** | `{"type":"agent_settled"}` | **Terminal signal. Stop reading, proceed to scoring.** |

> **Do not terminate on `agent_end`.** Pi's own Python example in `docs/rpc.md` breaks on
> `agent_end`, which is wrong for our purposes: `agent_end` marks one *low-level* run and
> may be followed by an automatic retry, a compaction-and-continue, or a queued follow-up.
> The docs are explicit that `agent_settled` is the "fully settled, nothing more coming"
> event. Terminating on `agent_end` would score a half-built dbt project whenever
> compaction fires mid-run — a silent, intermittent, and very confusing failure mode.
> **`agent_settled` is the only correct stop condition.**

### Why RPC over `--mode json`

`--mode json` is one-shot: prompt in, event stream out, exit. RPC keeps the process alive
and accepts further commands. For the v1 milestone either works, but RPC is the right
foundation — it leaves room for mid-run steering (e.g. a DFC-style intervention, feeding a
scorer hint back in, or a retry-with-guidance loop) without re-architecting the seam. The
extra cost now is a few lines of process management.

---

## 2. The run recipe for one task

```bash
cd ~/Desktop/DAPLab/spider/Spider2/spider2-dbt/examples/recharge001

pi --mode rpc \
   --approve \
   --tools read,bash,edit,write,grep,find,ls \
   --provider amazon-bedrock \
   --model "$BEDROCK_OPUS_ARN" \
   --name "pi-dbt-recharge001" \
   --session-dir ~/Desktop/DAPLab/spider/runs/pi/sessions
```

then on stdin:

```json
{"id":"req-1","type":"prompt","message":"Create a model to combine charge data, including line items, discounts, taxes, shipping, and refunds, while ensuring each item is uniquely identified and linked to its charge?"}
```

Flag by flag, and why:

| Flag | Why |
|---|---|
| `cwd = task dir` | Everything hinges on this. Pi binds every tool to `cwd` (`createTool(name, cwd, opts)`), and `profiles.yml` uses a relative DB path — see §3. |
| `--approve` / `-a` | **Required.** Without it, project-local settings and extensions are silently ignored in rpc/json/print mode. See §4. |
| `--tools read,bash,edit,write,grep,find,ls` | Only `read,bash,edit,write` are active by default. dbt projects have hundreds of files across `models/`, `macros/`, `dbt_packages/` — the agent needs `grep`/`find` to navigate. See §4. |
| `--provider amazon-bedrock` | Pi's Bedrock provider uses the standard AWS credential chain (`AWS_PROFILE` / IAM keys / bearer token), region defaults to `us-east-1`. |
| `--model "$BEDROCK_OPUS_ARN"` | Pi accepts an inference-profile ARN directly as the model ID. Keep the ARN in an env var, never a literal. |
| `--name` | Labels the session for later inspection. |
| `--session-dir` | Keeps Pi session JSONL alongside run artifacts rather than in `~/.pi/agent/sessions/`. The session file is the raw trajectory record. |

**Task instruction source:** `spider2-dbt/examples/spider2-dbt.jsonl`, keyed by
`instance_id`, field `instruction`. The orchestrator reads it from there — the instruction
is *not* stored in the task directory itself.

**Prompt composition.** The raw `instruction` field alone is likely insufficient. The old
harness wrapped it with environment framing (you are in a dbt project, here is how to run
it, materialize your result). Two options, in order of preference:

1. Put the framing in an `AGENTS.md` in the task dir, which Pi loads automatically as a
   context file. Cleanest — separates *task* from *environment description*, and the
   framing is reusable across all 71 tasks.
2. Prepend framing to the `prompt` message string.

Option 1 is preferred, with one caveat: dropping an `AGENTS.md` into the task dir mutates
the task fixture. Write it to a parent dir, or generate it into a working *copy* of the
task dir (which we want anyway — see §5).

---

## 3. Path (a): host-native dbt

### Setup

```
pip install "dbt-core~=1.9.0" "dbt-duckdb~=1.9.0"   # into the spider2 conda env
```

**Pin dbt-core to 1.9.x. This is not optional.** Installing unpinned (or with only the
`dbt_project.yml` constraint `require-dbt-version: [">=1.3.0", "<2.0.0"]`) resolves to
dbt-core 1.12.0, which **cannot open any spider2-dbt task**:

```
Compilation Error
  dbt expects 1 package(s) based on packages specified in packages.yml,
  but found only 4 package(s) installed in dbt_packages.
```

dbt 1.12 compares the root `packages.yml` count against the directory count in
`dbt_packages/`. The fixtures vendor **transitive** dependencies and ship **no
`package-lock.yml`** (there is none anywhere under `examples/`), so every task trips it —
recharge001 declares 1 and vendors 4, shopify001 declares 1 and vendors 5. The check lives
in **dbt-core, not the adapter**: downgrading only `dbt-duckdb` does not help.

Verified working pin: **dbt-core 1.9.10 + dbt-duckdb 1.9.6**. The 1.10/1.11 range is
untested. Do **not** "fix" this by running `dbt deps` — that re-resolves from the hub over
the network and can pull package versions different from the vendored ones, silently
changing results and breaking parity with the container.

Side effect to expect: the install upgrades protobuf (5.29.6 → 6.33.6) and pip warns about
`google-ai-generativelanguage` / `grpcio-status`. Verified metadata-only — `duckdb`,
`eval_utils.duckdb_match`, `score_run.py`, and `google.generativeai` all still work.

Audited state of the environment:

| Component | Where it is now |
|---|---|
| `dbt` / `dbt-duckdb` | **Only** inside Docker image `spider_agent-image` (`methods/spider-agent-dbt/spider_agent/images/spider_agent-image/Dockerfile`, final line `RUN pip install dbt-duckdb`). Absent from host and from the `spider2` env. |
| `duckdb` (Python) | Present on host: `spider2` env, v1.5.4. |

So the *scorer* already runs natively; only the *builder* is missing.

### Why cwd = task dir makes this work

`examples/recharge001/profiles.yml` (and every other task's):

```yaml
recharge:
  target: dev
  outputs:
    dev:
      type: duckdb
      path: "./recharge.duckdb"     # ← relative
      schema: main
```

The database path is **relative to the dbt invocation's working directory**. With
`cwd = examples/recharge001/`, `dbt run` materializes into
`examples/recharge001/recharge.duckdb` — in place, overwriting the seeded DB, which is
precisely the file the evaluator reads. No copying, no path rewriting, no `--project-dir`
juggling. The relative path is doing real work here; it is the reason this integration is
as thin as it is.

`profiles.yml` also sits in the task dir, so dbt discovers it without `--profiles-dir`.

### Scoring

Use the existing `methods/spider-agent-dbt/score_run.py`, written during Phase E:

```
python score_run.py --experiment_id <run-id> --instance_id recharge001
```

It globs recursively under `output/<experiment_id>/` for the expected DB filename, loads
the gold spec (`condition_tabs` / `condition_cols` / `ignore_orders`) from
`evaluation_suite/gold/spider2_eval.jsonl`, and calls the authoritative
`eval_utils.duckdb_match`. It additionally checks that each target table exists and has
`> 0` rows, so a silent no-op build cannot be scored as a pass.

**Prefer `score_run.py` over `evaluate.py` for a single task.** `evaluate.py` requires
assembling a `result_dir/results_metadata.jsonl` (it asserts on the exact filename) plus a
`result_dir/<instance_id>/<answer_or_path>` layout. That plumbing exists to score a whole
run of 60+ tasks and is pure overhead for one. Both call the same `duckdb_match`, so the
0/1 is identical. Keep `evaluate.py` as the path for full-set runs later.

For recharge001 the gold spec is:

```json
{"func": "duckdb_match",
 "parameters": {"gold": "recharge.duckdb",
                "condition_tabs": ["recharge__charge_line_item_history"],
                "condition_cols": [[0,3,4,5,6,7,8]],
                "ignore_orders": [true]}}
```

### Path (b): Docker routing — reproducibility fallback only

Kept out of the milestone, documented so it isn't re-derived later.

If host-native dbt drifts from the container (different dbt/duckdb patch versions
producing different results, or a task needing a package the host lacks), route Pi's `bash`
into the old harness's container instead of moving to a custom tool. Pi's `bash` tool
accepts a `spawnHook: (ctx) => ctx` that rewrites `command` / `cwd` / `env` before spawn —
wrapping each command in `docker exec -w <cwd> <container> bash -lc '<command>'` is a
~20-line extension. Template: `packages/coding-agent/examples/extensions/bash-spawn-hook.ts`.

Because the old harness bind-mounts the task dir `rw` into the container
(`spider_agent/envs/spider_agent.py`), `read`/`write`/`edit` can keep operating on the host
filesystem — only `bash` needs redirecting. If full isolation is ever wanted, all seven
tools expose a swappable `operations` backend, and
`packages/coding-agent/examples/extensions/gondolin/` overrides all seven to target a VM;
that is the reference implementation to copy.

**This remains a fallback.** Adopt it only if host-native dbt produces a demonstrable
scoring discrepancy — not preemptively.

---

## 4. Footgun checklist

Each of these is a silent failure — nothing errors, the run just quietly does the wrong
thing. Verify all six before trusting a first result.

- [ ] **The fixture is handed to the agent BROKEN. Do not pre-patch it.** Every recharge
      task ships `dbt_project.yml` in "buildkite" config —
      `order: "{{ ref('order_data') }}"` active, `recharge_order_identifier: "order_data"`
      commented out. `order_data` is a source *table* in the seeded DuckDB, not a model, so
      `ref()` fails at parse and **every** dbt command dies with `depends on a node named
      'order_data' which was not found`. **This is part of the task.** Verified from the
      successful Phase E run (`output/claude-opus-4-8-dfc-on/recharge001`): the run started
      with the same broken config and Opus flipped it itself at step 24 of 33. An
      orchestrator that "helpfully" fixes this first is solving part of the task for the
      agent and inflating the score. A fresh fixture failing `dbt compile` is **correct**.
      Expect the companion warning too — `Did not find matching node for patch with name
      'recharge__charge_line_item_history'` — that model is the deliverable.

- [ ] **`-a` / `--approve` is passed.** In rpc/json/print mode there is no UI, so the trust
      gate falls through to `defaultProjectTrust` (default `"ask"`) and returns `false`
      (`core/project-trust.ts:86-88`) — project-local settings and extensions are ignored
      **with no warning**. `trustOverride` short-circuits everything at line 47, and beats
      even a saved `"never"` in `trust.json`. Pass `-a` explicitly on every invocation
      rather than relying on global config; it's visible in the command line and immune to
      stale global state.

- [ ] **`grep` / `find` / `ls` are explicitly enabled.** Default active set is
      `["read","bash","edit","write"]` (`core/agent-session.ts:~2591`). All seven tools are
      *defined*; only four are *on*. An agent without `grep` on a 200-file dbt project will
      burn turns doing `bash ls -R` and reading files one at a time.

- [ ] **Compaction `reserveTokens` is an absolute token count, not a proportion.** Trigger
      is `contextTokens > contextWindow - reserveTokens` (`compaction.ts:235-237`); default
      `16384`. To get the team's ~50% rule on a 200k-context model, set `reserveTokens:
      100000`. **But that number is model-specific** — the same setting yields 25% on a
      400k model and never fires on a 100k one. If we switch models (Opus ↔ Haiku ↔
      whatever's next) without revisiting it, the compaction policy silently changes. A
      genuinely model-independent 50% rule needs an extension (§6). Defaults:
      `enabled: true`, `reserveTokens: 16384`, `keepRecentTokens: 20000`
      (`settings-manager.ts:766,779,783`).

- [ ] **Terminate on `agent_settled`, never `agent_end`.** See §1. Pi's own documented
      Python example gets this wrong. Breaking on `agent_end` scores a partially-built
      project whenever a retry or compaction-continue follows — intermittent and
      model-dependent, so it will pass local testing and fail under load.

- [ ] **`eval_utils` imports `google.cloud.bigquery` at module top.** The trimmed `spider2`
      env has no bigquery, so a bare `import eval_utils` raises. `score_run.py` already
      stubs the module before importing; any new scoring entry point must do the same or
      reuse `score_run.py`.

Two more worth knowing, though they should not bite at v1 scale:

- **No step/turn limits exist anywhere in Pi.** `agent-loop.ts:170` is a bare
  `while (true)`; a repo-wide search for max-steps/turns/iterations returns zero hits. The
  team's "remove step limits" goal needs no work — but it also means **nothing bounds a
  runaway task except cost**. The orchestrator should impose its own wall-clock timeout and
  kill the subprocess; that budget is now our responsibility, not the harness's.
- **Bash has no default timeout** (max ~24.8 days). Long `dbt build` runs will not be cut
  off. Same note as above: bound it in the orchestrator.

---

## 5. First milestone

**One task — `recharge001` — through Pi to a scored 0/1.** Chosen because its failure modes
(grain, fan-out, the discount-amount invariant) are already characterized from the Phase E
work, so a wrong answer is diagnosable rather than mysterious.

Steps:

1. `pip install dbt-duckdb` into `spider2`; verify `dbt --version` satisfies
   `>=1.3.0,<2.0.0`.
2. `npm install --ignore-scripts && npm run build` in `~/Desktop/DAPLab/pi/`. Verify
   `./pi-test.sh --version`.
3. Smoke Pi + Bedrock end to end, no dbt involved:
   `pi -p -a "list the .sql files under models/"` from a task dir. Confirms credentials,
   the ARN, tool wiring, and cwd binding independently of dbt.
4. Copy `examples/recharge001/` to a fresh working dir per run. **Never run against the
   pristine fixture** — `dbt run` overwrites the seeded `.duckdb` in place, and a failed
   run leaves it dirty for the next one. Copy-per-run also makes the run reproducible and
   gives `AGENTS.md` somewhere to live.
5. Write the orchestrator: spawn `pi --mode rpc` with the §2 flags, send the `prompt`, read
   events until `agent_settled`, tee every event to a trajectory log.
6. Score with `score_run.py`.

**Done means:**

- The orchestrator spawns Pi, delivers the instruction, and exits cleanly on
  `agent_settled` — no hangs, no premature termination.
- The agent used `bash` to invoke dbt at least once, visible in the
  `tool_execution_start` event log.
- `recharge__charge_line_item_history` exists in the produced DuckDB with `> 0` rows.
- `score_run.py` prints an authoritative `0` or `1`.

**A scored `0` is a successful milestone.** The deliverable is a working
harness→dbt→evaluator path, not a correct answer. Task accuracy is the next phase's
problem; conflating the two makes it impossible to tell an integration bug from a model
failure.

Sequencing note: steps 1–3 are independent of the orchestrator and each fails loudly and
specifically. Do them first — most of the plausible failures (missing dbt, bad ARN, unbuilt
Pi, trust gate) surface there, where they're one-line diagnoses, rather than inside the
RPC loop where they all look like "no events arrived".

---

## 6. Open questions (all deferred)

**True proportional compaction.** `reserveTokens` is absolute, so a "compact at 50%" policy
is model-specific config, not a rule. A small extension could compute the threshold from
`contextWindow` at runtime; Pi exposes `session_before_compact` and a custom-compaction hook
(`examples/extensions/custom-compaction.ts`). *Deferred:* a static `reserveTokens` tuned to
the model in use is correct for a single-model milestone. Revisit when we run more than one
model, which is exactly when the absolute/proportional distinction starts to bite.

**A `duckdb_query(sql)` tool.** Table inspection currently goes through
`bash` → `python -c "import duckdb; ..."`, which works but is awkward: shell quoting of
multi-line SQL is error-prone and results come back as unstructured text. A dedicated tool
would be better ergonomics and cleaner trajectory records. *Deferred:* it is an optimization,
and we should see whether the agent actually struggles with the bash path before adding
surface area. If added, note it breaks the zero-TypeScript property — worth it only if the
bash path measurably costs turns.

**Docker parity (path b).** Whether host-native dbt reproduces container results exactly is
untested. *Deferred:* the check is cheap once one task scores — run the same task both ways
and compare. Only worth building the `spawnHook` extension if they disagree.

**Bedrock parameter compatibility.** Prior Phase A/B work established that Opus 4.8 rejects
`temperature` via Converse (`ValidationException`), while Haiku 4.5 accepts it — the old
Python adapter omits the parameter per-model. Whether Pi's Bedrock provider handles this
correctly is **unverified**. Step 3's smoke test will surface it immediately if not; noting
it here so a `ValidationException` gets recognized rather than debugged from scratch.

**Trajectory format.** Phase E produced a harbor-parity `trajectory.json` writer. Pi's
session JSONL and RPC event stream carry equivalent information in a different shape.
*Deferred:* whether to translate Pi events into the harbor schema, or move the team to Pi's
native format, is a question for once runs are actually happening.
