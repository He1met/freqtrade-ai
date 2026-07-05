# Phase 6 Acceptance

## Status

Phase 6 live-candidate and deployment governance has passed final acceptance.

This acceptance result does not authorize live trading, real order placement,
exchange connectivity, live bot startup, production deployment, or Phase 7
implementation. Phase 6 remains a governance and audit layer only.

## Completed Scope

The Phase 6 scope is complete through the following issues and merged PRs:

| Issue | PR | Scope | Status |
| --- | --- | --- | --- |
| `#177` | `#186` | Live-candidate and deployment governance design plan | Done |
| `#178` | `#187` | `LiveCandidateProfile` schema and entry-condition lock | Done |
| `#179` | `#188` | Risk checklist and fail-closed preflight | Done |
| `#180` | `#189` | Human approval record and state machine | Done |
| `#181` | `#190` | `DeploymentRecord` schema and rollback plan | Done |
| `#182` | `#191` | Read-only runtime monitoring and alert summary DTOs | Done |
| `#183` | `#192` | Read-only frontend page for approvals and deployment records | Done |
| `#184` | `#193` | Phase 6 offline governance smoke | Done |

Epic `#176` remains an XL aggregation issue and is not a direct development
target. It can be closed and marked Done after the `#185` closeout review PR is
merged.

## Acceptance Commands

Final acceptance was run on `2026-07-05` from the `#185` closeout branch based
on `origin/main` commit `d6e75fd`. The closeout branch contains documentation
updates only.

```bash
python3 scripts/smoke_phase6.py --offline --tmp-dir /tmp/freqtrade-ai-phase6-smoke
```

Result: PASS.

Observed smoke coverage:

- `LiveCandidateProfile` and offline evidence manifest validation passed.
- Risk preflight returned `APPROVED_FOR_REVIEW` only for the review path.
- Manual approval state machine reached `APPROVED_FOR_DEPLOYMENT_RECORD`.
- Deployment governance records required a rollback plan.
- Read-only monitoring summary parsed without exposing control actions.
- Missing risk evidence, missing manual approval, missing rollback plan, and
  monitoring control input returned `BLOCKED`.
- Risk threshold breach returned `FAILED`.
- Smoke summary confirmed no real Freqtrade execution, exchange connection,
  market-data download, live trading, real order, credential read, or production
  deployment execution occurred.

```bash
cd backend && . .venv/bin/activate && pytest
```

Result: PASS, `296 passed`.

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

Phase 6 adds an auditable governance layer for future live-candidate decisions:

- documented live-candidate and deployment governance boundaries;
- `LiveCandidateProfile` schema with locked candidate variables and
  secret-shaped input rejection;
- fail-closed risk checklist and preflight summaries;
- human approval records and state transitions;
- `DeploymentRecord` audit schema and mandatory rollback plans;
- read-only runtime and alert summary DTOs sourced from controlled fixtures,
  artifacts, or local JSON;
- read-only frontend visibility for candidates, blocked reasons, approvals,
  deployment records, rollback plans, and monitoring alerts;
- repeatable offline smoke validation covering success, `BLOCKED`, and `FAILED`
  paths.

## Current Limits

Phase 6 is not a trading runtime or deployment executor.

- No live trading.
- No real order placement.
- No real exchange connection.
- No real K-line download.
- No automatic live bot startup.
- No production deployment execution.
- No deployment executor or start / stop / deploy live controls.
- No committed API key, secret, token, or passphrase.
- No Freqtrade source-code modification.
- No Redis, Celery, Kafka, RabbitMQ, worker pool, or production infrastructure.
- No Phase 7 implementation.

Risk preflight approval only means a candidate may enter human review. Human
approval only unlocks governance records. Neither state grants permission to
start a live bot, place orders, connect to an exchange, or deploy to production.

## Phase 7 Readiness

Phase 6 is accepted. The project must pause before Phase 7 until Phase 7 is
explicitly planned through new issues, acceptance criteria, and safety review.

Any future live or production work must start with a separate Phase 7 plan and
must not infer live-trading authorization from this Phase 6 acceptance.
