# Phase 8 Controlled Dry-run Boundary

Phase 8 controlled dry-run is local-only runtime evidence. It is not live
trading evidence and it must never be used as production deployment control.

## Start Gate

`POST /api/dry-run/control/start` is fail-closed. A process start is attempted
only when all of these are true:

- Phase 8 readiness returns `READY`.
- The request sets `manual_approval=true`.
- Backend `security.allow_controlled_dry_run_process` is `true`.
- Backend `allow_live_trading` and legacy `allow_dry_run_trading` remain `false`.
- Required dry-run ENV names are present, but ENV values are never serialized.

By default, `allow_controlled_dry_run_process` is `false`, so local runs return
`BLOCKED` evidence instead of starting Freqtrade.

## Evidence

The control service writes local evidence under `reports/runtime/`:

- `dry-run-manifest.json`: command shape, return code, redacted stdout/stderr,
  profile snapshot, ENV-name preflight, and status snapshots.
- `dry-run-status.json`: read-only status snapshot consumed by
  `/api/dry-run/status`, `/api/dry-run/management`, and frontend pages.

Manifest and status payloads redact credential-shaped assignment patterns,
passphrases, passwords, tokens, and bearer tokens. Reports contain ENV variable
names only, not credential values.

## Stop / Cleanup

`POST /api/dry-run/control/stop` records a `STOPPED` status snapshot and keeps
manifest/config evidence for audit. It does not kill arbitrary external
processes. The controlled start path is synchronous and bounded by
`timeout_seconds`; timeout is recorded as `FAILED`.

## Forbidden Boundaries

This Phase 8 path does not enable live trading, real orders, production or
shared database mutation, exchange connection without the approved dry-run
profile, Freqtrade source modification, Redis/Celery/Kafka/RabbitMQ, or
unattended production operation.
