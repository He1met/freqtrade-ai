# Phase 9 DeepSeek Backtest Minimal Loop

This runbook is for issue `#327`: the smallest durable backend loop that
persists one DeepSeek generation run, one local backtest preflight/execution
attempt, one artifact ingest, and one strategy score, or stops with explicit
`BLOCKED` / `FAILED` evidence.

The formal API entry is:

```text
POST /api/strategy-generation-runs/deepseek-single/backtest-loop
```

The loop is backend-only. It does not add queue workers, production schedulers,
frontend behavior, live trading, real orders, exchange connectivity, or market
data downloads.

## Request Contract

Required fields:

- `prompt_summary`
- `allow_real_call`
- `backtest_profile`

Optional fields:

- `timeout_seconds`

The endpoint still fails closed when `allow_real_call=false`. That default path
persists a `strategy_generation_runs` row with `provider=deepseek` and
`status=failed`, but it never sends a provider request.

## Response Contract

The response always includes:

- `overall_status`
- `generation_run`
- `evidence`

Depending on how far the loop reached, it may also include:

- `generation`
- `backtest`
- `execution`
- `artifact_ingest`

`evidence.ids` and `evidence.artifact_refs` are the canonical reconciliation
surface for QA. Success requires the response to include durable IDs for:

- `strategy_generation_run_id`
- `strategy_id`
- `strategy_version_id`
- `backtest_run_id`
- `backtest_task_id`
- `backtest_result_id`
- `strategy_score_id`

## Status Rules

- `BLOCKED`: missing explicit authorization, missing DeepSeek key, missing
  local Freqtrade binary, missing local data, missing writable artifact paths,
  or any manifest/result gate that must stop without pretending success.
- `FAILED`: provider request failure, invalid provider payload, non-zero local
  Freqtrade backtest exit, invalid artifact manifest/result, or scoring failure.
- `SUCCESS`: generation, strategy file write, preflight, local backtest
  execution, artifact ingest, and score persistence all completed with core
  database or `api_aggregate` evidence.

## Test Boundary

Normal automated tests must not call the real DeepSeek API. The backend tests
for this issue use:

- a mocked OpenAI-compatible DeepSeek provider boundary;
- a fake local Freqtrade CLI executor;
- real database writes in a local test SQLite database.

That proves the orchestrator/API/schema contract without sending a network call
to DeepSeek and without requiring a real Freqtrade binary during test runs.
