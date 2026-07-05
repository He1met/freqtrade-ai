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
