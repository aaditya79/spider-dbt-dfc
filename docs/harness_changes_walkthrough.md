# The Pi↔dbt harness: what it is, and what we changed

Everything below was read off the code on disk; every claim carries a `file:line`. Where the repo and
the working tree disagree, the working tree wins and that is called out.

**Read this next to the code.** The single file that matters most is
`pi_runner/run_task.py` (656 lines). If you read only one thing, read
`main()` at `pi_runner/run_task.py:495-652` — the whole flow is there in ~150 lines.

---

## 0. Before you clone: what you need, and the one thing still broken

**All four DFC checkers ship, and a fresh clone can run all of them.** This was not
true until commit `d6c12bc` and the follow-up that made the mirror loadable; if you
read an earlier copy of this doc, ignore what it said about missing checkers.

`.gitignore:13` still excludes `/Spider2/` (the 7.1 GB `xlang-ai/Spider2` clone), so
the canonical checker location is not in the repo. `dfc/spider_agent/agent/` is a
tracked mirror that now carries all four — `dfc_check.py`, `dfc_check_idtype.py`,
`dfc_check_enum.py`, `dfc_check_namegate.py` — each byte-identical to its Spider2
counterpart (`diff -q` + `md5`).

Two mechanisms make the mirror actually loadable, rather than merely present:

- **`_import_agent_module()`** (`run_task.py:157-202`) tries the Spider2 clone first
  and falls back to the mirror. The fallback loads the file **by location**, not by
  re-running package resolution: `spider_agent.agent` is a regular package on the
  Spider2 side and a namespace portion in the mirror, and a regular package wins
  regardless of `sys.path` order — so a Spider2 clone that exists but has no checkers
  installed would otherwise shadow the mirror forever.
- **`SPIDER2_EXAMPLES`** overrides the fixture directory that
  `dfc_check_namegate.py` derives from its own file depth
  (`dfc_check_namegate.py:57-65`). That derivation
  is correct only at the canonical depth; from `dfc/` it walks outside the repo and
  every check returns *"pristine fixture not found"*. When the fallback fires, the
  runner sets this itself from its own `EXAMPLES` constant, via `setdefault` so an
  explicit value still wins (`run_task.py:189-192`).

**So, to run from a fresh clone:** clone `xlang-ai/Spider2` to `<repo>/Spider2` and
that is all. It must be at exactly that path — `run_task.py:48-59` derives the task
JSONL, the fixtures, the gold spec and `score_run.py` from `SPIDER_ROOT/Spider2`, so
the clone location is not negotiable for *running* a task (only the checkers are
relocatable). You do **not** need to install the checkers into that clone: the loader
falls back to `dfc/` and points the name-gate at the right fixtures automatically.
Set `SPIDER2_EXAMPLES` yourself only if your fixtures live somewhere else.

**The one thing still broken: the default `--pi` path is stale.** `run_task.py:61` is
`DEFAULT_PI = ~/Desktop/DAPLab/pi/pi-test.sh`. That path no longer exists; the tree
moved and the launcher is now at `~/Desktop/Desktop - Aaditya's MacBook Pro/DAPLab/pi/pi-test.sh`
(a sibling of `spider/`). Every run must pass `--pi` explicitly until this is fixed.
It is the same stale-absolute-path bug class that commit `8b422b3` cleared out of the
readers and drivers; this one instance survives because it points *outside* the repo
and cannot be derived from `__file__`.

---

## 1. What the harness IS

### The flow, end to end

One invocation of `run_task.py` drives exactly one (task × model) cell to a scored 0/1.

