# spider-dbt-dfc

Deterministic Data-Flow Control (DFC) applied to Spider 2.0-DBT, run through the Pi agent harness.

## Overview

This repo extends prior DFC work to repository-level dbt tasks from Spider 2.0-DBT. An agent is given a broken/incomplete dbt project and must complete the missing models so the produced DuckDB matches the gold output under the official `duckdb_match` evaluator. DFC adds post-materialization policy checks that detect specific failure modes and steer the agent to correct them.

## Layout

- `pi_runner/` — Python orchestrator that drives the Pi agent against a dbt task and scores the result (`run_task.py`), plus sweep/matrix drivers.
- `dfc/` — DFC policy checkers and the agent-side integration.
- `docs/` — analysis writeups (e-commerce baseline, DFC results, failure taxonomy).
- `runs/pi/` — run outputs and result summaries.

## Approach

- Harness: Pi (`pi --mode rpc`) drives the agentic loop; dbt execution and the official evaluator stay Python-side.
- Scoring: `score_run.py` / `duckdb_match` — binary pass/fail, set comparison, no partial credit.
- DFC policies: gold-free checkers that flag a violation post-materialization and issue a targeted retry (name-gate, discount/amount, id-type, enum), composable via `--dfc-policy`.

## Key result

On a multi-defect task, no single policy passes it, but stacking the policies that target each defect does — each policy clears its own defect and the task passes under the official evaluator.
