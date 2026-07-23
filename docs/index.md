# Freqtrade AI Current Run Docs

Last updated: 2026-07-22

This is the current documentation entry point for local real-run validation,
runtime evidence, refactor work, and safety boundaries.

## Current Objective

The final Phase 9 refactor/runtime implementation item is Issue `#369`, a
single-process DB-backed local worker with durable idempotency, lease,
heartbeat, expiry, pause, cancel, and restart-safe evidence. The goal is not to
add a new trading capability or scheduler. The goal is to move the existing
DeepSeek-to-backtest chain out of a long HTTP request while keeping browser,
API, database, and artifact evidence reconcilable:

- core pages prefer real database / backend API evidence;
- `fixture`, `fallback`, `mock`, and `unknown` data stay visibly non-core;
- empty states explain why no real data exists and what to do next;
- core actions show `success`, `failed`, or `BLOCKED` evidence;
- QA can reconcile browser, API, and database state.

## Current Issue Queue

| Issue | Purpose | Status on 2026-07-22 |
| --- | --- | --- |
| [#369](https://github.com/He1met/freqtrade-ai/issues/369) | Single-process DB-backed worker, lease, and failure recovery | Delivered by this change; live status remains in Project #3 |
| [#362](https://github.com/He1met/freqtrade-ai/issues/362) | Phase 9 runtime/refactor Epic closeout | Tracked separately after #369 acceptance |

All other child dependencies for `#369` are complete. The historical queue
snapshots remain available through the phase acceptance and planning documents;
they are not a current source of work status.

## Primary Runtime Docs

- [phase9_operational_readiness_plan.md](phase9_operational_readiness_plan.md)
- [phase9_acceptance.md](phase9_acceptance.md)
- [phase9_deepseek_single_e2e.md](phase9_deepseek_single_e2e.md)
- [phase9_deepseek_backtest_loop.md](phase9_deepseek_backtest_loop.md)
- [phase9_db_backed_worker.md](phase9_db_backed_worker.md)
- [phase9_page_data_source_audit.md](phase9_page_data_source_audit.md)
- [phase9_bug_issue_flow.md](phase9_bug_issue_flow.md)
- [phase9_security_boundary_review.md](phase9_security_boundary_review.md)
- [phase8_acceptance.md](phase8_acceptance.md)
- [phase8_e2e_reconciliation.md](phase8_e2e_reconciliation.md)
- [phase8_local_strategy_lab_plan.md](phase8_local_strategy_lab_plan.md)

## Validation Entry Points

Use the narrowest command that proves the affected surface. For refactor PRs,
also include `git diff --check` and `python3 scripts/scan_secrets.py`.

| Surface | Command |
| --- | --- |
| Backend | `(cd backend && . .venv/bin/activate && pytest)` |
| DB-backed worker, at most one job | `(cd backend && .venv/bin/python -m app.workers.deepseek_backtest_worker --once)` |
| Python syntax | `python3 -m compileall backend/app backend/tests scripts` |
| Frontend | `(cd frontend && npm run build)` |
| Phase 8 local QA | `python3 scripts/smoke_phase8.py --offline --tmp-dir /tmp/freqtrade-ai-phase8-smoke` |
| Phase 9 single E2E, safe default | `python3 scripts/phase9_deepseek_single_e2e.py --json` |
| Phase 9 single E2E, real call | `python3 scripts/phase9_deepseek_single_e2e.py --allow-real-call --json` |
| Secret scan | `python3 scripts/scan_secrets.py` |
| Whitespace | `git diff --check` |

Real DeepSeek calls require local operator approval and a local ENV key. Never
write the key to code, config, database, logs, UI, screenshots, reports, Issue,
or PR text.

## Safety Boundary

Allowed local work:

- local DeepSeek API validation with explicit authorization;
- one local DB-backed research worker for the explicitly queued job;
- local database read/write and local test DB reset/seed;
- local strategy file writes in approved directories;
- local backtest and local controlled dry-run readiness checks;
- browser/API/database reconciliation.

Forbidden work:

- live trading, real orders, production deployment, automatic live deployment;
- switching dry-run to live;
- start/stop/deploy live controls;
- modifying Freqtrade source;
- committing or reporting real secrets;
- adding hourly or recurring scheduling, Redis, Celery, Kafka, RabbitMQ,
  production queues, distributed workers, or worker pools.

## Historical Phase Archive

These docs are retained as historical evidence and should not be deleted during
current-run cleanup:

- Phase 1: [phase1_acceptance.md](phase1_acceptance.md)
- Phase 2: [phase2_acceptance.md](phase2_acceptance.md),
  [phase2_architecture_review.md](phase2_architecture_review.md),
  [phase2_real_freqtrade_backtest_spike.md](phase2_real_freqtrade_backtest_spike.md)
- Phase 3: [phase3_acceptance.md](phase3_acceptance.md),
  [phase3_plan.md](phase3_plan.md)
- Phase 4: [phase4_acceptance.md](phase4_acceptance.md),
  [phase4_hyperopt_design.md](phase4_hyperopt_design.md),
  [post_phase4_cleanup.md](post_phase4_cleanup.md)
- Phase 5: [phase5_acceptance.md](phase5_acceptance.md),
  [phase5_plan.md](phase5_plan.md),
  [phase5_pr127_split_decision.md](phase5_pr127_split_decision.md)
- Phase 6: [phase6_acceptance.md](phase6_acceptance.md),
  [phase6_live_candidate_plan.md](phase6_live_candidate_plan.md)
- Phase 7: [phase7_acceptance.md](phase7_acceptance.md),
  [phase7_engineering_plan.md](phase7_engineering_plan.md),
  [phase7_ci.md](phase7_ci.md),
  [phase7_secret_scanning.md](phase7_secret_scanning.md),
  [phase7_worker_queue_design.md](phase7_worker_queue_design.md)