```
prepare_run_dir()          copy pristine fixture  →  <runs_root>/<exp>/<instance>/
        ↓                  run_task.py:297-311
resolve_produced_db()      decide which .duckdb will be scored, BEFORE the agent runs
        ↓                  run_task.py:96-129, called at :550
PiRpcSession.start()       spawn `pi --mode rpc -a --provider … --model … --tools …`
        ↓                  run_task.py:339-367, cwd = the run dir
drive_round(round 0)       send the task as a `prompt`, tee every event to
        ↓                  trajectory.jsonl, stop on `agent_settled`
        ↓                  run_task.py:425-475
[optional DFC loop]        checker(produced_db) → violation? → retry_message() →
        ↓                  drive_round(round N) in the SAME session → re-check
        ↓                  run_task.py:578-614
sess.close()               close stdin; Pi exits.  run_task.py:400-422, called :616
        ↓
score()                    subprocess → score_run.py → eval_utils.duckdb_match
        ↓                  run_task.py:478-490
run_record.json            everything above, serialized.  run_task.py:624-652
```

### Where each piece lives

| Piece | Path | Ours or upstream? |
|---|---|---|
| Orchestrator | `pi_runner/run_task.py` | **ours** |
| dbt PATH shim (nudge arms) | `pi_runner/nudge_bin/dbt` | **ours** |
| Batch drivers | `pi_runner/run_*.sh`, `repeat_runs.sh` | **ours** |
| Post-hoc analysis | `pi_runner/analyze_nudge.py`, `analyze_runs.py`, `nudge_validity.py`, `inspect_trajectory.py` | **ours** |
| Scorer wrapper | `Spider2/methods/spider-agent-dbt/score_run.py` | **ours** (dropped into the upstream clone) |
| DFC checkers | `Spider2/methods/spider-agent-dbt/spider_agent/agent/dfc_check*.py` | **ours** (same) |
| Official scoring fn | `Spider2/spider2-dbt/evaluation_suite/eval_utils.py` :: `duckdb_match` | upstream, untouched |
| Task fixtures + gold | `Spider2/spider2-dbt/examples/`, `…/evaluation_suite/gold/` | upstream, untouched |
| Stock Spider agent | `Spider2/methods/spider-agent-dbt/run.py`, `spider_agent/agent/agents.py` | upstream — **we do not use this path at all** |

Provenance was established with `git ls-files --error-unmatch` *inside the Spider2
clone* (`git config --get remote.origin.url` → `https://github.com/xlang-ai/Spider2.git`):
files it tracks are upstream, files it does not are ours.

### The scoring seam, precisely

`score()` (`run_task.py:530-542`) shells out to `score_run.py` and parses its JSON on
stdout. `score_run.py:173` calls the **official** `duckdb_match` — we did not
reimplement scoring. Two things it adds on top:

- **PASS integrity** (`score_run.py:180-186`): every scored table must exist *and*
  have >0 rows, else the verdict is `SUSPECT_PASS`. A silent no-op build can never
  be recorded as success.
- **FAIL diagnostics** (`score_run.py:85-119`): per-checked-column matched/unmatched,
  replicating `compare_pandas_table`'s any-match semantics (`score_run.py:122-146`).
  This is what produces the `[6]amount` / `[37]active_months_to_date` style findings.

**The checkers never score.** Stated at `dfc_check.py:14-15` and enforced
structurally: no checker opens the gold DuckDB or the eval spec.

---

## 2. What we modified, vs stock Pi / stock Spider

Stock Spider2's agent (`methods/spider-agent-dbt/run.py`) is bypassed entirely — we
drive Pi ourselves. Stock Pi is unmodified; everything we do is invocation flags,
environment, and the RPC event loop. Here is the full list, each with what breaks
without it.

### 2.1 Terminate on `agent_settled`, never `agent_end`

**What.** The event loop records `agent_end` for its usage/final-text payload but does
*not* stop on it (`run_task.py:509-516`). It breaks only on `agent_settled`
(`run_task.py:517-519`).

**Why.** Pi's own documented Python example in `docs/rpc.md` breaks on `agent_end`,
and that is wrong here. `agent_end` marks one *low-level* run and may be followed by
an automatic retry, a compaction-and-continue, or a queued follow-up.
`docs/pi_dbt_integration_plan.md:76-82` spells out the consequence: terminating on
`agent_end` scores a half-built dbt project whenever compaction fires mid-run — "a
silent, intermittent, and very confusing failure mode." Our runs are long (up to 85
turns observed), so compaction is not hypothetical.

