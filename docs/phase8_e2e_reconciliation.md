# Phase 8 E2E Reconciliation

`scripts/smoke_phase8.py` is the local QA entry point for page/API/database
reconciliation. It proves that the Local Strategy Lab core path is backed by
database rows and backend API responses, while fixture, dirty, fallback, and
unknown-source rows remain non-core evidence.

## Default Local Run

```bash
python3 scripts/smoke_phase8.py
```

The default run uses:

```text
sqlite+pysqlite:////tmp/freqtrade-ai-phase8-e2e.sqlite
```

The script refuses unsafe database targets through the Phase 8 local database
guard, resets the local DB, seeds baseline and dirty fixture rows, inserts one
database-backed core strategy/backtest/scoring flow, starts the backend and
frontend, checks key API paths, and writes QA evidence to:

```text
/tmp/freqtrade-ai-phase8-e2e/reports/phase8-e2e-evidence.json
```

## Offline CI Run

```bash
python3 scripts/smoke_phase8.py \
  --offline \
  --skip-frontend \
  --tmp-dir /tmp/freqtrade-ai-phase8-e2e-ci \
  --database-url sqlite+pysqlite:////tmp/freqtrade-ai-phase8-e2e-ci.sqlite
```

Offline mode uses FastAPI `TestClient` instead of starting local services. It
still validates API contracts, database reconciliation, source metadata, and
fixture/unknown non-core behavior.

## Evidence Contract

The evidence JSON contains:

- `core_ids`: strategy, generation run, strategy version, backtest run/task,
  backtest result, and strategy score IDs.
- `database_reconciliation`: direct DB source counts and non-core row counts.
- `api_reconciliation`: HTTP status and item counts for key API paths.
- `frontend_page`: frontend page delivery check when frontend is enabled.
- `local_test_summary`: reset, baseline, and dirty seed batch metadata.
- `safety`: local-only boundaries proving no Freqtrade start, exchange
  connection, market data download, live trading, real order path, secret
  persistence, or production database mutation.
- `table_queries`: the exact per-table SQL, expected primary key, and result for
  every persisted core-flow row.
- `core_snapshot_before_refresh` / `core_snapshot_after_refresh`: stable
  `database_ids`, `artifact_refs`, and `data_source` snapshots from two separate
  API reads.
- `page_evidence_points`: routes, sections, API paths, and IDs that must be
  visible in browser evidence.
- `acceptance`: the explicit `PASS`, `FAILED`, or `BLOCKED` decision,
  `acceptance_ready`, failed checks, reason, and next action.

## Direct Database Reconciliation

Use the IDs from `core_ids` in the evidence file. For SQLite, open the guarded
database with `sqlite3 /tmp/freqtrade-ai-phase8-e2e.sqlite`; for PostgreSQL use
the equivalent read-only client. Run every query, not only aggregate counts:

```sql
SELECT * FROM strategies WHERE id = :strategy_id;
SELECT * FROM strategy_generation_runs WHERE id = :strategy_generation_run_id;
SELECT * FROM strategy_versions WHERE id = :strategy_version_id;
SELECT * FROM backtest_runs WHERE id = :backtest_run_id;
SELECT * FROM backtest_tasks WHERE id = :backtest_task_id;
SELECT * FROM backtest_results WHERE id = :backtest_result_id;
SELECT * FROM strategy_scores WHERE id = :strategy_score_id;
```

Each query must return exactly the row identified by `core_ids`. The API row's
`data_source.database_ids` must contain that primary key and all available
parent IDs. Strategy version, backtest result, and ranking `artifact_refs` must
exactly match the schema projection built from the database row.

## API and Page Evidence Matrix

| Page evidence point | API paths | Required visible IDs / evidence |
| --- | --- | --- |
| `/local-strategy-lab`, generation and strategy version | `/api/strategy-generation-runs`, `/api/strategies`, `/api/strategy-versions` | generation run, strategy, and strategy version IDs; Provider/status; strategy file artifact; `database` source |
| `/local-strategy-lab`, backtest | `/api/backtest-runs`, `/api/backtest-tasks`, `/api/backtest-results` | run, task, and result IDs; status; config/result artifacts; `database` source |
| `/local-strategy-lab`, ranking | `/api/ranking` | score and result IDs; score; `api_aggregate` source |

For each API path verify HTTP `200`, locate the exact ID from `core_ids`, and
record `database_ids`, `artifact_refs`, `source_type`, and `core_data`. Fixture,
fallback, and unknown rows may be displayed for diagnostics but must remain
`core_data=false` and cannot satisfy acceptance.

## Refresh Verification

1. Capture the three page sections and the corresponding API rows.
2. Reload the browser route without reseeding or rerunning the smoke.
3. Call every key API path again.
4. Confirm all core IDs remain present and the before/after `database_ids`,
   `artifact_refs`, and `data_source` snapshots are byte-for-byte equivalent.
5. Confirm the direct SQL rows still use the same primary and parent IDs.

The smoke performs steps 3-5 automatically and writes both snapshots. Browser
screenshots before and after refresh remain manual evidence because offline CI
does not render the React application.

## Acceptance Decision

- `PASS`: every direct table row exists, all key APIs return the same IDs and
  source/artifact contract, refresh is stable, and enabled frontend delivery
  checks pass. Browser screenshots may be attached as follow-up evidence when
  the smoke is run with `--offline`.
- `FAILED`: a runnable check produces a mismatch, missing row, HTTP error,
  source/artifact drift, refresh drift, or frontend delivery failure. Fix the
  product or test and rerun; do not relabel this as an environment blocker.
- `BLOCKED`: the check cannot run because a required environment capability is
  absent, such as an unavailable configured database or browser runtime. Record
  the exact `blocked_reason` and `next_action`; do not claim acceptance.

## Browser Runtime Check

When running from Codex or another browser-capable QA shell, open:

```text
http://127.0.0.1:5173/local-strategy-lab
```

The page should show visible `database` / `api_aggregate` markers for core data
and `fixture`, `fallback`, or `unknown` markers for non-core rows. A browser
runtime pass means no white screen, no severe console error, and no 404/500 on
the key Phase 8 API paths listed in the evidence file.

## Safety Boundary

This smoke path does not start Freqtrade, connect to an exchange, download
market data, place orders, mutate production/shared/remote databases, persist
secrets, or modify Freqtrade source code.
