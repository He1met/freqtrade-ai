# Issue #341 Regression QA Report

## Scope

- Issue: `#341` `[QA][Refactor] 重构后真实运行链路回归验收`
- Worktree: `/private/tmp/freqtrade-ai-341`
- Branch: `codex/issue-341-refactor-regression-qa`
- Baseline: `origin/main` @ `ab1f20d` (`docs: finalize hourly controlled run design (#353)`)
- Report time: `2026-07-11 Asia/Shanghai`

## Verdict

- Overall: `PARTIAL PASS`
- Backend / compile / frontend build / secret scan: `PASS`
- Offline regression smoke: `PASS`
- Page/API/DB reconciliation on service-backed local startup: `PASS`
- Real DeepSeek single-run path: `BLOCKED`

This report is intentionally sanitized. It records command outcomes, guarded
temporary evidence paths, and fail-closed blockers only. No credential values,
live trading actions, exchange connections, or production resources were used.

## Stale Report Check

An older manual report exists only in the separate primary checkout as an
untracked historical artifact:

- file: `reports/manual/local_ui_e2e_report.md`
- report date: `2026-07-05 20:17:04 CST`
- old commit: `b7ca77fd2350938329f5a8a91bf3ffca692607ee`

That report does not belong to the current `origin/main` commit under this
worktree, so it is stale for `#341` and must not be reused as current evidence.

## Validation Matrix

| Command | Result | Evidence |
| --- | --- | --- |
| `/Users/shenjianpeng/Documents/Freqtrade Ai/backend/.venv/bin/python -m pytest backend/tests` | `PASS` | `393 passed in 8.82s` |
| `python3 -m compileall backend/app backend/tests scripts` | `PASS` | compile completed without syntax errors |
| `cd frontend && npm run build` | `PASS` | Vite build succeeded; `70 modules transformed` |
| `python3 scripts/scan_secrets.py` | `PASS` | `scanned 215 files; no secret-shaped values found` |
| `python3 scripts/scan_secrets.py --include-untracked` | `PASS` | `scanned 215 files; no secret-shaped values found` |
| `python3 scripts/smoke_phase7.py --offline --tmp-dir /tmp/freqtrade-ai-phase7-smoke-341` | `PASS` | runtime/operator dashboard/secret scan smoke passed |
| `python3 scripts/smoke_phase8.py --offline --tmp-dir /tmp/freqtrade-ai-phase8-smoke-341 --json` | `PASS` | DB/API reconciliation passed with guarded local SQLite |
| `python3 scripts/smoke_phase8.py --tmp-dir /tmp/freqtrade-ai-phase8-full-341-rerun --backend-port 8108 --frontend-port 4173 --backend-url http://127.0.0.1:8108 --frontend-url http://127.0.0.1:4173 --json` | `PASS` | backend/frontend startup, HTTP API, and `/local-strategy-lab` page delivery passed |
| `python3 scripts/phase9_deepseek_single_e2e.py --json` | `BLOCKED` | no real call authorized; `DEEPSEEK_API_KEY` absent |

## Execution Notes

- The first frontend build attempt in the fresh worktree failed because the
  worktree had no local `node_modules`. After a clean `npm ci` in the worktree,
  `npm run build` passed. This was an environment bootstrap issue, not a code
  regression in `origin/main`.
- One non-offline `smoke_phase8.py` attempt initially reported `BLOCKED`
  because the probe URL was left at the default port while the backend was
  started on `8108`. A corrected rerun with matching `--backend-port` and
  `--backend-url` passed. The initial timeout was an operator invocation
  mistake, not product evidence.

## Current Commit Evidence

### Backend Regression

- Full backend suite passed on the current baseline.
- The suite includes the Phase 8 and Phase 9 coverage relevant to `#341`,
  including:
  - `backend/tests/test_phase8_data_source_contract.py`
  - `backend/tests/test_phase8_local_test_db.py`
  - `backend/tests/test_phase9_deepseek_single_e2e.py`
  - `backend/tests/test_phase9_live_api_contract.py`
  - `backend/tests/test_strategy_generation_service.py`
  - `backend/tests/test_strategy_generation_api.py`