**The corollary that makes the DFC loop possible.** `agent_settled` is a *quiescence*
signal, not a termination signal — the Pi process stays alive and accepts more
prompts; only closing stdin ends it (`run_task.py:24-25`, `:452-453`). That is
exactly why a DFC retry can be delivered into the **same** session with context and
workspace intact (`run_task.py:16-18`, verified).

**Where.** `run_task.py:517-519`; rationale `docs/pi_dbt_integration_plan.md:74-82`,
checklist item at `:304-306`.

### 2.2 Fixture copy per run, never pre-patched

**What.** `prepare_run_dir()` (`run_task.py:349-363`) copies
`Spider2/spider2-dbt/examples/<instance_id>/` to a fresh
`<runs_root>/<experiment_id>/<instance_id>/` with `shutil.copytree(src, dst, symlinks=True)`
(`:362`). Refuses to clobber without `--force` (`:355-360`).

**Why not pre-patch.** The `order_data` compile blocker in `dbt_project.yml` **is part
of the task** (`run_task.py:8-10`). Pre-patching it would hand the agent a working
project and silently delete a real part of the work — and it is a part agents solve
in more than one way. `analyze_runs.py:11-22` documents two distinct valid fixes
(comment out `order:` + uncomment `recharge_order_identifier:`, or set
`recharge__using_orders: true` so the `var('orders')` branch never evaluates
`ref('order_data')`), and notes that an earlier version of that function knew only the
first and **mislabelled a passing run as NOT-FIXED**.

**Why per-run.** Runs mutate the project (agents edit `models/**/*.yml`, write `.sql`,
materialize into the DuckDB). A shared dir would leak state between trials and destroy
the seed sweeps.

**Where.** `run_task.py:349-363`, docstring `:8-10`; fix-route taxonomy
`pi_runner/analyze_runs.py:11-22`.

### 2.3 Metadata lives outside the task dir

**What.** `trajectory.jsonl`, `pi.stderr.log`, `sessions/`, and `run_record.json` go to
`<runs_root>/<exp>/_pi_meta/<instance>/`, a sibling of the project dir
(`run_task.py:594-601`, `:700`).

**Why.** Two reasons, stated at `run_task.py:592-593`: the agent must not be able to
read its own trajectory or session files, and they must not pollute the workspace the
scorer and the name-gate read. The name-gate in particular compares the run dir's
`.sql` files against the pristine fixture; stray harness files there would corrupt it.

### 2.4 Tool enablement

**What.** `DEFAULT_TOOLS = "read,bash,edit,write,grep,find,ls"` (`run_task.py:67`),
passed as `--tools` (`:401`, `:554`).

**Why.** Only `read,bash,edit,write` are active in Pi by default. dbt projects have
hundreds of files across `models/`, `macros/`, `dbt_packages/` — without `grep`/`find`
the agent cannot navigate (`docs/pi_dbt_integration_plan.md:120`). This is not
cosmetic: the baseline analysis shows the *passing* route on the 1041-line
`models/shopify.yml` is grep-then-seek (`grep {"pattern":"discount","glob":"models/**/*.yml"}`
→ `read offset:847`), which is impossible without the grep tool.

### 2.5 `-a` / `--approve` is mandatory

**What.** Hardcoded into the command line at `run_task.py:400`.

**Why.** In rpc/json/print mode there is no UI, so the trust prompt cannot be
answered; without `-a`, project-local settings and extensions are **silently ignored**
(`docs/pi_dbt_integration_plan.md:119`, checklist `:281-285`). Silently — which is why
it is hardcoded rather than left to a flag someone can forget.

### 2.6 AWS / Bedrock wiring

**What.** `build_env()` (`run_task.py:370-388`) sets three things on the Pi subprocess
env:

