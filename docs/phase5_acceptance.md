# Phase 5 Acceptance

## Status

Phase 5 Dry-run / FreqUI runtime management has passed final acceptance.

This acceptance result does not authorize live trading, real order placement,
exchange connectivity, production deployment, market-data download, or Phase 6
implementation. Phase 6 planning requires a separate issue, review, and safety
approval path.

## Completed Scope

The Phase 5 scope is complete through the following issues and merged PRs:

| Issue | PR | Scope | Status |
| --- | --- | --- | --- |
| `#152` | `#164` | Dry-run / FreqUI safety boundary and execution plan | Done |
| `#153` | `#165` | Freqtrade dry-run local prerequisites and risk preflight | Done |
| `#154` | `#166` | DryRunProfile schema and runtime-variable lock | Done |
| `#155` | `#167` | Controlled Freqtrade dry-run CLI command construction | Done |
| `#156` | `#168` | Dry-run config generation and ENV-only secret preflight | Done |
| `#157` | `#169` | Dry-run artifact manifest and status archive | Done |
| `#158` | `#170` | Dry-run read-only status snapshots and event parsing | Done |
| `#159` | `#171` | FreqUI entry configuration and read-only link boundary | Done |
| `#160` | `#172` | Dry-run / FreqUI runtime management page | Done |
| `#161` | `#173` | PR #127 runtime API and fallback contract split decision | Done |
| `#162` | `#174` | Phase 5 offline smoke acceptance script | Done |

Epic `#151` remains an XL aggregation issue and is not a direct development
target. It can be closed and marked Done after the `#163` closeout review PR is
merged.

## Acceptance Commands

Final acceptance was run on `2026-07-05` from the `#163` closeout branch based
on `origin/main` commit `80840f8`. The closeout branch contains documentation
updates only.

```bash
python3 scripts/smoke_phase5.py --offline --tmp-dir /tmp/freqtrade-ai-phase5-smoke
```

Result: PASS.

Observed smoke coverage:

- DryRunProfile fixture and ENV-only config generation passed.
- SUCCESS, FAILED, and BLOCKED dry-run artifact manifest paths passed.
- Read-only dry-run status snapshot parsing passed.
- FreqUI metadata stayed at `read-only-link` and disabled / blocked states were
  represented.
- Smoke summary kept real Freqtrade, exchange connection, market-data download,
  real dry-run, live trading, real orders, and secrets persistence disabled.
- Frontend Phase 5 build inside smoke passed.

```bash
cd backend && . .venv/bin/activate && pytest
```

Result: PASS.

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

Phase 5 adds a safe management layer around local dry-run readiness and FreqUI
entry points:

- documented dry-run / FreqUI safety boundaries;
- local prerequisite and risk preflight checks;
- DryRunProfile schema with locked runtime variables;
- allowlisted Freqtrade dry-run command construction;
- temporary dry-run config generation with ENV-only secret preflight;
- dry-run artifact manifests with SUCCESS / FAILED / BLOCKED / SKIPPED states;
- read-only dry-run status snapshots and events;
- FreqUI link metadata with disabled / blocked states;
- frontend display for dry-run status, manifest, snapshot, and FreqUI link
  information;
- PR #127 split decision that keeps the monolithic draft PR out of Phase 5;
- repeatable offline Phase 5 smoke validation.

## Current Limits

Phase 5 remains a dry-run / FreqUI management phase, not a trading runtime or
deployment phase.

- No live trading.
- No real order placement.
- No real exchange connection.
- No real K-line download.
- No real dry-run execution during acceptance.
- No committed API key, secret, token, or passphrase.
- No FreqUI embedding, proxying, control-surface replacement, or rewrite.
- No Freqtrade source-code modification.
- No Redis, Celery, Kafka, RabbitMQ, worker pool, or production deployment.
- No Phase 6 or Phase 7 implementation.

Real dry-run readiness remains fail-closed. The project may only run a real
dry-run path when a future issue explicitly allows it, local prerequisites are
present, required ENV variables exist, and the safety boundary is re-verified.
Missing prerequisites must return `BLOCKED`, not fabricated success.

## Phase 6 Readiness

Phase 5 is accepted. The project may move to Phase 6 planning only after the
`#163` closeout review is merged and Epic `#151` is closed / marked Done.

Phase 6 must separately define human approval, risk checks, deployment records,
rollback, monitoring, live-candidate governance, and explicit live-trading
safety gates before any live or production work is attempted.