### Page / API / DB Reconciliation

`scripts/smoke_phase8.py` produced current-commit evidence that:

- direct database reconciliation passed for:
  - `strategies`
  - `strategy_generation_runs`
  - `strategy_versions`
  - `backtest_runs`
  - `backtest_tasks`
  - `backtest_results`
  - `strategy_scores`
- API reconciliation passed for:
  - `/health`
  - `/api/strategies`
  - `/api/strategy-versions`
  - `/api/strategy-generation-runs`
  - `/api/backtest-runs`
  - `/api/backtest-tasks`
  - `/api/backtest-results`
  - `/api/ranking`
- service-backed page delivery passed for `/local-strategy-lab` with HTTP `200`
  on a local guarded run.

The smoke evidence also confirmed that non-core fixture/unknown data remains
explicitly non-acceptance evidence:

- offline evidence source counts: `database=2`, `fixture=11`, `unknown=1`
- dirty/local-test states remained present for QA only and did not satisfy core
  success

Evidence artifacts generated during this run:

- `/tmp/freqtrade-ai-phase7-smoke-341/repo-fixture/reports/runtime/phase7-smoke-summary.json`
- `/tmp/freqtrade-ai-phase8-smoke-341/reports/phase8-e2e-evidence.json`
- `/tmp/freqtrade-ai-phase8-full-341-rerun/reports/phase8-e2e-evidence.json`
- `/tmp/freqtrade-ai-phase9-deepseek-e2e/phase9-deepseek-single-e2e-evidence.json`

## Browser / Provider / External Capability Status

| Capability | Status | Evidence |
| --- | --- | --- |
| Local Chrome presence | `AVAILABLE` | local Chrome app detected on this machine |
| Service-backed page delivery | `PASS` | `/local-strategy-lab` returned HTTP `200` in the guarded Phase 8 rerun |
| Full interactive browser console/DOM capture | `NOT RUN` | current repo smoke script validates startup and page delivery; no extra browser screenshot run was added to this docs-only QA task |
| `DEEPSEEK_API_KEY` in local environment | `MISSING` | env-presence check returned missing |
| Real DeepSeek request | `BLOCKED` | `phase9_deepseek_single_e2e.py --json` persisted fail-closed blocked evidence |
| External network dependency for real provider | `NOT EXERCISED` | no authorized real provider request was sent |

## Real DeepSeek Fail-Closed Result

`scripts/phase9_deepseek_single_e2e.py --json` returned `BLOCKED` on the
current baseline with these relevant facts:

- `credential_env_present=false`
- `allow_real_call=false`
- `real_call_attempted=false`
- one durable `strategy_generation_runs` row was recorded
- no `strategies`, `strategy_versions`, `backtest_runs`, `backtest_results`, or
  `strategy_scores` rows were created
- next action remains: set local `DEEPSEEK_API_KEY`, explicitly authorize the
  single real call, then provide local backtest prerequisites/artifacts if
  preflight reaches `READY`

This matches the issue requirement to mark missing real-provider prerequisites
as `BLOCKED` rather than pretending success.

## Safety Boundary

Confirmed during this run:

- no live trading
- no real orders
- no exchange connection
- no market-data download
- no production deployment
- no credential value written to code, report, or output

## Conclusion

For the current `origin/main` commit `ab1f20d`, regression QA evidence shows:

- the guarded local backend, compile, frontend build, secret scanning, and
  offline smoke paths are healthy;
- the current commit can reconcile Local Strategy Lab evidence across database,
  backend API, and service-backed page delivery;
- fixture, fallback, dirty, and unknown states remain non-core evidence;
- the real DeepSeek path is correctly fail-closed and currently `BLOCKED`
  because the required local credential and explicit authorization are absent.