| var | line | why |
|---|---|---|
| `AWS_PROFILE` | `:380` | Pi does **not** fall back to the `[default]` profile in `~/.aws/credentials`. Without it Pi sees **zero** Bedrock models. |
| `AWS_REGION` | `:381` | Region routing; `--aws_region` defaults to `us-east-1` (`:560`). Per-model routing works — this is how the Qwen arm was to run on `us-east-2`. |
| `PATH` prepend | `:382` | Pi's bash tool inherits this env. Without the conda bin on PATH the agent **cannot find `dbt`**. |

`build_env` sets no secrets (`:377`). The model is a **bare inference-profile ID**,
not an ARN (`run_task.py:65`) — an opaque application-inference-profile ARN is what
blocks the Qwen arm, because Pi cannot match it to a catalog entry and falls back to a
wrong token ceiling (`docs/qwen_arm_blocker.md`).

### 2.7 RPC, not `--mode json`

**What.** `cmd = [pi_path, "--mode", "rpc", "-a", …]` (`run_task.py:400`).

**Why.** `--mode json` is one-shot: prompt in, events out, exit. RPC keeps the process
alive and accepts further commands, which is the entire foundation for mid-run
steering (`docs/pi_dbt_integration_plan.md:86-92`). Without it the DFC retry loop would
need a fresh process — and therefore a cold context and a re-read of the workspace.

**Framing detail worth knowing.** stdout is read in **binary** and split on `b"\n"`
only (`run_task.py:394-396`, `:421-426`), because Python text mode rewrites lone `\r`
inside JSON payloads and would corrupt events. Reading runs on a background thread
into a queue (`:419`) so the deadline can be enforced independently (`:432-450`).

### 2.8 Produced-DB resolution mirrors the scorer

**What.** `gold_db_name()` reads the filename from the gold spec's
`evaluation.parameters.gold` (`run_task.py:82-99`); `resolve_produced_db()` globs it
under the run dir excluding `/gold/`, shallowest path first (`:102-135`). Resolved
*before* the agent starts (`:602`).

**Why.** It is deliberately the *same* algorithm as `score_run.py:52-58` so the
checker and the scorer can never disagree about which file they are looking at
(`run_task.py:71-80`). The predecessor was a hardcoded `{instance_id → filename}` map
that defaulted every unmapped task to `recharge.duckdb` and silently handed the
checker a nonexistent path — confirmed on `tpch001`, whose real db is `tpch.duckdb`.

It now **fails loudly and never guesses**: no hits → `SystemExit` listing what
`.duckdb` files actually exist (`:113-121`); ambiguous shallowest tie → `SystemExit`
rather than an undefined tie-break (`:123-130`).

### 2.9 The prompt scaffold

**What.** `ORIENTATION` (`run_task.py:143-149`): three sentences — you are in a dbt
project using DuckDB, cwd is the project root; the task; make sure `dbt run` succeeds
and your model is materialized.

**Why minimal, and why recorded.** It affects comparability against the old harness,
so it is kept deliberately minimal and written verbatim into the run record as
`prompt_scaffold` and `prompt_sent` (`:683-684`). A proposed but **unapplied** change
sits at `pi_runner/scaffolds/CANDIDATE_yaml_navigation.md`, pending a team decision —
deliberately not silently adopted, because it would break comparability with every
existing run.

### 2.10 The `--harness-nudge` PATH shim

**What.** `--harness-nudge {off,generic,specific}` (`run_task.py:575-580`) prepends
`pi_runner/nudge_bin/` to the agent's PATH (`:383-387`) so `dbt` resolves to our shim
(`pi_runner/nudge_bin/dbt`). The shim execs the real dbt, passes output and exit code
through unchanged, and appends a `HARNESS NOTICE` block if the output contains a
warning or error (`nudge_bin/dbt:103-115`).

**Why a PATH shim and not the runner.** This is the important architectural fact:
**the Pi runner cannot intercept tool results — Pi executes its bash tool internally**
(`nudge_bin/dbt:5-7`). PATH is the only lever the harness has over what a tool prints.

