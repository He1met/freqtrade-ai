# Phase 9 DB-backed Local Worker

Issue `#369` replaces the long synchronous DeepSeek-to-backtest HTTP request
with one durable, local-only research job and one database-backed worker.
The implementation is intentionally small: it uses the existing application
database and a single local process. It does not add a scheduler, broker,
distributed queue, or trading control plane.

## Scope

The worker may execute only this existing local research chain:

```text
DeepSeek authorization and generation
-> Strategy / StrategyVersion persistence
-> local backtest preflight
-> Freqtrade backtest execution
-> artifact ingest
-> BacktestResult persistence
-> StrategyScore persistence
```

The API persists and returns a job; it does not wait for that chain to finish.
The job is the reconciliation root for the generation, strategy, backtest,
result, score, and artifact evidence created by the worker.

## API Contract

Create one job with either equivalent entry point:

```text
POST /api/deepseek-backtest-jobs
POST /api/strategy-generation-runs/deepseek-single/backtest-loop
```

The legacy loop path is retained as an asynchronous enqueue alias. Both paths
return `202 Accepted` with a `ResearchJobRead` payload. A newly accepted job is
normally `PENDING` with `stage=QUEUED`; `status_url` identifies its durable
query endpoint.

Request fields remain:

- `prompt_summary`;
- `allow_real_call`;
- `backtest_profile`;
- optional `timeout_seconds`.

All create, cancel, pause, and resume requests require a valid local
`X-Operator-Token` and `Idempotency-Key`. A request with
`allow_real_call=true` also requires the `X-Provider-Authorization` header with
the documented single-use consent value. These headers authorize creation of
one durable job; no credential or header value is stored in the job.

Read and control endpoints:

```text
GET  /api/deepseek-backtest-jobs
GET  /api/deepseek-backtest-jobs/{job_id}
POST /api/deepseek-backtest-jobs/{job_id}/cancel
GET  /api/deepseek-backtest-worker/status
POST /api/deepseek-backtest-worker/pause
POST /api/deepseek-backtest-worker/resume
```

The job response exposes only safe reconciliation data: status, stage, lease
timestamps and owner, attempt count, durable database IDs, sanitized evidence,
artifact references, and a sanitized error summary. It does not expose the
stored request payload, idempotency digest, lease token, operator token,
Provider authorization header, or Provider key.

### Durable idempotency

Idempotency is enforced in the database with the operation and a SHA-256 digest
of the caller's key. The normalized request payload has a separate hash.

- The same key and the same payload return the existing job.
- The same key with a different payload returns `409 BLOCKED`.
- The raw idempotency key is not persisted or returned.
- A restart does not erase this decision.

The process-local operator request coordinator remains an immediate request
guard, but it is not the source of durable queue idempotency.

## Job State And Stage

The job status is one of:

| Status | Meaning | Terminal |
| --- | --- | --- |
| `PENDING` | Persisted and eligible for the single worker lease | No |
| `RUNNING` | Claimed by the worker under an unexpired lease | No |
| `SUCCESS` | Result, score, IDs, and evidence were reconciled | Yes |
| `FAILED` | A non-policy execution failure was recorded | Yes |
| `BLOCKED` | Authorization, prerequisite, safety, or provenance stopped execution | Yes |
| `CANCELLED` | The operator cancelled the job at a safe checkpoint | Yes |
| `STALE` | The worker lease expired before a safe terminal checkpoint | Yes |

`stage` provides the finer progress marker. The initial stages are `QUEUED`,
`GENERATION`, and `PROVIDER_CALL`; terminal jobs use `COMPLETED`, `FAILED`,
`BLOCKED`, `CANCELLED`, or `LEASE_EXPIRED` as appropriate. Consumers must use
`status`, not infer success from a stage name or from the existence of a partial
artifact.

## Claim, Lease, And Heartbeat

The database holds a singleton worker-control row and permits only one active
job. Claiming a job atomically:

1. confirms that the worker is not paused and no other job owns the global
   execution slot;
2. selects the oldest eligible `PENDING` job;
3. changes it to `RUNNING`, assigns a unique lease token and owner, records the
   expiry and heartbeat, and increments `attempt_count`;
4. records the active job in worker control.

