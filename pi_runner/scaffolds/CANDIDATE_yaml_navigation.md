# CANDIDATE scaffold — YAML navigation

**STATUS: CANDIDATE. NOT APPLIED.** The active scaffold in `run_task.py` (`ORIENTATION`)
is unchanged. Do not wire this in until the team agrees on one shared scaffold — this is
shared-harness territory now, and the scaffold directly determines whether Pi pass rates
compare to the Spider-harness numbers.

Drafted 2026-08-02. Rationale and evidence: memory `mode-b-comparability`.

---

## Why this exists

Mode B (4/10 runs built a wrongly-named model and scored 0) is a **navigation** failure, not
a missing-info failure:

- `models/recharge.yml` fully specifies the target — name, all 9 columns in gold order,
  descriptions, and a uniqueness test. It is the only file in the fixture that names it.
- Reading it predicted the outcome perfectly, 10/10: all 6 Mode A runs read it and built the
  right table; **none** of the 4 Mode B runs ever read it.

The old Spider harness (`spider_agent/agent/prompts.py`, `DBT_SYSTEM`) **never names the
target table** either — but it directs the agent to the YAML files **five separate times**.
Our current scaffold does so zero times. That makes the current Pi setup *weaker* than the
baseline, so today's 0/10 is not yet comparable to the team's numbers.

This candidate closes that gap and nothing more. **It does not name the target table**, so
discovery remains the agent's job and Mode B stays measurable.

---

## Current ACTIVE scaffold (unchanged, for reference)

```
You are working in a dbt project that uses DuckDB. The current working directory is the
project root.

Task: {instruction}

When you are done, make sure `dbt run` succeeds and your model is materialized into the
project's DuckDB database.
```

## PROPOSED scaffold

```
You are working in a dbt project that uses DuckDB. The current working directory is the
project root.

Task: {instruction}

This is an unfinished dbt project. Work as follows:
1. Review the project's YAML files to understand the task requirements and the database,
   and to identify which defined models are still incomplete or missing a SQL file.
2. Write the SQL needed to complete those models, following the definitions given in the
   YAML files. In most cases you should create new SQL files rather than modify existing
   ones.
3. Run `dbt run` and make sure it succeeds and your model is materialized into the
   project's DuckDB database.
4. Verify the models you produced in the database actually match their YAML definitions —
   do not assume the task is complete without checking.
```

---

## Mapping to the old harness's five YAML pointers

| Old harness `DBT_SYSTEM` | Proposed line |
|---|---|
| (3) "Solve the task by **reviewing the YAML files**, understanding the task requirements, understanding the database and identifying the SQL transformations needed" | 1 |
| (4) "The project is an **unfinished project** … **refer to the YAML file to identify which defined model SQLs are incomplete**" | preamble + 1 |
| (7) "do not easily assume the task is complete. You must complete all SQL queries **according to the YAML files**" | 2 + 4 |
| (8) "**verify** the new data models generated in the database **meet the definitions in the YAML files**" | 4 |
| (9) "you only need to **create new SQL files according to the YAML files**" | 2 |

Deliberately **not** carried over from `DBT_SYSTEM`:

- **The target table name** — never in the old harness's `system_message` or `Task` either.
  Adding it would go beyond the baseline and inflate our numbers relative to the team's.
- **"You must not attempt to modify the yml file" (point 5)** — see caveat below.
- **"Terminate with the DuckDB filename" (point 10)** — an artifact of the old harness's
  `Terminate()` action. Pi has no such action; the orchestrator reads the DuckDB from the
  run dir directly.
- **The 50-step `max_steps` limit** — the team explicitly wants step limits gone, and Pi has
  none. Not reintroducing it.
- Action-space / response-format sections — Pi's own tool schema covers these.

---

## Open caveat for the team

`DBT_SYSTEM` point 5 says *"When encountering bugs, you must not attempt to modify the yml
file; instead, you should write correct SQL based on the existing yml."*

This is in tension with the `order_data` blocker: fixing it requires editing
`dbt_project.yml`, and the Phase E Opus run did exactly that (step 24/33) despite point 5.
Arguably `dbt_project.yml` is project config rather than a *model* yml, so point 5 doesn't
bind — but it is ambiguous. The proposed scaffold **omits point 5 entirely** rather than
guess. If the team wants it, the safe phrasing is to scope it to model schema files
explicitly, e.g. *"do not modify the model schema .yml files; write SQL that satisfies
them"*, leaving `dbt_project.yml` untouched by the rule.

Flagging because getting this wrong in either direction changes pass rates: too strict and
the agent can't fix `order_data` (guaranteed 0); too loose and it may "fix" failures by
rewriting the schema definitions it's being scored against.

---

## When applying

Replace `ORIENTATION` in `pi_runner/run_task.py`. The runner already records the scaffold
verbatim into every `run_record.json` under `prompt_scaffold`, so old and new runs stay
distinguishable after the fact. Re-baseline with N=10 before comparing to any prior number.