**The no-leakage rule.** `nudge_bin/dbt:9-14`: the appended text is derived *only*
from what the real dbt just printed. The shim never reads gold, the eval spec, a
checker, or any knowledge of which model is graded. `generic` is task-agnostic and
contains no names (`:47-59`); `specific` quotes dbt's own missing-node warning
de-jargonised (`:78-106`), falling back to the first error line so the *trigger* stays
identical across arms (`:97-104`).

### 2.11 Driver scripts

`pi_runner/run_matrix.sh` (model × task matrix), `run_namegate_sweep.sh` (N=10 matched
policy-off vs policy-on), `run_stack_sweep.sh` (stacked policies), `run_nudge_arms.sh`
and `run_nudge_family.sh` (three-arm nudge experiment), `run_arms.sh` (scaffold arms),
`repeat_runs.sh` (N repeats of one cell).

Two shared properties worth knowing:

- **Resumable.** A cell is skipped if its `stdout.json` parses with a non-null score
  (`run_namegate_sweep.sh:20-25`, applied at `:31`). The nudge drivers go further: "done" means
  `nudge_validity.py` classifies the record **VALID**, not merely that a file exists
  (`run_nudge_arms.sh:8`), so a DNS outage cannot be recorded as a model failure.
- **Root derived from `${BASH_SOURCE[0]}`**, not hardcoded (as of `8b422b3`). The two
  `status_of()` helpers pass that root into their quoted heredocs as `argv[2]`
  (`run_nudge_arms.sh:33`, used at `:36`) — the heredocs are `<<'PYX'`, so the shell cannot expand
  into them.

---

## 3. The DFC checkers

### 3.1 The contract

Every checker is a module under `spider_agent/agent/` exposing two functions.

**`check_*(produced_db_path) -> dict`**

```python
{"status": "pass" | "violation" | "error",
 "violations": [ {...policy-specific...} ],
 "checked_rows": int,
 "message": str}
```

Stated at `dfc_check.py:26-31`; the namegate adds
`undeclared_built` / `declared_unbuilt` / `built_candidates`
(`dfc_check_namegate.py:95-100`).

The three statuses are **not** two-plus-a-nuisance:

- `pass` — invariant holds.
- `violation` — invariant broken, and we have enough to say how. **Only this steers.**
- `error` — the checker could not evaluate. Target table not materialized
  (`dfc_check.py:37-39`), a source table missing (`:40-43`), a join returning zero rows
  (`:90-92`). This is the status that makes stacking order matter (§3.4).

**`retry_message(verdict) -> str`** — turns a violation verdict into the follow-up
prompt. It must name the specific offending rows/names; the whole design bet is that
concrete feedback steers and vague feedback does not.

One wrinkle: the `recharge001` policy's message builder lives in the runner
(`run_task.py:305-331`) rather than beside its checker, because it was ported verbatim
from the old harness's `agents.py::SpiderAgent._dfc_retry_message`. The one deviation
is documented at `run_task.py:311-317`: the original ends "…then Terminate again",
referring to the old harness's `Terminate()` action, which Pi has no equivalent for.

### 3.2 How a checker plugs in

`_load_dfc_checker(name)` (`run_task.py:205-241`) is the registry: a name from
`DFC_POLICIES` (`:154`) → an `(checker, retry_message)` pair. Unknown names die at
`:228-229`, before any work. Resolution is delegated to `_import_agent_module()`
(`:157-202`), which prefers the Spider2 clone and falls back to the tracked `dfc/`
mirror (§0). Imports stay lazy, so an unused checker's dependencies are never loaded.

The four today: `recharge001` (discount-amount invariant), `idtype` (id columns must
keep their source integer type), `enum` (`line_item_type` inside the domain declared in
`models/recharge.yml`), `namegate` (task-agnostic: a created model must carry a
declared name).

### 3.3 How the retry loop consumes a violation

`run_task.py:630-666`. In plain terms:

1. Only runs if round 0 **settled** (`:631`) — never steer a timed-out run.
2. `verdict = checker(produced_db)` (`:634`), wrapped so a raising checker becomes
   `status:"error"` rather than killing the run (`:635-637`).
