# Phase 3 Acceptance

## Status

Phase 3 backtesting-system enhancement has passed final acceptance.

The project may move to Phase 4 planning. This document does not authorize or
implement Hyperopt, dry-run, live trading, exchange connectivity, data download,
deployment, or production operation.

## Completed Scope

The Phase 3 scope is complete through the following issues and merged PRs:

| Issue | PR | Scope | Status |
| --- | --- | --- | --- |
| `#103` | `#115` | Backtesting experiment design and execution plan | Done |
| `#104` | `#116` | Local market-data availability and real backtest baseline spike | Done |
| `#105` | `#117` | MarketDataCatalog and data quality checks | Done |
| `#106` | `#118` | BacktestProfile schema v2 and experiment variable lock | Done |
| `#107` | `#119` | Freqtrade backtesting artifact manifest | Done |
| `#108` | `#120` | Backtest result metrics expansion and version-compatible parsing | Done |
| `#109` | `#121` | Batch backtest matrix execution and fail-closed aggregation | Done |
| `#110` | `#122` | Backtest baseline comparison and reproducibility checks | Done |
| `#111` | `#123` | BacktestRun artifact and enhanced metrics display | Done |
| `#112` | `#124` | Backtest Matrix status and summary display | Done |
| `#113` | `#125` | Phase 3 offline smoke acceptance script | Done |

Epic `#102` remains an XL aggregation issue and is not a development target.

## Acceptance Commands

Final acceptance was run on `2026-07-02` from the closeout branch based on
`origin/main` commit `1f9319b`. The closeout branch contains documentation-only
changes.

```bash
python3 scripts/smoke_phase3.py --offline --tmp-dir /tmp/freqtrade-ai-phase3-smoke
```

Result: PASS.

Observed smoke coverage:

- MarketDataCatalog local fixture scan and metadata checks passed.
- BacktestProfile v2 local-only validation passed.
- Artifact manifest and matrix summary covered SUCCESS and BLOCKED tasks.
- Fixture Freqtrade result JSON metrics parsing passed.
- Reproducibility fingerprint and missing-baseline handling passed.
- Frontend Phase 3 build inside smoke passed.
- Real local-data readiness remained fail-closed: no exchange directories were
  found under `user_data/data`.

```bash
cd backend && . .venv/bin/activate && pytest
```

Result: PASS, `126 passed`.

```bash
python3 -m compileall backend/app backend/tests scripts
```

Result: PASS.

```bash
cd frontend && npm run build
```

Result: PASS.

```bash
git diff --check
```

Result: PASS.

## Accepted Capabilities

Phase 3 adds a local-only, auditable backtesting research loop:

- Local market-data catalog and data quality status.
- BacktestProfile v2 validation, snapshotting, and safe config inputs.
- Freqtrade backtesting artifact manifests with stdout, stderr, return code,
  config path, strategy path, result path, and clear result status.
- Stable metrics DTO parsing across fixture result JSON shapes.
- Small deterministic matrix execution with SUCCESS / FAILED / BLOCKED
  aggregation.
- Reproducibility fingerprints and baseline comparison summaries.
- Frontend BacktestRun and Backtest Matrix summaries for artifact, metric, and
  blocked/failed state review.
- Offline Phase 3 smoke command for repeatable local validation.

## Current Limits

Phase 3 remains a local research and backtesting phase. It does not enable
trading runtime features.

- No dry-run.
- No live trading.
- No Hyperopt.
- No real exchange connection.
- No real K-line download.
- No real order placement.
- No committed API key, secret, or passphrase.
- No Freqtrade source-code modification.
- No Redis, Celery, Kafka, or RabbitMQ.
- No Phase 4, Phase 5, or Phase 6 implementation.

Real Freqtrade backtesting remains local-data dependent. The project may only
run a real backtest when the user's machine already has compatible market data
under `user_data/data`. If local market data is missing, the correct behavior is
a clear `BLOCKED` result, not a fabricated success and not an automatic data
download.

## Phase 4 Readiness

Phase 3 is accepted. The project can enter Phase 4 planning.

Phase 4 should start with planning and issue creation only. It should not
silently widen this acceptance result into Hyperopt execution, dry-run, live
trading, exchange connectivity, queue infrastructure, deployment, or production
operation.
