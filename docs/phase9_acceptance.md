# Phase 9 Acceptance Review

Date: 2026-07-06 Asia/Shanghai

Verdict: Phase 9 is accepted for the local-only Operational Readiness scope.
The project now has durable database/API/UI evidence paths, explicit
fail-closed provider and backtest boundaries, data-source explanations, QA seed
coverage, Bug intake rules, and a design-only hourly local run boundary.

This acceptance does not authorize live trading, real orders, production
deployment, `freqtrade trade`, production queue infrastructure, or unattended
hourly execution.

## Project Closeout Evidence

All required Phase 9 child issues are closed and `Done` in Project #3 except
the review and Epic items that this acceptance PR closes:

| Issue | Result |
| --- | --- |
| `#266` Phase 9 plan and execution order | Closed / Done |
| `#267` page real-data source audit | Closed / Done |
| `#268` local readiness preflight matrix | Closed / Done |
| `#269` DeepSeek ENV-only provider boundary | Closed / Done |
| `#270` provider result to DB chain | Closed / Done |
| `#271` generated strategy file validation | Closed / Done |
| `#272` local backtest preflight checks | Closed / Done |
| `#273` backtest result and score chain | Closed / Done |
| `#274` real data evidence in frontend pages | Closed / Done |
| `#275` source marker explanations | Closed / Done |
| `#276` local test DB seed/reset/dirty/failure states | Closed / Done |
| `#277` single DeepSeek E2E evidence runner | Closed / Done |
| `#330` hourly local controlled run design | Closed / Done |
| `#279` Bug issue flow | Closed / Done |
| `#280` security boundary review | Closed / Done |
| `#281` deferred Phase 10 boundary | Closed / Done |

Merged PR evidence for this phase is `#283` through `#298`.

## What Is Accepted

Phase 9 accepts these capabilities:

- DeepSeek can be configured through an ENV-only provider boundary without
  persisting credential values.
- Provider failures and missing authorization create durable failed generation
  records instead of fake success.
- Strategy generation can persist `strategy_generation_run`, `strategy`, and
  `strategy_version` records with provider/model metadata.
- Generated strategy files are written to approved local directories and
  exposed with file state.
- Local backtest creation is guarded by structured preflight checks.
- Backtest artifact ingest persists results and scores, and fails closed when
  parsing or scoring cannot complete.
- Core frontend pages display database IDs, artifact refs, data-source markers,
  and required actions.
- Fixture, fallback, mock, unknown, dirty, and local-test data are visibly
  non-core and cannot satisfy acceptance.
- QA can safely reset/seed local test DB scenarios for success, failure,
  `BLOCKED`, dirty data, missing artifact, partial completion, and unknown
  source states.
- Bug intake rules exist for page/API/DB/provider/backtest/data-source/security
  mismatches.
- Hourly local generation/backtest is documented as a future design only.

## Current Blocked Or Deferred Items

These are accepted gaps, not hidden successes:

- No real DeepSeek API call was made in the #277 PR or this review. The local
  environment did not expose `DEEPSEEK_API_KEY`, and the project policy is to
  avoid real calls unless explicitly authorized.
- A successful real-provider-to-score run still requires a local operator to
  set the credential in the environment, pass `--allow-real-call`, satisfy local
  market data/Freqtrade preflight, and supply a real backtest artifact if
  preflight reaches `READY`.
- Hourly local execution is design-only. No scheduler, loop, worker, or queue
  implementation was added.
- Production deployment, live trading, real orders, exchange trading
  connections, and production queue infrastructure remain Phase 10 or later
  decisions.

## Evidence Surfaces

Primary Phase 9 docs and tools:

- [phase9_operational_readiness_plan.md](phase9_operational_readiness_plan.md)
- [phase9_page_data_source_audit.md](phase9_page_data_source_audit.md)
- [phase9_local_readiness_preflight.md](phase9_local_readiness_preflight.md)
- [phase9_security_boundary_review.md](phase9_security_boundary_review.md)
- [phase9_bug_issue_flow.md](phase9_bug_issue_flow.md)
- [phase9_deferred_scope.md](phase9_deferred_scope.md)
- [phase9_local_test_db.md](phase9_local_test_db.md)
- [phase9_deepseek_single_e2e.md](phase9_deepseek_single_e2e.md)
- [phase9_hourly_local_controlled_run_design.md](phase9_hourly_local_controlled_run_design.md)
- [../scripts/phase9_deepseek_single_e2e.py](../scripts/phase9_deepseek_single_e2e.py)
- [../scripts/phase8_local_test_db.py](../scripts/phase8_local_test_db.py)

## Validation Evidence

Final local validation used for this review:

- `backend/.venv/bin/python -m pytest backend/tests`
- `npm run build` in `frontend`
- `python3 scripts/scan_secrets.py --include-untracked`
- `python3 scripts/scan_secrets.py`
- `git diff --check`

The last full backend run passed 372 tests. The frontend build passed. Secret
scan passed without findings.

## Security Boundary

Phase 9 preserved these safety rules:

- no credential value in code, config, DB, report, UI, Issue, or PR;
- no live trading or real orders;
- no `freqtrade trade`;
- no production deployment;
- no Freqtrade source edits;
- no Redis, Celery, Kafka, RabbitMQ, worker pool, or production queue
  infrastructure;
- no fixture/fallback/mock/unknown data presented as real provider success.

## Next Step

The only next operational step is to run the controlled #277 script with local
operator approval if a real DeepSeek success path is needed:

```bash
python3 scripts/phase9_deepseek_single_e2e.py --allow-real-call --json
```

If that run reaches local backtest preflight `READY`, supply a real local
backtest manifest or result artifact. Any productionization, hourly runner
implementation, queue infrastructure, live trading, or deployment work must be
opened as a new Phase 10 or later issue.
