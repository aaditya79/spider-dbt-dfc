#!/usr/bin/env python
"""Drive one spider2-dbt task through the Pi harness to a scored 0/1.

Implements milestone steps 4-6 of docs/pi_dbt_integration_plan.md, plus the
optional DFC policy loop (--dfc-policy).

Flow per run:
  1. Copy the PRISTINE fixture examples/<instance_id>/ to a fresh run dir.
     The fixture is copied AS-IS and deliberately NOT patched -- the
     `order_data` compile blocker in dbt_project.yml is part of the task.
  2. Spawn `pi --mode rpc` with cwd = the run dir.
  3. Send the task instruction as a `prompt` command.
  4. Read the JSONL event stream, tee it to trajectory.jsonl, drive to
     `agent_settled`.
  5. If --dfc-policy is set: run the policy checker on the produced DuckDB.
     On violation, send violation-specific feedback as a follow-up `prompt`
     into the SAME rpc session (context and workspace survive `agent_settled`
     -- verified) and drive another agent_start..agent_settled cycle. Loop
     until the checker passes or --dfc-max-retries is hit.
  6. Score with score_run.py (authoritative duckdb_match). The checker only
     STEERS; it never decides pass/fail.
  7. Emit a run record.

`agent_settled` is a QUIESCENCE signal, not a termination signal: the Pi
process stays alive and accepts further prompts. Only closing stdin ends it.

Run dir layout matches score_run.py's expectations exactly:
    <runs_root>/<experiment_id>/<instance_id>/
"""

import argparse
import glob
import importlib
import importlib.util
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time

# ---------------------------------------------------------------- paths ----

HERE = os.path.dirname(os.path.abspath(__file__))
SPIDER_ROOT = os.path.dirname(HERE)             # repo root, wherever it lives
SPIDER2 = os.path.join(SPIDER_ROOT, "Spider2")
DBT_DIR = os.path.join(SPIDER2, "spider2-dbt")
EXAMPLES = os.path.join(DBT_DIR, "examples")
TASKS_JSONL = os.path.join(EXAMPLES, "spider2-dbt.jsonl")
METHODS_DBT = os.path.join(SPIDER2, "methods", "spider-agent-dbt")
# Tracked mirror of the DFC checkers. Spider2/ is gitignored (7.1 GB upstream clone),
# so on a fresh clone METHODS_DBT does not exist and the checkers would be
# unimportable even though they ship here. See _import_agent_module().
DFC_MIRROR = os.path.join(SPIDER_ROOT, "dfc")
SCORE_RUN = os.path.join(METHODS_DBT, "score_run.py")
GOLD_DIR = os.path.join(DBT_DIR, "evaluation_suite", "gold")
EVAL_JSONL = os.path.join(GOLD_DIR, "spider2_eval.jsonl")

def _find_pi():
    """First existing pi launcher: $PI_BIN, then known clone locations.

    The old single hardcoded path (~/Desktop/DAPLab/pi/pi-test.sh) went stale
    when the Desktop moved; every driver then had to pass --pi. Unresolved ->
    the old path is returned so the error message still names something.
    """
    cands = [os.environ.get("PI_BIN")] + [os.path.expanduser(p) for p in (
        "~/Desktop/Desktop - Aaditya’s MacBook Pro/DAPLab/pi/pi-test.sh",
        "~/Desktop/DAPLab/pi/pi-test.sh",
        "~/pi/pi-test.sh",
    )]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return cands[1]


DEFAULT_PI = _find_pi()
# Tracked copy of the ~/.pi/agent/models.json entries this harness needs (Qwen's
# application-inference-profile ARN with its real 65536 max-out). Pi only reads
# the copy in its agent dir, so this is an install artefact; check_models_json()
# refuses to start an ARN-model run without it rather than fail 100% silently.
PI_MODELS_JSON = os.path.join(HERE, "pi_models.json")
# Pi extension loaded per run (-e). Sanitizes tool names in history (prevents the
# Bedrock 400 that killed 3/15 Qwen cells), repairs whitespace-mismatched edit
# oldText / newText-only edits, and lifts the read cap. See the file header.
PI_EXTENSION = os.path.join(HERE, "pi_ext", "dbt_harness.ts")
DEFAULT_CONDA_BIN = os.path.expanduser("~/miniconda3/envs/spider2/bin")
DEFAULT_RUNS_ROOT = os.path.join(SPIDER_ROOT, "runs", "pi")

DEFAULT_MODEL = "us.anthropic.claude-opus-4-8"   # bare inference-profile ID, NOT the ARN
DEFAULT_PROVIDER = "amazon-bedrock"
DEFAULT_TOOLS = "read,bash,edit,write,grep,find,ls"  # grep/find/ls are OFF by default in Pi


# ------------------------------------------------------ produced-db lookup ----
# The DuckDB a task materializes into is NOT hardcoded here. It is resolved the
# same way the authoritative scorer resolves it, so the DFC checker and
# score_run.py can never disagree about which file they are looking at:
#   1. the gold spec's `parameters.gold` names the filename
#      (score_run.py :: load_gold_spec)
#   2. that filename is globbed recursively under the run dir, excluding any
#      gold/ path, shallowest path first (score_run.py :: find_pred_db)
# A previous hardcoded {instance_id -> filename} map defaulted every unmapped
# task to "recharge.duckdb", which silently handed the checker a nonexistent
# path (confirmed on tpch001, whose real db is tpch.duckdb).