3. Every verdict is appended to `dfc_events` **and** written into the trajectory as a
   `_runner_dfc_check` record (`:638-644`), so the whole
   violation → retry → fix → pass sequence is auditable in one stream.
4. Anything other than `violation` breaks the loop (`:646-647`) — you cannot steer on
   `pass`, and you must not steer on `error`.
5. On violation: `msg = retry_message(verdict)` (`:651`) and `drive_round(...)` with
   id `dfc-<n>` into the **same session** (`:653`). A non-settling retry breaks the
   loop (`:655-656`).
6. Bounded by `--dfc-max-retries` (default 3, `:574`). If the cap is exhausted, the
   `for/else` at `:657-666` runs one final check so the terminal state is recorded.

`drive_round` brackets each round with `_runner_round_start` / `_runner_round_end`
markers in the trajectory (`:484-486`, `:524-526`).

### 3.4 Stacking

`--dfc-policy` takes a comma-separated list; `_load_dfc_stack()` (`run_task.py:244-302`)
validates it (unknown name `:265-268`, duplicate `:269-270`) and, for two or more,
returns a composite.

`composite_checker` (`:276-293`) evaluates **left to right and returns the first
violation**, tagged with `policy`. `composite_retry` (`:295-300`) dispatches to that
policy's own message builder.

Two properties that are easy to get wrong and are handled explicitly:

- **Order is significant** (`:254-257`). `namegate,recharge001` is correct because the
  discount checker returns `error` ("target table not materialized") on a run that
  built the model under an invented name — it cannot judge what does not exist.
- **An `error` from one policy does not mask the others** (`:259-260`, `:287-288`).
  Errors are skipped and the next policy is evaluated; the composite reports `error`
  only if *every* policy errored.

No loop change was needed for stacking: the existing loop already re-checks after every
retry, so a run can violate A, be steered, violate B on the next check, and be steered
again — all inside one Pi session (`:248-252`). Retries are bounded across the whole
stack, not per policy.

---

## 4. If you're extending this

### Adding a new policy

Four edits, in this order:

1. **Write the checker** as `spider_agent/agent/dfc_check_<name>.py`, next to the
   existing ones. Export `check_*(produced_db_path)` returning the §3.1 dict, and a
   `retry_message(verdict)`. Give it a `__main__` block that takes a db path and prints
   the verdict (`dfc_check.py:100-105`) — you will want it while iterating.
2. **Register the name** in `DFC_POLICIES` (`run_task.py:154`).
3. **Add the import branch** in `_load_dfc_checker()` (`run_task.py:231-241`).
4. **Mirror it into `dfc/spider_agent/agent/`** so it actually ships (§0). Keep the
   two copies byte-identical — if the checker needs a path that differs by location,
   use an env override in *both* copies, the way `SPIDER2_EXAMPLES` does.

Nothing in `_load_dfc_stack()`, the retry loop, or the drivers needs to change — a new
policy composes with the existing ones for free.

**Two design rules the existing checkers hold to, worth keeping.** Never read gold or
the eval spec — the namegate derives "what should have been built" purely from the
pristine fixture (`dfc_check_namegate.py:17-25`). And read the *pristine* fixture, not
the run dir, for anything spec-like: agents edit schema YAML, and reading the run dir
would let an agent legalise its own invented name by adding it to the spec
(`dfc_check_namegate.py:27-33`).

### Where an intercept-before-materialization hook would go

Today **every** DFC check is post-hoc: the checker runs after `agent_settled`, against
an already-materialized DuckDB (`run_task.py:634`). Nothing stops a bad model from
being built; we detect it afterwards and ask for a rebuild.

For a true pre-materialization intercept, the constraint is the one stated at
`nudge_bin/dbt:5-7`: **Pi executes its bash tool internally, so the runner cannot
intercept tool calls or their results.** That leaves three seams, in increasing order
of invasiveness:

