# Phase 9 DeepSeek Backtest Job

Issue `#327` originally introduced the smallest durable backend
DeepSeek-to-backtest loop as one synchronous HTTP request. Issue `#369`
preserves that research chain and evidence contract but moves execution to the
single-process DB-backed local worker.

The formal enqueue entry is retained:

```text
POST /api/strategy-generation-runs/deepseek-single/backtest-loop
```

The equivalent canonical job entry is:

```text
POST /api/deepseek-backtest-jobs
```

Both return `202 Accepted`. They persist a research job and return immediately;
neither endpoint calls DeepSeek, runs Freqtrade, ingests artifacts, or computes
a score inside the HTTP request.

## Enqueue Contract

Required fields:

- `prompt_summary`;
- `allow_real_call`;
- `backtest_profile`.

Optional fields:

- `timeout_seconds`.

The request requires local operator authorization and a valid
`Idempotency-Key`. When `allow_real_call=true`, it also requires the
`X-Provider-Authorization` header with the documented single-use consent value.
The Provider key remains ENV-only and no credential or authorization-header
value is persisted.

`allow_real_call=false` is still fail-closed. The API may accept and persist the
job, but the worker must not send a Provider request; the durable result becomes
`BLOCKED` rather than fake success.

The enqueue response is a `ResearchJobRead` payload. A new job normally has:

```json
{
  "id": 123,
  "status": "PENDING",
  "stage": "QUEUED",
  "attempt_count": 0,
  "status_url": "/api/deepseek-backtest-jobs/123"
}
```

The complete response also includes safe lease metadata, terminal evidence,
database IDs, and artifact references when available. It never returns the raw
request payload, idempotency key/digest, lease token, operator token, or
Provider key.

## Query Contract

Poll the returned URL instead of holding the enqueue request open:

```text
GET /api/deepseek-backtest-jobs/{job_id}
```

The list endpoint is:

```text
GET /api/deepseek-backtest-jobs?limit=100
```

`PENDING` and `RUNNING` are non-terminal. Terminal status is one of:

- `SUCCESS`;
- `FAILED`;
- `BLOCKED`;
- `CANCELLED`;
- `STALE`.

Only `SUCCESS` with reconciled core evidence is acceptance-ready. `STALE`
means the lease expired before a safe checkpoint; it must not be displayed or
treated as success.

## Evidence Contract

The job's `data_source.database_ids` and `evidence_snapshot` are the canonical
reconciliation surfaces. A complete success links durable IDs for:

- `research_job_id`;
- `strategy_generation_run_id`;
- `strategy_id`;
- `strategy_version_id`;
- `backtest_run_id`;
- `backtest_task_id`;
- `backtest_result_id`;
- `strategy_score_id`.

Artifact manifest and result references remain under the evidence snapshot and
data-source trace. Their presence alone is not success: the worker must finish
artifact validation, database persistence, scoring, and terminal reconciliation.

## Idempotency And Recovery

The job database stores a digest of the idempotency key and a separate stable
request hash. The same key and payload return the existing job, including after
an API restart. Reusing the key for a different payload returns `409 BLOCKED`.

Before a real Provider request, the worker commits a Provider-attempt marker.
If the worker loses its lease before recording a safe completion, the job
becomes `STALE` with an unknown Provider outcome and is not automatically
called again. This fail-closed rule prevents a restart from silently duplicating
an external Provider side effect.

Detailed claim, heartbeat, pause, cancel, CLI, and shutdown behavior is in
[phase9_db_backed_worker.md](phase9_db_backed_worker.md).

## Test Boundary

Normal automated tests must not call the real DeepSeek API or require a real
Freqtrade binary. They use a mocked Provider boundary, a fake Freqtrade
executor, and real database writes in guarded SQLite and PostgreSQL test
databases. Acceptance covers immediate `202` enqueue, cross-session
idempotency, single-flight claim, lease expiry, pause/cancel, restart ambiguity,
and API/database/artifact reconciliation.

## Safety Boundary

This asynchronous job contract does not add frontend controls, hourly or cron
scheduling, recurring job creation, a worker pool, Redis, Celery, Kafka,
RabbitMQ, production deployment, live trading, real orders, exchange
connectivity, market-data downloads, or Freqtrade source changes.