def gold_db_name(instance_id):
    """Filename of the DuckDB this task is scored on, from the gold spec."""
    with open(EVAL_JSONL) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("instance_id") == instance_id:
                try:
                    return rec["evaluation"]["parameters"]["gold"]
                except KeyError as e:
                    raise SystemExit(
                        f"gold spec for {instance_id!r} in {EVAL_JSONL} has no "
                        f"evaluation.parameters.gold (missing key {e})")
    raise SystemExit(
        f"no gold spec for instance_id {instance_id!r} in {EVAL_JSONL} -- the "
        f"task is not scorable, so there is no produced DuckDB to resolve")


def resolve_produced_db(run_dir, instance_id):
    """Absolute path of the DuckDB the run produces. Fails loudly, never guesses.

    Mirrors score_run.py :: find_pred_db, including its shallowest-first
    tie-break, so the checker and the scorer always agree on the file.
    """
    name = gold_db_name(instance_id)
    hits = [h for h in glob.glob(os.path.join(run_dir, "**", name), recursive=True)
            if "/gold/" not in h]
    hits.sort(key=lambda p: len(p))        # identical key to find_pred_db

    if not hits:
        present = sorted(glob.glob(os.path.join(run_dir, "**", "*.duckdb"),
                                   recursive=True))
        raise SystemExit(
            f"cannot resolve produced DuckDB for {instance_id!r}: the gold spec "
            f"names {name!r} but no such file exists under {run_dir}.\n"
            f"  .duckdb files actually present: "
            f"{present if present else '(none -- nothing was materialized)'}\n"
            f"  refusing to fall back to a default filename.")

    shallowest = [h for h in hits if len(h) == len(hits[0])]
    if len(shallowest) > 1:
        raise SystemExit(
            f"ambiguous produced DuckDB for {instance_id!r}: {len(shallowest)} "
            f"files named {name!r} tie for the shallowest path under {run_dir}, "
            f"so the scorer's tie-break is undefined here.\n"
            f"  candidates: {shallowest}\n"
            f"  refusing to guess -- remove or rename the duplicates.")
    if len(hits) > 1:
        print(f"[runner] NOTE: {len(hits)} files named {name!r} under {run_dir}; "
              f"using the shallowest ({hits[0]}), matching score_run.py. "
              f"Others ignored: {hits[1:]}", file=sys.stderr)
    return hits[0]


# ------------------------------------------------------- prompt scaffolding ----
# Two scaffolds, selected by --scaffold and recorded verbatim in the run record
# (prompt_scaffold / system_prompt), because they affect comparability.
#
#   minimal : stock Pi system prompt + the short ORIENTATION user message. This
#             is what every run before branch spider_pi_2.0 used.
#   dbt     : the Spider-harness DBT_SYSTEM prompt ported to Pi -- same persona,
#             same project rules, but the ACTION SPACE rewritten around Pi's
#             native tools (bash/read/edit/write/grep/find/ls) instead of the
#             Thought:/Action: text protocol. Sent via `--system-prompt`, which
#             REPLACES Pi's default prompt. The user message is TASK_TEMPLATE.
#
# Rationale for `dbt`: the minimal scaffold says nothing about what tools exist
# or how to reach the database, and the Haiku e-comm traces show the model
# burning early turns on tools it assumes exist (a `duckdb` CLI, `read` on a
# directory). See pi_runner/scaffolds/CANDIDATE_yaml_navigation.md.
ORIENTATION = (
    "You are working in a dbt project that uses DuckDB. The current working "
    "directory is the project root.\n\n"
    "Task: {instruction}\n\n"
    "When you are done, make sure `dbt run` succeeds and your model is "
    "materialized into the project's DuckDB database."
)

# Pi appends "Current working directory: <cwd>" to a custom system prompt itself,
# so this text never hardcodes the path. Tool names below must match --tools.
DBT_SYSTEM_PROMPT = """\
You are a data scientist proficient in databases, SQL, and dbt projects.
You are starting in the root of a dbt project, which contains all the codebase needed for your task.
You solve the task by calling the tools below. Every step until the task is complete must be a tool call; keep any text between tool calls brief.

# TOOLS

The only tools are exactly these seven, by these exact names: bash, read, ls, find, grep, write, edit. A shell command is never a tool name -- `dbt run` or `python` go in the `command` argument of the bash tool.

- bash: run a non-interactive shell command in the project root. `dbt` and `python` are on PATH. Use it for `dbt deps`, `dbt run --profiles-dir .`, and for querying the database (see below).
- read: read one file; `path` is required. Files only -- for a directory use ls. Output is capped at about 50KB / 860 lines, so a long schema YAML gets cut off: list the declared models first with `grep -n "  - name: " models/*.yml`, then read the file with `offset` to reach the part you need.
- ls: list a directory. find: find files by glob. grep: search file contents.
- write: create a new file, or overwrite an existing file in full. Prefer write for a new model file or when replacing most of a file.
- edit: change an existing file by exact-text replacement. `oldText` must match the file byte-for-byte including whitespace, so read the file immediately before editing and keep each edit small; if an edit fails twice, use write instead.

Querying the DuckDB database: there is no `duckdb` CLI and no `python` tool in this environment. Run the Python module through the bash tool, e.g.

    python -c "import duckdb; print(duckdb.connect('<name>.duckdb').sql(\\"select * from my_table limit 10\\"))"

The database file is the `*.duckdb` in the project root; `profiles.yml` names it. Do not read the .duckdb file with the read tool.

Finishing: there is no terminate action. When the task is complete, stop calling tools and reply with a short summary naming the DuckDB file and the model(s) you materialized. Do not produce a CSV as the deliverable.

# DBT PROJECT RULES

1. First inspect dbt_project.yml, profiles.yml, models/, the YAML schema files, docs, and relevant SQL files.
2. The project is unfinished. Use the YAML model definitions to identify missing or incomplete model SQL files.
3. Do not modify YAML unless absolutely required. Prefer creating/fixing SQL models.
4. If packages.yml exists and dbt_packages/ is missing, run `dbt deps` before `dbt run`.
5. Run `dbt run --profiles-dir .` to execute the transformations. Do not pipe dbt run through grep, tail, head, tee, sed, or awk; the bash tool already truncates long output and saves the full log to a temp file.
6. Verify generated models where practical by querying the database, only after `dbt run` succeeds.
7. Do not finish until all required SQL models are complete according to the YAML and `dbt run` succeeds.
8. Do not use networking (the one exception is `dbt deps`, which fetches packages), privilege escalation, destructive system commands, or interactive editors.
"""

