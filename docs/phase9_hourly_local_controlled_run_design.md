# Phase 9 Hourly Local Controlled Run Design

This document scopes issue `#330`: a future local hourly run that can generate
at most one strategy, preflight a local backtest, ingest a result, and expose
evidence. It is design only. It does not implement a scheduler, start a loop,
start live trading, place orders, deploy production services, or introduce
Redis, Celery, Kafka, RabbitMQ, worker pools, or managed queues.

## Product Boundary

The hourly run is a local operator tool, not an unattended trading system. It
may be useful after Phase 9 proves the single DeepSeek E2E path, but Phase 9
does not authorize always-on execution.

The default product state is `DISABLED`. A future implementation must require
an explicit local enable switch and must provide a visible pause/stop control
before any periodic run can start.

## Implementation Gate

Issue `#330` authorizes design and acceptance criteria only. Implementation of
the hourly runner must remain blocked until both of these single-run paths have
passed against real local evidence:

1. The single DeepSeek entry point completes with a real Provider response,
   persists the generation, strategy, and strategy-version records, writes a
   validated strategy file, and exposes redacted evidence.
2. The minimum generation-to-backtest chain completes with local market data
   and Freqtrade, then persists the backtest run, task, parsed result, score,
   and artifact manifest.

The gate is not satisfied by mock HTTP responses, fake providers, fixtures,
fallback data, preflight-only runs, or manually supplied artifacts from an
unrelated run. QA must be able to refresh the API and UI and reconcile the
reported IDs and artifact refs with the local database and filesystem. A
missing DeepSeek credential, unavailable Provider network, missing Freqtrade
binary, missing market data, unsafe DB target, or unwritable artifact path
keeps implementation `BLOCKED`; it does not justify enabling a partial hourly
loop.

## Required Inputs

Each attempted hourly run needs these local prerequisites:

- Provider boundary configured for DeepSeek or another approved real provider.
- Credential value present only in the local environment.
- Local DB target is guarded and non-production.
- Local market data exists for the selected exchange, pair, timeframe, and
  timerange.
- Freqtrade binary is available only for local backtesting.
- Strategy output and backtest artifact directories are writable.
- Live trading, real orders, exchange connections, dry-run bot control, and
  production deploy flags remain disabled unless a later issue explicitly
  changes scope.

Missing prerequisites produce `BLOCKED` evidence and no provider call.

## State Machine

| State | Meaning | Terminal |
| --- | --- | --- |
| `DISABLED` | Hourly runner is not enabled. | No |
| `PAUSED` | Runner is configured but paused by operator. | No |
| `READY` | Preconditions passed and the next local run is eligible. | No |
| `RUNNING` | One local attempt has an active lease. | No |
| `COMPLETED` | Provider, DB, strategy file, backtest artifact, result, score, and UI evidence are complete. | Yes |
| `BLOCKED` | A prerequisite is missing or unsafe; no fake success is allowed. | Yes |
| `FAILED` | A provider, parser, validation, backtest, or scoring step failed after start. | Yes |
| `CANCELLED` | Operator stopped the pending or running local attempt. | Yes |
| `STALE` | Lease expired without a safe completion path. | Yes |

Only one `RUNNING` attempt may exist at a time. A future implementation should
store a local lease owner, start timestamp, heartbeat timestamp, and expiry
deadline. If the heartbeat expires, the attempt becomes `STALE`; it must not be
silently retried as success.

## Cadence Rules

- At most one attempt per wall-clock hour.
- No catch-up storm after downtime; missed hours are skipped.
- Manual "run now" is allowed only if no active lease exists.
- Backoff after `FAILED` or repeated `BLOCKED` results is recorded in DB and
  visible in the UI.
- A pause takes effect before the next attempt and should also allow cancelling
  a pending lease.

## Execution Chain

The future runner should reuse the existing Phase 9 surfaces instead of adding
parallel paths:

1. Read runner config and confirm it is enabled, local-only, and not paused.
2. Create a local run record with `READY` or `BLOCKED` preflight evidence.
3. Validate Provider config, credential presence, DB guard, local data,
   Freqtrade binary, strategy output directory, artifact directory, and safety
   flags.
4. If any preflight check is blocked, persist `BLOCKED` and stop.
5. If preflight is ready, make exactly one Provider request for one strategy.
6. Persist `strategy_generation_run`, `strategy`, and `strategy_version`.
7. Write and validate the strategy file in the approved local directory.
8. Create a local backtest run/task through the existing local backtest
   preflight service.
9. Execute or ingest only local backtest artifacts allowed by a later
   implementation issue.
10. Persist `backtest_result` and `strategy_score`.
11. Expose browser/API/DB evidence and mark the attempt `COMPLETED` only when
    all core data sources are traceable.

