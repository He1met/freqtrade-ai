# Phase 8 Local Test Database

This QA tool is local-only. It resets and seeds a guarded database so Phase 8
records can be reconciled across API responses, direct DB inspection, and
fixture metadata without connecting to exchanges or starting trading workflows.

## Safety Guard

The command refuses mutation unless the target is one of:

- SQLite file under `/tmp` whose filename starts with `freqtrade-ai-`.
- PostgreSQL on `localhost`, `127.0.0.1`, `::1`, or a local socket, where the
  database name explicitly contains `local`, `test`, `debug`, or `phase8`.

It refuses production, shared, remote, live, unknown, in-memory, relative SQLite,
and non-local PostgreSQL targets. The command redacts database URLs in stored
batch metadata.

## Commands

```bash
python3 scripts/phase8_local_test_db.py guard
python3 scripts/phase8_local_test_db.py reset
python3 scripts/phase8_local_test_db.py seed-baseline
python3 scripts/phase8_local_test_db.py dirty-scenario
python3 scripts/phase8_local_test_db.py seed-operational-readiness
python3 scripts/phase8_local_test_db.py --json acceptance-report
python3 scripts/phase8_local_test_db.py --json summary
```

By default the script uses:

```text
sqlite+pysqlite:////tmp/freqtrade-ai-phase8-local-test.sqlite
```

To use a safe local PostgreSQL database:

```bash
python3 scripts/phase8_local_test_db.py \
  --database-url postgresql+psycopg://freqtrade:change_me@localhost:5432/freqtrade_ai_phase8_test \
  --environment phase8 \
  seed-baseline
```

## Seeded Data

`seed-baseline` creates one `local_test_batches` row and scenario events for:

- `success`
- `failed`
- `blocked`
- `unknown-source`
- `missing-artifact`
- `partial-completion`

`dirty-scenario` creates intentionally inconsistent but constraint-valid rows:

- score without a backtest result row
- stale running backtest
- task result path without a parsed result row

`seed-operational-readiness` is the Phase 9 QA shortcut. It resets the guarded
database, seeds baseline and dirty scenarios, and returns a machine-readable
coverage report. See [phase9_local_test_db.md](phase9_local_test_db.md).

Each seeded business row carries Phase 8 local-test metadata in JSON-capable
fields such as `params_snapshot`, `blueprint`, `diff_snapshot`,
`config_snapshot`, `metrics_snapshot`, failure `details`, and strategy tags.
The metadata includes `test_batch.batch_key`, `scenario`, `scenario_set`,
`source_kind`, `source_label`, `environment_label`, and `seed_version`.

## Source Categories

API-generated data means rows produced by Phase 8 backend APIs in later issues.
Those rows may satisfy core success only when the API persists traceable
database IDs and artifact references.

Seed-generated data means rows produced by
`scripts/phase8_local_test_db.py seed-baseline`. These rows are fixture data for
QA and debug display. They are never core-flow success evidence.

Dirty seed data means rows produced by
`scripts/phase8_local_test_db.py dirty-scenario`. These rows are deliberate QA
edge cases and are never production evidence.

Real-flow-generated data means rows produced by the actual local Phase 8 product
workflow after its backend and frontend issues land. Those rows must be
reconciled through page, API, DB, and artifact checks.

When a read model sees Phase 8 local-test metadata, its `DataSourceTrace` uses
`fixture` or `unknown` with `core_data=false` while keeping database IDs for QA
lookup. This prevents fixture and dirty rows from masquerading as core success.

## Batch Summary

Use the `summary` command to list local-test batch/event metadata for direct QA
reconciliation. The summary path is script/service only in `#235`; business APIs
for Phase 8 product flows are left to later issues.