TASK_TEMPLATE = """\
# {instance_id}

## Task Description

{instruction}

## Environment

You are working with a dbt project that transforms data in a DuckDB database.
The project is the current working directory, with this typical structure:

- `dbt_project.yml` - dbt project configuration
- `profiles.yml` - DuckDB profile
- `models/` - SQL transformation models
- `*.duckdb` - Source database containing raw data and generated model outputs

## Objective

Complete or fix the dbt models to produce the correct table outputs.
Run `dbt run --profiles-dir .` to execute the transformations.

Safety note: operate only inside the local task sandbox. Do not attempt privilege escalation,
networking, or destructive actions.
"""

SCAFFOLDS = ("minimal", "dbt")
# Bump when DBT_SYSTEM_PROMPT / TASK_TEMPLATE wording changes, so runs stay
# comparable by (scaffold, scaffold_version) without diffing system_prompt.
#   1: 2026-09-16 first port of DBT_SYSTEM to Pi (ecom-v2-qwen-* batch)
#   2: 2026-09-17 tool-name discipline, no `python` tool, read needs path,
#      yml truncation guidance (grep -n then offset), write-over-edit advice
#   3: 2026-09-18 rule 8 names `dbt deps` as the one permitted network use
#      (it contradicted rule 4; fixtures without dbt_packages/ need deps)
SCAFFOLD_VERSION = 3


def build_prompts(scaffold, instance_id, instruction):
    """Return (user_prompt, system_prompt_or_None, scaffold_text_for_record)."""
    if scaffold == "minimal":
        return ORIENTATION.format(instruction=instruction), None, ORIENTATION
    if scaffold == "dbt":
        user = TASK_TEMPLATE.format(instance_id=instance_id, instruction=instruction)
        return user, DBT_SYSTEM_PROMPT, TASK_TEMPLATE
    raise ValueError(f"unknown scaffold {scaffold!r}")


# ------------------------------------------------------------- DFC policy ----

DFC_POLICIES = ("recharge001", "idtype", "enum", "namegate")