Any fake, fixture, fallback, mock, unknown, missing-artifact, parser-failed, or
score-missing path must remain `BLOCKED` or `FAILED`.

## Data And Evidence

The future runner should keep evidence in existing tables where possible:

- `strategy_generation_runs`: Provider/model/status/counts/error summary.
- `strategies` and `strategy_versions`: generated record and file artifact.
- `backtest_runs` and `backtest_tasks`: local preflight and task status.
- `backtest_results`: parsed local artifact.
- `strategy_scores`: scoring result tied to the parsed result.
- `local_test_batches` and `local_test_db_events`: QA fixture setup only.

A future small runner table may be added for cadence and lease metadata, but it
must store only public state: enabled flag, paused flag, next eligible time,
lease owner, timestamps, status, linked database IDs, and redacted reason
strings. It must not store credential values, raw provider response bodies, or
raw command output containing secrets.

## API And UI Requirements

Future API endpoints should support:

- read current runner config and status;
- enable/disable local hourly mode;
- pause/resume;
- request one manual local attempt;
- cancel a pending or running attempt;
- list recent attempts with linked generation/backtest/result/score IDs.

The UI must show:

- current state and next eligible time;
- whether the latest attempt can be accepted as real evidence;
- linked database IDs and artifact refs;
- `BLOCKED` or `FAILED` reasons and required actions;
- a clear local-only safety boundary.

No page may display fixture/fallback/mock rows as hourly success.

## Failure Semantics

| Failure | Required Result |
| --- | --- |
| Missing provider credential | `BLOCKED`, no provider request. |
| Provider request failure | `FAILED`, durable generation run with redacted error. |
| Provider returns invalid blueprint | `FAILED`, durable generation run and validation reason. |
| Strategy file missing or checksum mismatch | `BLOCKED` or `FAILED`, no accepted backtest. |
| Local data missing | `BLOCKED`, no download and no exchange connection. |
| Freqtrade binary missing | `BLOCKED`, no fake backtest result. |
| Backtest artifact missing | `BLOCKED`, no result/score. |
| Parser failure | `FAILED`, no score. |
| Score generation failure | `FAILED`, result retained but not accepted. |
| Lease expiry | `STALE`, no automatic retry-as-success. |

All failures must be visible through API/UI and must preserve database IDs for
debugging when records exist.

## Why No Production Queue In Phase 9

The hourly local design needs single-flight behavior and auditability, but not
distributed queue infrastructure. Adding Redis, Celery, Kafka, RabbitMQ, worker
pools, or managed queues would expand operations, deployment, replay,
monitoring, and security scope before the local product chain is accepted.

Phase 9 should prove the local chain first. Queue implementation belongs to a
later approved Phase 10 or productionization issue.

## QA Acceptance Checklist

QA should accept the design only if it answers:

- How is the runner disabled by default?
- How can an operator pause, resume, cancel, or run once?
- How does it prevent more than one attempt per hour?
- How does it prevent concurrent attempts?
- Where are `BLOCKED`, `FAILED`, `STALE`, and `COMPLETED` persisted?
- Which existing tables prove Provider, strategy, file, backtest, result, and
  score evidence?
- How do API/UI expose data source, database IDs, artifact refs, and required
  actions?
- Why can fixture/fallback/mock/unknown data never complete an hourly run?
- Why are live trading, real orders, production deploy, and production queues
  still out of scope?
- Which durable evidence proves that the single real DeepSeek run and the
  minimum generation-to-backtest chain passed before hourly implementation was
  enabled?

If any answer is missing, the future implementation issue should remain blocked.

## Issue #330 Acceptance Mapping

| Issue requirement | Design decision |
| --- | --- |
| At most one strategy and backtest per hour | Cadence allows at most one attempt per wall-clock hour and skips missed hours. |
| Wait for the single DeepSeek and minimum chain | The implementation gate requires real, refreshable Provider, DB, file, backtest, result, score, and manifest evidence. |
| Pause and shutdown | Default is `DISABLED`; API/UI must support disable, pause/resume, and cancellation. |
| No concurrency | A single local lease permits only one `RUNNING` attempt and records stale lease expiry. |
| Run and failure records | State, linked database IDs, artifact refs, and redacted reasons are durable and visible through DB/API/UI. |
| Page history and actionable empty states | UI lists recent attempts and shows acceptance state, missing conditions, next action, and Gap classification instead of fake success. |
| QA-verifiable safety boundary | Real data sources are reconcilable after refresh; mock/fallback data cannot complete a run. |
| No production runtime expansion | No Redis/Celery/Kafka/RabbitMQ, managed queue, worker pool, background silent execution, production deployment, live trading, or real orders. |