1. **The PATH shim** — `pi_runner/nudge_bin/dbt`. This is already a
   `dbt run` interceptor: it has the full argv and the real binary
   (`nudge_bin/dbt:104-108`). A gate would inspect argv/project state *before*
   `subprocess.run([real] + argv)` at `:114` and could refuse to exec, returning a
   non-zero code with an explanation on stdout. This is the only seam that sits
   genuinely before materialization and needs no Pi change. Note the shim currently
   uses `capture_output=True`, so it buffers rather than streams — a long `dbt run`
   prints nothing until it finishes.
2. **The `steer` RPC command** — `{"type":"steer","message":"..."}`, documented at
   `docs/pi_dbt_integration_plan.md:63` and **not used in v1**. It injects mid-run
   without waiting for `agent_settled`. `PiRpcSession.send()` (`run_task.py:428-430`)
   already accepts arbitrary command objects, so this is a few lines — but the runner
   would need a second thread watching events, since `drive_round`'s loop currently
   blocks until settle.
3. **A Pi-side tool-result hook** — the clean answer, and the only one that
   generalizes past `dbt`. Not available today; it is a change to a shared dependency,
   the same category as the Qwen `models.json` fix (`docs/qwen_arm_blocker.md`).

If the goal is specifically "don't let a wrongly-named model materialize," option 1 is
the honest fit and the name-gate's logic drops into it almost unchanged — it already
derives everything from the project directory, which the shim can see.

---

## 5. Reading a run afterwards

`run_record.json` (`run_task.py:676-704`) is the unit of analysis. The fields that
carry the most weight:

| field | line | note |
|---|---|---|
| `score` / `verdict` | `run_task.py:697-698` | from `score_run.py`; the only authoritative result |
| `score_report.all_targets_materialized_nonempty` | `score_run.py:182` | "target built" |
| `dfc_events` | `run_task.py:689` | one entry per check, with status and message |
| `dfc_retries_used` | `run_task.py:690` | counts rounds > 0 |
| `settled` / `timed_out` | `run_task.py:691-692` | `settled` requires **all** rounds to settle |
| `prompt_sent` / `prompt_scaffold` | `run_task.py:683-684` | verbatim, for comparability |
| `run_dir` / `produced_db` / `trajectory` | `run_task.py:680` | **absolute paths written at run time** |

That last row is the trap that cost us a full analysis cycle. Those paths are absolute
and go stale the moment the tree moves. `pi_runner/analyze_nudge.py` and
`nudge_validity.py` now resolve artifacts from the on-disk layout under `runs_root`
instead (`analyze_nudge.py::resolve_run_paths`), and a run whose artifacts cannot be
located is reported `UNREADABLE` and excluded from every denominator rather than
silently counted as a clean zero. If you write a new reader, derive paths from
`<runs_root>/<exp>/` — do not trust the stored ones.

---

## 6. Open items

- ~~Ship the three missing checkers~~ — **done**. All four are
  mirrored into `dfc/`, the loader falls back to them, and `SPIDER2_EXAMPLES` makes the
  name-gate find its fixtures from there. Verified from a clone-shaped tree with no
  Spider2 present: all four policies resolve from the mirror and the gate flags known
  wrong-name runs while passing known-correct ones.
- **`DEFAULT_PI` is stale** (`run_task.py:61`, §0). Cannot be derived from `__file__`
  since the launcher lives outside the repo; wants an env var
  (`PI_BIN`) with a loud failure when unset, rather than a wrong default.
- **`run_arms.sh:20`** exports a `SCAFFOLD` path into a deleted tmp scratchpad. Arm B
  is broken until that file has a real home.
- **`status_of()` swallows stderr** (`run_nudge_arms.sh:33`, `run_nudge_family.sh:43`):
  `2>/dev/null || echo MISSING` still converts any future breakage into a false
  `MISSING`, which would make a resumable driver re-run a finished batch.
- **Retry cap does not scale with stack depth** — `--dfc-max-retries` defaults to 3
  across the whole stack (`run_task.py:574`); a deeper stack would truncate silently.