def _import_agent_module(modname):
    """Import `spider_agent.agent.<modname>`, preferring the Spider2 clone.

    Primary source is METHODS_DBT -- the canonical location, unchanged. It is only
    when that import FAILS that the tracked `dfc/` mirror is tried, so an installed
    Spider2 checker always wins and nothing about the existing path changes.

    The fallback exists because Spider2/ is gitignored: a fresh clone has the
    checkers (dfc/spider_agent/agent/) but not the tree they normally live in.

    The fallback loads the file DIRECTLY rather than re-running package resolution,
    because sys.path order is not enough to win. `spider_agent.agent` is a REGULAR
    package on the Spider2 side (it ships an __init__.py) while the mirror has none,
    so it is a namespace portion; when a regular package and a namespace portion both
    match, the regular package wins REGARDLESS of path order. A Spider2 clone that
    exists but has no checkers installed would therefore keep shadowing the mirror
    forever. Loading by file location sidesteps package semantics entirely, which is
    safe here: no checker imports another.
    """
    if METHODS_DBT not in sys.path:
        sys.path.insert(0, METHODS_DBT)
    full = f"spider_agent.agent.{modname}"
    try:
        return importlib.import_module(full)
    except ImportError as primary:
        path = os.path.join(DFC_MIRROR, "spider_agent", "agent", modname + ".py")
        if not os.path.isfile(path):
            raise SystemExit(
                f"cannot import checker {full!r} from either location:\n"
                f"  Spider2 clone : {METHODS_DBT}\n"
                f"  tracked mirror: {path} (absent)\n"
                f"  primary error : {type(primary).__name__}: {primary}")
        # The mirror copy of dfc_check_namegate derives its fixture directory from its
        # own file depth, which is wrong from dfc/. The runner already knows the right
        # one, so hand it over -- setdefault, so an explicit override still wins.
        os.environ.setdefault("SPIDER2_EXAMPLES", EXAMPLES)
        spec = importlib.util.spec_from_file_location(full, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[full] = mod          # so chk.__module__ resolves back to this
        try:
            spec.loader.exec_module(mod)
        except Exception as exc:
            del sys.modules[full]
            raise SystemExit(f"checker {full!r} failed to load from the tracked "
                             f"mirror {path}: {type(exc).__name__}: {exc}")
        return mod


def _load_dfc_checker(name):
    """Resolve a --dfc-policy name to its (checker, retry_message) pair.

    Checker logic is NOT reimplemented here -- each policy owns its module under
    spider_agent/agent/. All three share one contract: the checker takes a produced
    DuckDB path and returns {"status": "pass"|"violation"|"error", "violations": [...],
    "checked_rows": int, "message": str}; the retry builder turns a violation verdict
    into the follow-up prompt sent back into the SAME Pi session.

      recharge001 -- discount-amount invariant (Phase 2/3, the original policy).
                     Its retry text was ported verbatim into this file, so it is the
                     one policy whose message builder lives here rather than beside
                     the checker.
      idtype      -- charge_id/customer_id/address_id must keep the integer type of
                     the charge_data source columns (not be cast to string).
      enum        -- line_item_type must lie inside the domain declared for it in
                     models/recharge.yml.
      namegate    -- a model the agent creates must carry a name declared in the
                     project's schema YAML (task-agnostic; derives the spec from the
                     pristine fixture, never from gold).

    Every checker STEERS only; the final 0/1 always comes from score_run.py/duckdb_match.
    """
    if name not in DFC_POLICIES:
        raise SystemExit(f"unknown --dfc-policy {name!r} (known: {', '.join(DFC_POLICIES)})")

    if name == "recharge001":
        mod = _import_agent_module("dfc_check")
        return mod.check_recharge001_discounts, dfc_retry_message
    if name == "idtype":
        mod = _import_agent_module("dfc_check_idtype")
        return mod.check_recharge001_id_types, mod.retry_message
    if name == "namegate":
        mod = _import_agent_module("dfc_check_namegate")
        return mod.check_namegate, mod.retry_message
    mod = _import_agent_module("dfc_check_enum")
    return mod.check_recharge001_line_item_type, mod.retry_message


def _load_dfc_stack(spec):
    """Resolve a (possibly comma-separated) --dfc-policy spec to (checker, retry).

    A single policy behaves exactly as before. Two or more STACK: the composite
    checker evaluates them LEFT TO RIGHT and returns the FIRST violation, tagged with
    `policy`. The existing DFC loop already re-checks after every retry, so stacking
    needs no loop change: a run can violate policy A, be steered, then violate policy B
    on the next check and be steered again, all inside one Pi session. Retries are
    bounded by --dfc-max-retries across the whole stack, not per policy.

    Order is significant and is preserved as given. A policy whose precondition another
    policy establishes must come first: `namegate,recharge001` is correct, because the
    discount checker returns "error" (target table not materialized) on a run that built
    the model under an invented name -- it cannot judge what does not exist.

    An "error" from one policy does NOT mask the others: errors are skipped and the next
    policy is evaluated. The composite reports "error" only if every policy errored.
    """
    names = [n.strip() for n in spec.split(",") if n.strip()]
    if not names:
        raise SystemExit("--dfc-policy given an empty policy list")
    for n in names:
        if n not in DFC_POLICIES:
            raise SystemExit(f"unknown --dfc-policy {n!r} "
                             f"(known: {', '.join(DFC_POLICIES)})")
    if len(names) != len(set(names)):
        raise SystemExit(f"--dfc-policy lists a policy twice: {names}")

    pairs = [(n, *_load_dfc_checker(n)) for n in names]
    if len(pairs) == 1:
        return pairs[0][1], pairs[0][2]

    def composite_checker(produced_db):
        seen = []
        for name, chk, _ in pairs:
            try:
                v = dict(chk(produced_db), policy=name)
            except Exception as e:
                v = {"status": "error", "violations": [], "checked_rows": 0,
                     "message": f"checker raised: {type(e).__name__}: {e}", "policy": name}
            seen.append(v)
            if v["status"] == "violation":
                return dict(v, stack=names, evaluated=[x["policy"] for x in seen])
        if all(v["status"] == "error" for v in seen):
            return dict(seen[0], stack=names, evaluated=[x["policy"] for x in seen])
        return {"status": "pass", "violations": [],
                "checked_rows": sum(v.get("checked_rows", 0) or 0 for v in seen),
                "message": "; ".join(f"{v['policy']}: {v['message']}" for v in seen),
                "policy": "+".join(names), "stack": names,
                "evaluated": [x["policy"] for x in seen]}

    def composite_retry(verdict):
        fired = verdict.get("policy")
        for name, _, rt in pairs:
            if name == fired:
                return rt(verdict)
        raise SystemExit(f"no retry builder for policy {fired!r} in stack {names}")

    return composite_checker, composite_retry


def dfc_retry_message(verdict):
    """Violation-specific RETRY feedback for the `recharge001` discount policy.

    The `idtype` and `enum` policies supply their own retry_message() beside their
    checker; _load_dfc_checker() dispatches to whichever belongs to the policy in use.

    Ported verbatim from the old harness
    (spider_agent/agent/agents.py :: SpiderAgent._dfc_retry_message) with ONE
    deviation: the original ends "...then Terminate again", which refers to the
    old harness's Terminate() action. Pi has no such action, so that clause is
    replaced with a Pi-appropriate instruction to confirm the rebuild. The
    policy content -- the required transform and the named offending rows -- is
    unchanged.
    """
    rows = "; ".join(
        f"charge_id {v['charge_id']} ({v['title']}): found {v['amount_found']}, "
        f"expected {v['amount_expected']}"
        for v in verdict["violations"]
    )
    return (
        "DFC policy violation on recharge__charge_line_item_history: the `amount` for "
        "percentage discounts must be derived as round(value/100 * total_line_items_price, 2), "
        "not the raw discount value. The following rows have the raw value where the derived "
        f"amount is expected -> {rows}. Revise the model SQL (the discount CTE must convert "
        "percentage discounts using the charge's total_line_items_price) and rebuild with dbt run, "
        "then confirm the rebuilt table has the corrected amounts."
    )


# ---------------------------------------------------------------- helpers ----

def check_models_json(model):
    """Fail fast if `model` is an ARN that Pi has no metadata for.

    An application-inference-profile ARN carries no model name, so Pi cannot look
    it up in its catalog and sends max_completion_tokens from the wrong entry
    (128000 -> Qwen rejects every call). The fix is a models.json entry; Pi reads
    it from its agent dir only. See pi_runner/pi_models.json.
    """
    if not model.startswith("arn:"):
        return
    agent_dir = os.environ.get("PI_CODING_AGENT_DIR") or os.path.expanduser("~/.pi/agent")
    path = os.path.join(agent_dir, "models.json")
    try:
        with open(path) as f:
            ids = [m.get("id") for prov in json.load(f).get("providers", {}).values()
                   for m in prov.get("models", [])]
    except (OSError, ValueError):
        ids = []
    if model not in ids:
        raise SystemExit(
            f"model {model!r} is an inference-profile ARN but {path} does not define it, "
            f"so Pi would send the wrong max_completion_tokens and every call would fail.\n"
            f"  install: cp {PI_MODELS_JSON} {path}   (merge if you already have one)")


def load_instruction(instance_id):
    """Read the verbatim task instruction from spider2-dbt.jsonl."""
    with open(TASKS_JSONL) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("instance_id") == instance_id:
                return rec["instruction"]
    raise SystemExit(f"instance_id {instance_id!r} not found in {TASKS_JSONL}")


def prepare_run_dir(instance_id, runs_root, experiment_id, force=False):
    """Copy the pristine fixture to <runs_root>/<experiment_id>/<instance_id>/."""
    src = os.path.join(EXAMPLES, instance_id)
    if not os.path.isdir(src):
        raise SystemExit(f"no such fixture: {src}")
    dst = os.path.join(runs_root, experiment_id, instance_id)
    if os.path.exists(dst):
        if not force:
            raise SystemExit(
                f"run dir already exists: {dst}\n"
                f"use --force to overwrite, or pick a new --experiment_id")
        shutil.rmtree(dst)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copytree(src, dst, symlinks=True)
    return dst


NUDGE_BIN = os.path.join(HERE, "nudge_bin")   # harness `dbt` shim (see nudge_bin/dbt)
NUDGE_MODES = ("off", "generic", "specific")


def build_env(aws_profile, aws_region, conda_bin, nudge_mode="off"):
    """Environment for the Pi subprocess.

      * AWS_PROFILE / AWS_REGION -- Pi does NOT fall back to the [default]
        profile in ~/.aws/credentials; without these it sees zero Bedrock models.
      * PATH -- Pi's bash tool inherits this env, so the conda bin must be on
        PATH or the agent cannot find `dbt`.
    No secrets are set here.
    """
    env = dict(os.environ)
    env["AWS_PROFILE"] = aws_profile
    env["AWS_REGION"] = aws_region
    env["PATH"] = conda_bin + os.pathsep + env.get("PATH", "")
    # --harness-nudge: prepend the shim dir so the agent's `dbt` resolves to it.
    # The shim re-surfaces dbt's OWN diagnostics; it never sees gold or a checker.
    env["HARNESS_NUDGE_MODE"] = nudge_mode
    if nudge_mode != "off":
        env["PATH"] = NUDGE_BIN + os.pathsep + env["PATH"]
    return env


class PiRpcSession:
    """Spawn `pi --mode rpc` and drive it over JSONL stdin/stdout.

    Framing: strict LF-delimited JSONL. stdout is read in BINARY and split on
    b"\\n" only -- Python text mode would rewrite lone \\r inside JSON payloads.
    """

    def __init__(self, pi_path, cwd, provider, model, tools, env,
                 session_dir=None, extra_args=None):
        cmd = [pi_path, "--mode", "rpc", "-a",
               "--provider", provider, "--model", model, "--tools", tools]
        if session_dir:
            cmd += ["--session-dir", session_dir]
        if extra_args:
            cmd += list(extra_args)
        self.cmd = cmd
        self.cwd = cwd
        self.env = env
        self.proc = None
        self._q = queue.Queue()
        self.stderr_path = None

    def start(self, stderr_path):
        self.stderr_path = stderr_path
        self._stderr_fh = open(stderr_path, "wb")
        self.proc = subprocess.Popen(
            self.cmd, cwd=self.cwd, env=self.env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr_fh)
        threading.Thread(target=self._read_stdout, daemon=True).start()

    def _read_stdout(self):
        try:
            for raw in iter(self.proc.stdout.readline, b""):
                self._q.put(raw)
        finally:
            self._q.put(None)

    def send(self, obj):
        self.proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def events(self, deadline):
        """Yield (raw_line, parsed_or_None) until EOF or deadline."""
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError("wall-clock timeout waiting for Pi events")
            try:
                raw = self._q.get(timeout=min(remaining, 5.0))
            except queue.Empty:
                continue
            if raw is None:
                return
            line = raw.rstrip(b"\r\n")
            if not line:
                continue
            try:
                yield raw, json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                yield raw, None

    def close(self, grace=15):
        """Close stdin (Pi shuts down on stdin 'end'), then escalate."""
        if self.proc is None:
            return None
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            return self.proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                return self.proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                return self.proc.wait(timeout=grace)
        finally:
            try:
                self._stderr_fh.close()
            except Exception:
                pass


# Pi's RPC stream reports the same content several times per turn: an empty
# `message_start` placeholder, the full assistant message at `message_end`, the
# same message again inside `turn_end`, each tool result both at
# `tool_execution_end` and as a `toolResult`-role message pair, and finally the
# whole conversation again in `agent_end.messages`. trajectory.jsonl keeps ONE
# copy of each thing. Every reader in pi_runner/ (analyze_runs, analyze_nudge,
# inspect_trajectory) consumes only the kept types.
#
#   kept    : _runner_* markers, response (ack), agent_start, turn_start,
#             message_end for role user/assistant (carries content + usage),
#             tool_execution_start, tool_execution_end, agent_settled,
#             agent_end WITHOUT its `messages` replay
#   dropped : message_start, turn_end, message_end for role toolResult
TRAJ_DROP = {"message_start", "turn_end"}


def traj_record(ev):
    """Return the JSON-serialisable record to log for `ev`, or None to skip it."""
    t = ev.get("type")
    if t in TRAJ_DROP:
        return None
    if t == "message_end" and (ev.get("message") or {}).get("role") == "toolResult":
        return None                      # already logged as tool_execution_end
    if t == "agent_end":
        return {k: v for k, v in ev.items() if k != "messages"}
    return ev


# Bedrock Converse rejects any request whose history contains a toolUse.name
# outside this pattern. A model that emits e.g. toolName="dbt deps" poisons the
# session permanently: every later turn 400s, Pi records stopReason=error with
# willRetry=false, and the run settles with nothing built. Seen on 3/15 Qwen
# cells (2026-09-16). Detected here so the run record says HARNESS_ERROR instead
# of a silent 0.
TOOL_NAME_OK = __import__("re").compile(r"^[a-zA-Z0-9_-]+$")

USAGE_KEYS = ("input", "output", "cacheRead", "cacheWrite", "totalTokens")


def _add_usage(total, u):
    """Accumulate one assistant message's usage into `total` (in place)."""
    if not u:
        return total
    for k in USAGE_KEYS:
        total[k] = total.get(k, 0) + (u.get(k) or 0)
    cost = (u.get("cost") or {}).get("total") or 0.0
    total["cost_total"] = total.get("cost_total", 0.0) + cost
    total["turns"] = total.get("turns", 0) + 1
    return total


def _sum_usage(rounds):
    total = {}
    for r in rounds:
        for k, v in (r.get("usage") or {}).items():
            total[k] = total.get(k, 0) + v
    return total


def drive_round(sess, traj_fh, deadline, req_id, prompt_text, round_no, quiet,
                max_tool_calls=0):
    """Send one prompt and drive a full agent_start..agent_settled cycle.

    Events are teed to the shared trajectory file after de-duplication (see
    traj_record), bracketed by `_runner_round_*` marker records so the
    multi-round violation -> RETRY -> fix -> pass sequence stays auditable in
    one stream. Unparseable lines are logged raw so nothing is silently lost.

    `usage` is the SUM over every assistant message in the round (it used to be
    the last message only, which under-reported a 143-turn run as one turn).

    `harness_error` is set when the round ended for a reason that is not the
    model's doing: a malformed tool name that Bedrock will reject, an API/stream
    error (network, validation), or the `max_tool_calls` cap. The caller decides
    what that means for the verdict.

    `max_tool_calls` > 0 sends Pi an `abort` once the round has issued that many
    tool calls; the round then settles normally and is marked `capped`. Default
    0 = no cap (Pi has none of its own; the Spider harness capped at 30 steps).
    """
    marker = {"type": "_runner_round_start", "round": round_no,
              "req_id": req_id, "prompt": prompt_text}
    traj_fh.write((json.dumps(marker) + "\n").encode()); traj_fh.flush()

    r = {"round": round_no, "settled": False, "timed_out": False, "ack": None,
         "counts": {}, "tool_calls": [], "final_text": None, "usage": {},
         "harness_error": None, "capped": False}
    aborted = False

    sess.send({"id": req_id, "type": "prompt", "message": prompt_text})
    try:
        for raw, ev in sess.events(deadline):
            if ev is None:
                traj_fh.write(raw); traj_fh.flush()
                continue
            rec = traj_record(ev)
            if rec is not None:
                traj_fh.write((json.dumps(rec) + "\n").encode()); traj_fh.flush()
            t = ev.get("type")
            r["counts"][t] = r["counts"].get(t, 0) + 1
            if t == "response" and ev.get("id") == req_id:
                r["ack"] = ev
                if not ev.get("success"):
                    print(f"[runner] prompt REJECTED: {ev.get('error')}", file=sys.stderr)
                    break
            elif t == "tool_execution_start":
                name = ev.get("toolName") or ev.get("name") or ""
                r["tool_calls"].append({"tool": name, "args": ev.get("args")})
                if not quiet:
                    print(f"[runner]  r{round_no} tool: {name}", file=sys.stderr)
                if not TOOL_NAME_OK.match(name) and not r["harness_error"]:
                    r["harness_error"] = {"kind": "bad_tool_name", "tool_name": name,
                                          "after_tool_calls": len(r["tool_calls"])}
                    print(f"[runner] HARNESS: malformed tool name {name!r} -- Bedrock "
                          f"will reject every later turn of this session", file=sys.stderr)
                if max_tool_calls and len(r["tool_calls"]) >= max_tool_calls and not aborted:
                    aborted = True; r["capped"] = True
                    print(f"[runner] CAP: {max_tool_calls} tool calls reached, aborting round",
                          file=sys.stderr)
                    sess.send({"type": "abort"})
            elif t == "message_end":
                m = ev.get("message") or {}
                if m.get("role") == "assistant":
                    _add_usage(r["usage"], m.get("usage"))
                    if m.get("stopReason") == "error" and not r["harness_error"]:
                        r["harness_error"] = {"kind": "api_error",
                                              "message": (m.get("errorMessage") or "")[:500],
                                              "after_tool_calls": len(r["tool_calls"])}
                        print(f"[runner] HARNESS: API error -- {m.get('errorMessage')!s:.160}",
                              file=sys.stderr)
            elif t == "agent_end":
                msgs = ev.get("messages") or []
                if msgs:
                    for c in (msgs[-1].get("content") or []):
                        if c.get("type") == "text":
                            r["final_text"] = c.get("text")
            elif t == "agent_settled":
                r["settled"] = True
                break
    except TimeoutError as e:
        r["timed_out"] = True
        print(f"[runner] TIMEOUT in round {round_no}: {e}", file=sys.stderr)

    if r["capped"] and r["harness_error"] is None:
        r["harness_error"] = {"kind": "tool_call_cap", "cap": max_tool_calls,
                              "after_tool_calls": len(r["tool_calls"])}
    end_marker = {"type": "_runner_round_end", "round": round_no,
                  "settled": r["settled"], "timed_out": r["timed_out"],
                  "harness_error": r["harness_error"]}
    traj_fh.write((json.dumps(end_marker) + "\n").encode()); traj_fh.flush()
    return r


def score(instance_id, runs_root, experiment_id, python_bin):
    """Authoritative 0/1 from score_run.py -> eval_utils.duckdb_match."""
    proc = subprocess.run(
        [python_bin, SCORE_RUN, "--experiment_id", experiment_id,
         "--instance_id", instance_id, "--output_dir", runs_root],
        capture_output=True, text=True)
    if proc.returncode != 0:
        return {"score": 0, "verdict": "SCORER_ERROR",
                "stderr": proc.stderr[-2000:], "stdout": proc.stdout[-2000:]}
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return {"score": 0, "verdict": "SCORER_UNPARSEABLE", "stdout": proc.stdout[-2000:]}


# ------------------------------------------------------------------ main ----

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--instance_id", default="recharge001")
    ap.add_argument("--experiment_id", required=True)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--provider", default=DEFAULT_PROVIDER)
    ap.add_argument("--tools", default=DEFAULT_TOOLS)
    ap.add_argument("--runs_root", default=DEFAULT_RUNS_ROOT)
    ap.add_argument("--pi", default=DEFAULT_PI)
    ap.add_argument("--conda_bin", default=DEFAULT_CONDA_BIN)
    ap.add_argument("--python_bin", default=os.path.join(DEFAULT_CONDA_BIN, "python"))
    ap.add_argument("--aws_profile", default="default")
    ap.add_argument("--aws_region", default="us-east-1")
    ap.add_argument("--timeout", type=float, default=3600.0)
    ap.add_argument("--pi-extension", default=PI_EXTENSION,
                    help="Pi extension file passed as -e (default: pi_ext/dbt_harness.ts)")
    ap.add_argument("--no-pi-extension", action="store_true",
                    help="run stock Pi tools with no extension (pre-2026-09-18 behaviour)")
    ap.add_argument("--max-tool-calls", type=int, default=0,
                    help="abort a round after this many tool calls (0 = no cap; the "
                         "Spider harness capped at 30 steps). Marks the run HARNESS_ERROR "
                         "kind=tool_call_cap unless it passed anyway.")
    ap.add_argument("--prompt", default=None, help="override the instruction (smoke only)")
    ap.add_argument("--no-scaffold", action="store_true")
    ap.add_argument("--scaffold", default="dbt", choices=list(SCAFFOLDS),
                    help="'dbt' (default): Spider DBT_SYSTEM ported to Pi-native tools, "
                         "sent as --system-prompt, task via TASK_TEMPLATE. 'minimal': "
                         "stock Pi system prompt + ORIENTATION (pre-2.0 behaviour).")
    ap.add_argument("--no-score", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--dfc-policy", default="off",
                    help="DFC policy loop (steering only). One policy, or several "
                         "comma-separated to STACK them, evaluated left to right: "
                         f"{{off, {', '.join(DFC_POLICIES)}}}. Order matters -- put a "
                         "policy that must hold before others can be judged first "
                         "(e.g. 'namegate,recharge001': the discount checker cannot "
                         "evaluate a target table that was never built).")
    ap.add_argument("--dfc-max-retries", type=int, default=3)
    ap.add_argument("--harness-nudge", default="off", choices=list(NUDGE_MODES),
                    help="re-surface dbt's OWN warnings/errors more saliently by putting "
                         "a `dbt` shim on the agent's PATH. 'generic' appends a fixed "
                         "task-agnostic instruction to re-check schema files; 'specific' "
                         "echoes dbt's own missing-node warning de-jargonised. Neither "
                         "reads gold or any checker.")
    args = ap.parse_args()

    if args.dfc_policy == "off":
        args.dfc_policy = None

    t0 = time.time()
    checker, retry_message = (_load_dfc_stack(args.dfc_policy)
                              if args.dfc_policy else (None, None))

    run_dir = prepare_run_dir(args.instance_id, args.runs_root,
                              args.experiment_id, force=args.force)
    # Metadata lives OUTSIDE the task dir so the agent cannot see its own
    # trajectory/session files and they don't pollute the workspace.
    meta_dir = os.path.join(args.runs_root, args.experiment_id,
                            "_pi_meta", args.instance_id)
    if args.force and os.path.isdir(meta_dir):
        shutil.rmtree(meta_dir)
    os.makedirs(meta_dir, exist_ok=True)
    traj_path = os.path.join(meta_dir, "trajectory.jsonl")
    stderr_path = os.path.join(meta_dir, "pi.stderr.log")
    session_dir = os.path.join(meta_dir, "sessions")
    produced_db = resolve_produced_db(run_dir, args.instance_id)

    system_prompt = None
    if args.prompt is not None:
        prompt_text, instruction, scaffold_used = args.prompt, None, "<--prompt override>"
    else:
        instruction = load_instruction(args.instance_id)
        if args.no_scaffold:
            prompt_text, scaffold_used = instruction, None
        else:
            prompt_text, system_prompt, scaffold_used = build_prompts(
                args.scaffold, args.instance_id, instruction)
    pi_extra = ["--system-prompt", system_prompt] if system_prompt else []
    pi_extension = None if args.no_pi_extension else args.pi_extension
    if pi_extension:
        if not os.path.isfile(pi_extension):
            raise SystemExit(f"--pi-extension {pi_extension!r} does not exist")
        pi_extra += ["-e", pi_extension]
    pi_extra = pi_extra or None

    check_models_json(args.model)
    env = build_env(args.aws_profile, args.aws_region, args.conda_bin,
                    nudge_mode=args.harness_nudge)
    sess = PiRpcSession(args.pi, run_dir, args.provider, args.model,
                        args.tools, env, session_dir=session_dir, extra_args=pi_extra)

    print(f"[runner] run dir    : {run_dir}", file=sys.stderr)
    scaffold_name = (scaffold_used if scaffold_used in (None, "<--prompt override>")
                     else args.scaffold)
    print(f"[runner] scaffold   : {scaffold_name}"
          f"{' (+ --system-prompt)' if system_prompt else ''}", file=sys.stderr)
    print(f"[runner] extension  : {pi_extension or 'none (stock Pi tools)'}", file=sys.stderr)
    print(f"[runner] dfc policy : {args.dfc_policy or 'OFF'} "
          f"(max retries {args.dfc_max_retries})", file=sys.stderr)

    sess.start(stderr_path)
    deadline = t0 + args.timeout
    rounds, dfc_events = [], []

    with open(traj_path, "wb") as traj:
        # --- round 0: the task itself -----------------------------------
        rounds.append(drive_round(sess, traj, deadline, "req-1", prompt_text, 0, args.quiet,
                                  max_tool_calls=args.max_tool_calls))

        # --- DFC loop: check -> steer -> recheck ------------------------
        if checker and rounds[-1]["settled"]:
            for attempt in range(1, args.dfc_max_retries + 1):
                try:
                    verdict = checker(produced_db)
                except Exception as e:
                    verdict = {"status": "error", "violations": [], "checked_rows": 0,
                               "message": f"checker raised: {type(e).__name__}: {e}"}
                dfc_events.append({"round": attempt, "checked_after_round": attempt - 1,
                                   **verdict})
                print(f"[runner] DFC check after round {attempt-1}: "
                      f"{verdict['status']} -- {verdict['message']}", file=sys.stderr)
                traj.write((json.dumps({"type": "_runner_dfc_check",
                                        "after_round": attempt - 1, **verdict}) + "\n").encode())
                traj.flush()

                if verdict["status"] != "violation":
                    break   # pass, or error we cannot steer on
                if attempt > args.dfc_max_retries:
                    break

                msg = retry_message(verdict)
                print(f"[runner] DFC RETRY {attempt}/{args.dfc_max_retries}", file=sys.stderr)
                r = drive_round(sess, traj, deadline, f"dfc-{attempt}", msg, attempt, args.quiet,
                                max_tool_calls=args.max_tool_calls)
                rounds.append(r)
                if not r["settled"]:
                    break
            else:
                # retry cap exhausted -- record the final state
                try:
                    verdict = checker(produced_db)
                    dfc_events.append({"round": "final", "checked_after_round": len(rounds) - 1,
                                       **verdict})
                    traj.write((json.dumps({"type": "_runner_dfc_check",
                                            "after_round": len(rounds) - 1, **verdict}) + "\n").encode())
                except Exception:
                    pass

    exit_code = sess.close()
    wall = time.time() - t0

    score_report = None
    if not args.no_score:
        score_report = score(args.instance_id, args.runs_root,
                             args.experiment_id, args.python_bin)

    record = {
        "instance_id": args.instance_id, "experiment_id": args.experiment_id,
        "provider": args.provider, "model": args.model, "tools": args.tools,
        "dfc_policy": args.dfc_policy, "dfc_max_retries": args.dfc_max_retries,
        "run_dir": run_dir, "produced_db": produced_db, "trajectory": traj_path,
        "pi_stderr": stderr_path, "session_dir": session_dir,
        "harness_nudge": args.harness_nudge,
        "prompt_sent": prompt_text, "instruction_verbatim": instruction,
        "prompt_scaffold": scaffold_used,
        "scaffold": None if (args.prompt is not None or args.no_scaffold) else args.scaffold,
        "scaffold_version": SCAFFOLD_VERSION,
        "system_prompt": system_prompt,
        "pi_extension": pi_extension,
        "wall_clock_s": round(wall, 1), "pi_exit_code": exit_code,
        "n_rounds": len(rounds),
        "rounds": [{k: v for k, v in r.items() if k != "tool_calls"} for r in rounds],
        "n_tool_calls_total": sum(len(r["tool_calls"]) for r in rounds),
        "dfc_events": dfc_events,
        "dfc_retries_used": sum(1 for r in rounds if r["round"] > 0),
        "settled": all(r["settled"] for r in rounds) if rounds else False,
        "timed_out": any(r["timed_out"] for r in rounds),
        "harness_error": next((r["harness_error"] for r in rounds if r["harness_error"]), None),
        "usage_total": _sum_usage(rounds),
        "agent_final_text": rounds[-1]["final_text"] if rounds else None,
        "score_report": score_report,
    }
    if score_report is not None:
        record["score"] = score_report.get("score")
        record["verdict"] = score_report.get("verdict")
        # A harness error that ended the run before it could pass is not a model
        # failure. score=None makes the resumable drivers re-run the cell; a run
        # that passed despite a late harness error keeps its 1 (the work is on
        # disk and the scorer is authoritative).
        if record["harness_error"] and record["score"] != 1:
            record["verdict_scorer"] = record["verdict"]
            record["verdict"] = "HARNESS_ERROR"
            record["score"] = None

    rec_path = os.path.join(meta_dir, "run_record.json")
    with open(rec_path, "w") as f:
        json.dump(record, f, indent=2, default=str)
    record["run_record"] = rec_path
    print(json.dumps(record, indent=2, default=str))


if __name__ == "__main__":
    main()
