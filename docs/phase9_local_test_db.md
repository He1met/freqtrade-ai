# Phase 9 Local Test Database QA

Phase 9 reuses the guarded local test database tooling from Phase 8, but the
QA goal is Operational Readiness: create success, failure, BLOCKED, dirty data,
missing artifact, partial completion, and unknown-source rows without touching
production or shared databases.

This is local fixture data only. It cannot satisfy real Provider acceptance and
must be displayed as `fixture` or `unknown` with `core_data=false`.

## Safe Target Rules

Destructive commands are allowed only when all guard checks pass:

- SQLite is an absolute `/tmp/freqtrade-ai-*` file.
- PostgreSQL is on `localhost`, `127.0.0.1`, `::1`, or a local socket.
- PostgreSQL database names include `local`, `test`, `debug`, `phase8`, or
  `phase9`.
- The environment label is one of the approved local/dev/test labels.

The guard refuses production, shared, remote, live, unknown, in-memory,
relative SQLite, and non-local PostgreSQL targets. Stored and printed database
URLs are redacted.

## One Command Setup

Use a disposable local DB:

```bash
python3 scripts/phase8_local_test_db.py \
  --database-url sqlite+pysqlite:////tmp/freqtrade-ai-phase9-local-test.sqlite \
  --environment phase9 \
  --json \
  seed-operational-readiness
```

The command resets the guarded DB, seeds baseline rows, seeds dirty rows, and
prints an acceptance report with guard refusal examples.

For a read-only report after seeding:

```bash
python3 scripts/phase8_local_test_db.py \
  --database-url sqlite+pysqlite:////tmp/freqtrade-ai-phase9-local-test.sqlite \
  --environment phase9 \
  --json \
  acceptance-report
```

## Required Scenario Coverage

The report must show all of these scenarios as `present=true` and
`can_accept_as_real_run=false`:

- `success`
- `failed`
- `blocked`
- `unknown-source`
- `missing-artifact`
- `partial-completion`
- `dirty-score-without-result`
- `dirty-stale-running-backtest`
- `dirty-task-result-path-without-result-row`

These rows are useful for API/UI/DB reconciliation and failure display checks.
They are not real LLM output, real local backtest success, or production
evidence.

## Real Acceptance Boundary

Real Phase 9 acceptance still requires a separate controlled E2E run where:

- Provider credentials come from environment-only configuration.
- API and UI show `database` or `api_aggregate` data with `core_data=true`.
- API payloads include stable `database_ids`.
- The strategy file exists in an approved local strategy directory and matches
  its checksum.
- Local backtest result and strategy score rows are persisted.
- `SourceMarker` has no `BLOCKED` reason on the accepted core row.

Any fixture, fallback, mock, dirty, unknown, or local-test row must remain
non-acceptable and must explain the missing prerequisite.