PostgreSQL additionally uses a locked, skip-locked selection. SQLite uses the
same singleton reservation and conditional database updates. Heartbeats extend
only the matching, still-valid lease. Completion and cancellation are fenced by
the private lease token, so a former owner cannot overwrite a newer state.

When a lease expires, the next claim audit marks the abandoned job `STALE`,
clears its lease, and releases the global execution slot. `STALE` is evidence,
not success and not an automatic retry.

## Provider And Restart Safety

Before a real Provider call, the worker commits `provider_attempted_at`. After
the chain returns a safe terminal response, it records
`provider_completed_at` and the durable IDs. This order is deliberate.

An external Provider call cannot generally be proven exactly once across a
process crash. If a lease expires after the attempt started but before a
completion checkpoint, the outcome is treated as unknown and the job becomes
`STALE`. The worker must not automatically submit another Provider request for
that job. The operator must inspect the job, Provider diagnostics, database
rows, and artifacts before deciding whether a new job with a new idempotency
key is safe.

Existing `BacktestResult` and `StrategyScore` uniqueness and transaction
boundaries remain part of reconciliation. No retry path may present a duplicate
or partial result as a second success.

## Pause And Cancel

Pause is a persisted intake gate:

- a paused worker does not claim another `PENDING` job;
- pausing does not create an undocumented `PAUSED` job status;
- pausing does not silently cancel an active job;
- resume clears the gate and allows a later poll to claim work.

Cancel is job-specific. A `PENDING` job becomes `CANCELLED` immediately. A
`RUNNING` job records `cancel_requested=true`; the worker changes it to
`CANCELLED` and releases the lease at the next safe checkpoint. Cancel is not a
promise to interrupt a Provider or Freqtrade subprocess halfway through an
unsafe operation.

## Local Worker Lifecycle

For a deterministic one-job check from the backend virtual environment:

```bash
cd backend
.venv/bin/python -m app.workers.deepseek_backtest_worker --once
```

For continuous local polling:

```bash
cd backend
.venv/bin/python -m app.workers.deepseek_backtest_worker
```

Optional local-only controls include `--poll-interval` and `--lease-seconds`.
The default remains one worker process and one active job. Starting a second
process must not increase execution concurrency because the database owns the
single active lease.

The managed runtime starts and stops the worker with the backend and frontend:

```bash
make up
make status
make logs
make verify
make down
```

The managed runtime uses only the local PostgreSQL `freqtrade_ai` database
documented in the README. Runtime status, logs, PID validation, and shutdown
include the worker. Startup refuses a non-idle queue; shutdown stops the worker
before the API process and never marks unfinished work as successful or deletes
persisted jobs.

## Verification

The acceptance surface includes:

- SQLite and PostgreSQL single-flight claim behavior;
- durable idempotency across independent sessions;
- lease heartbeat, expiry, and former-owner fencing;
- persisted pause, resume, and cancel behavior;
- no repeated Provider call after an ambiguous expired attempt;
- no duplicate result or score;
- API, database ID, and artifact-reference reconciliation;
- payload and log redaction;
- managed worker status, logs, verification, and shutdown.

Run the complete backend and repository gates before accepting the change:

```bash
cd backend
.venv/bin/python -m pytest
.venv/bin/python -m app.workers.deepseek_backtest_worker --once
cd ..
python3 -m compileall backend/app backend/tests scripts
python3 scripts/scan_secrets.py
git diff --check
```

Real Provider validation remains opt-in and requires explicit local operator
authorization plus the configured ENV key. Normal automated tests use fake or
mock Provider and Freqtrade boundaries.

## Explicit Non-goals

Issue `#369` does not implement:

- hourly, cron, or unattended scheduling;
- recurring job creation;
- a worker pool or parallel backtests;
- Redis, Celery, Kafka, RabbitMQ, or a managed/distributed queue;
- live trading, real orders, exchange connections, or market-data download;
- dry-run/live bot start, stop, deploy, or production deployment;
- Freqtrade source changes;
- a new frontend or Operator Dashboard control plane.

The earlier [Phase 7 worker/queue design](phase7_worker_queue_design.md) remains
historical architecture evidence. Issue `#369` is the later, explicitly
approved single-process database-backed implementation; it does not retroactively
turn Phase 7 issue `#202` into an implementation issue.
