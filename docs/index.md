# Freqtrade AI Current Run Docs

Last updated: 2026-07-08

This is the current documentation entry point for local real-run validation,
runtime evidence, refactor work, and safety boundaries.

## Current Objective

The active work is the Phase 9 refactor/runtime queue. The goal is not to add a
new trading capability. The goal is to make the local product more truthful and
operable:

- core pages prefer real database / backend API evidence;
- `fixture`, `fallback`, `mock`, and `unknown` data stay visibly non-core;
- empty states explain why no real data exists and what to do next;
- core actions show `success`, `failed`, or `BLOCKED` evidence;
- QA can reconcile browser, API, and database state.

## Current Issue Queue

| Issue | Purpose | Status on 2026-07-08 |
| --- | --- | --- |
| [#323](https://github.com/He1met/freqtrade-ai/issues/323) | Real-data-first runtime policy | Open |
| [#324](https://github.com/He1met/freqtrade-ai/issues/324) | Pages show only real DB/API data or explain absence | Open |
| [#325](https://github.com/He1met/freqtrade-ai/issues/325) | Core action button feedback | Open |
| [#326](https://github.com/He1met/freqtrade-ai/issues/326) | DeepSeek single real-provider entry | Open |
| [#327](https://github.com/He1met/freqtrade-ai/issues/327) | DeepSeek-to-backtest minimal loop | Open |
| [#328](https://github.com/He1met/freqtrade-ai/issues/328) | Local Strategy Lab real-run chain display | Open |
| [#329](https://github.com/He1met/freqtrade-ai/issues/329) | Browser/API/database reconciliation | Open |
| [#330](https://github.com/He1met/freqtrade-ai/issues/330) | Hourly local controlled-run design | Open / deferred |
| [#331](https://github.com/He1met/freqtrade-ai/issues/331) | Runtime failure to Bug Issue flow | Open |
| [#332](https://github.com/He1met/freqtrade-ai/issues/332) | Page acceptance status marker | Open |
| [#333](https://github.com/He1met/freqtrade-ai/issues/333) | Backend operation evidence structure | Open |
| [#334](https://github.com/He1met/freqtrade-ai/issues/334) | DeepSeek single E2E QA report | Open |
| [#335](https://github.com/He1met/freqtrade-ai/issues/335) | Refactor/runtime audit | Done |
| [#336](https://github.com/He1met/freqtrade-ai/issues/336) | Split frontend API client and source helpers | Open |
| [#337](https://github.com/He1met/freqtrade-ai/issues/337) | Split Local Strategy Lab evidence panels | Open |
| [#338](https://github.com/He1met/freqtrade-ai/issues/338) | Unified data-source acceptance helper | Done |
| [#339](https://github.com/He1met/freqtrade-ai/issues/339) | Current docs entry and historical archive index | This PR |
| [#340](https://github.com/He1met/freqtrade-ai/issues/340) | Legacy draft PR cleanup | Done |
| [#341](https://github.com/He1met/freqtrade-ai/issues/341) | Refactor regression QA | Open |

## Primary Runtime Docs

- [phase9_operational_readiness_plan.md](phase9_operational_readiness_plan.md)
- [phase9_acceptance.md](phase9_acceptance.md)
- [phase9_deepseek_single_e2e.md](phase9_deepseek_single_e2e.md)
- [phase9_deepseek_backtest_loop.md](phase9_deepseek_backtest_loop.md)
- [phase9_local_test_db.md](phase9_local_test_db.md)
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
- adding Redis, Celery, Kafka, RabbitMQ, production queues, or worker pools
  without a separate approved Issue.

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
