# Phase 9 Page Data Source Audit

This audit maps the user-visible Phase 9 pages to backend API endpoints,
database rows, source markers, fallback paths, and the follow-up issues required
to prove real local operation.

It is an audit only. It does not call DeepSeek, does not start Freqtrade, does
not rewrite pages, and does not mark fixture, fallback, mock, or unknown data as
real success.

## Audit Inputs

| Input | Evidence used |
| --- | --- |
| Frontend routes | `frontend/src/App.tsx` routes for Dashboard, Strategies, StrategyDetail, GenerationRuns, LocalStrategyLab, BacktestRuns, BacktestTasks, HyperoptRuns, LiveGovernance, OperatorDashboard, Ranking, and FreqUI. |
| Frontend data loader | `frontend/src/api/useMvpData.ts` and `frontend/src/api/client.ts` `loadMvpData`. |
| Backend APIs | `backend/app/api/strategies.py`, `strategy_generation.py`, `backtests.py`, `ranking.py`, `dry_run.py`, `runtime.py`, and `debug_mvp.py`. |
| Source contract | `backend/app/schemas/data_source.py` with `database`, `api_aggregate`, `fixture`, `fallback`, and `unknown`. |
| Database model surface | `strategies`, `strategy_versions`, `strategy_generation_runs`, `backtest_runs`, `backtest_tasks`, `backtest_results`, `strategy_scores`, `strategy_failure_reasons`, `local_test_batches`, `local_test_db_events`, and `debug_mvp_seed_payloads`. |
| Phase 9 gates | `docs/phase9_operational_readiness_plan.md`, `docs/phase9_local_readiness_preflight.md`, `docs/phase9_bug_issue_flow.md`, and `docs/phase9_deferred_scope.md`. |

## Source Semantics

| Source type | Core Phase 9 success | Required page behavior |
| --- | --- | --- |
| `database` | Yes | Show database identifiers and freshness or artifact refs when available. |
| `api_aggregate` | Yes, only with DB row IDs | Show aggregate source plus underlying row IDs or blocker. |
| `fixture` | No | Label as local/test/debug fixture and explain which real data is missing. |
| `fallback` | No | Label as fallback and explain the missing API, DB, config, file, or local dependency. |
| `mock` | No | Keep visibly separate from provider/database data; never count as success. |
| `unknown` | No | Show warning or `BLOCKED`; never pass QA as real evidence. |

## Global Loader Findings

The current frontend uses one shared MVP loader for most pages:

| Finding | Current behavior | Risk | Follow-up |
| --- | --- | --- | --- |
| Shared fallback switch | `useMvpData` initializes from `mockMvpData` and marks page source as `fallback` when all candidate API paths fail. | A single missing endpoint can push a whole page into fallback data. | `#275` must make no-real-data explanations page-specific, not just global. |
| Candidate endpoint probing | `fetchList` and `fetchValue` try multiple API paths and return fallback after failures. | Failed primary endpoints can be hidden unless the page exposes source details. | `#274` and `#275` must show endpoint/source evidence for core pages. |
| API base path | Frontend requests are built as `${VITE_API_BASE_URL || "/api"}${path}`. | Candidate paths without `/api` still become `/api/...`; backend routes without `/api` may never be reached. | Treat runtime/operator path mismatch as a high-priority page audit risk. |
| Debug MVP endpoint aliases | `/api/mvp/...` aliases read `debug_mvp_seed_payloads` and attach `fixture` data_source. | Seeded debug DB rows are useful for QA but cannot satisfy real Phase 9 success. | `#276` may use them for local test states; `#277` must not count them as real provider evidence. |
| Source marker components | Local Strategy Lab and some specialist pages can display `DataSourceTrace`. | Older list pages mostly show only global API/fallback state. | `#275` should extend explicit source markers to every core page. |

## Page Matrix

| Page / route | Primary UI data | Intended source | Backend endpoint candidates | Database IDs required for success | Current fallback / fixture risk | Priority | Follow-up |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Dashboard `/` | Counts for strategies, generation runs, backtests, hyperopt, ranking, top strategy. | `api_aggregate` from multiple DB-backed APIs. | `/api/strategies`, `/api/strategy-generation-runs`, `/api/backtest-runs`, `/api/backtest-tasks`, `/api/backtest-results`, `/api/ranking`, optional `/api/hyperopt-runs`. | At least one relevant `strategy_id`, `strategy_version_id`, `generation_run_id`, `backtest_run_id`, `backtest_result_id`, or `strategy_score_id` per displayed success metric. | High: global fallback can populate every count from `mockMvpData`; empty DB can look like a functioning dashboard unless no-data explanation is visible. | P1 | `#274` for real result display, `#275` for source and no-data explanation. |
| Strategies `/strategies` | Strategy list, status, source, current version link. | `database`. | `/api/strategies`, fallback `/api/mvp/strategies`. | `strategy_id`; when current version is shown, `strategy_version_id`. | Medium: `/api/mvp/strategies` is fixture and must not count as real strategy inventory. | P1 | `#270` creates real rows; `#275` should expose row/source markers. |
| StrategyDetail `/strategies/:strategyId` | Strategy summary, current version, generated code and validation state. | `database` plus artifact refs. | Same loader data: `/api/strategies`, `/api/strategy-versions`. | `strategy_id`, `strategy_version_id`, `generation_run_id` when generated by provider. | Medium: detail page depends on list payload; missing strategy can fall back to mock detail if global fallback is active. | P1 | `#270` and `#271` for durable records and file validation; `#275` for detail-level source display. |
| GenerationRuns `/generation-runs` | Provider/model run records, counts, status, failure reasons. | `database`. | `/api/strategy-generation-runs`, fallback `/api/mvp/generation-runs`. | `strategy_generation_run_id`; linked `strategy_id` and `strategy_version_id` when accepted. | High: fake/local providers and fixture runs can look similar unless provider is labeled and DB IDs are shown. | P1 | `#269` for DeepSeek ENV-only provider path; `#270` for persisted strategy/version linkage. |
| LocalStrategyLab `/local-strategy-lab` | Generation action, source trace rows, strategy versions, generation evidence, backtest evidence, ranking evidence, dry-run readiness/control. | `database` or `api_aggregate` for evidence; `BLOCKED` for missing prerequisites. | POST `/api/strategy-generation-runs`, POST `/api/backtest-runs/local`, POST `/api/dry-run/readiness`, POST `/api/dry-run/control/start`, GET APIs from shared loader. | `strategy_generation_run_id`, `strategy_id`, `strategy_version_id`, `backtest_run_id`, `backtest_task_id`, `backtest_result_id`, `strategy_score_id`; dry-run evidence requires manifest refs and local status refs. | Medium: this page already shows `DataSourceTrace`, but it can still rely on fallback snapshot when backend APIs are unavailable. | P0 | `#269` through `#273` for the real chain; `#275` to keep fallback explanation visible; `#277` for single real E2E. |
| BacktestRuns `/backtest-runs` | Backtest run status, matrix summary, task/result artifact hints. | `database` and `api_aggregate`. | `/api/backtest-runs`, `/api/backtest-tasks`, `/api/backtest-results`, fallback `/api/mvp/backtest-runs` and `/api/mvp/backtest-tasks`. | `backtest_run_id`, linked `strategy_version_id`, task IDs, result IDs, artifact refs. | High: seeded fixture or mock runs can show plausible metrics; no run should be accepted without DB IDs. | P1 | `#272` for preflight/task creation, `#273` for result/score persistence, `#274` for page display. |
| BacktestTasks `/backtest-tasks` | Per-pair/timeframe task status, config path, result path, errors. | `database`. | `/api/backtest-tasks`, fallback `/api/mvp/backtest-tasks`; detail endpoint `/api/backtest-tasks/{task_id}` exists. | `backtest_task_id`, `backtest_run_id`, `strategy_version_id`; result ID when succeeded. | High: `BLOCKED` and `FAILED` are core states and must not be hidden behind generic error or success labels. | P1 | `#272`, `#273`, `#274`, `#275`. |
| Ranking `/ranking` | Ranked strategy scores and score breakdown. | `api_aggregate` from `strategy_scores` joined to strategies/versions/results. | `/api/ranking`, fallback candidates `/api/strategy-ranking`, `/api/mvp/ranking`. | `strategy_score_id`, `strategy_id`, `strategy_version_id`; `backtest_result_id` for score tied to real backtest. | High: ranking can be fixture/mock and still look authoritative; fallback ranking is not Phase 9 success. | P1 | `#273` for score persistence, `#274` for UI display, `#275` for source markers. |
| FreqUI `/freq-ui` | Dry-run manifest, status snapshot, FreqUI link metadata, recent events. | Local status artifact or `BLOCKED`; not core provider/backtest success. | `/api/dry-run/management`, `/api/dry-run/status`, fallback `/api/mvp/dry-run`. | Dry-run does not currently have a persisted DB table; acceptance requires local manifest/status refs and explicit dry-run-only boundary. | Medium: dry-run fixtures can be useful but must not imply live trading or production readiness. | P2 | Keep under Phase 8/9 local-only dry-run boundary; `#330` may design controlled hourly use but not production scheduling. |
| OperatorDashboard `/operator-dashboard` | Runtime read-only contract, operator diagnostics, artifacts, env presence, audit events. | Read-only runtime artifacts and diagnostics; not live control. | Intended `/runtime/read-only` and `/runtime/operator-status`; frontend currently requests `/api/runtime/read-only` and `/api/runtime/operator-status` by default; audit event candidates are not currently backed by a routed API in `main.py`. | Runtime/operator evidence uses artifact refs, not strategy DB success IDs; governance events need archive IDs if exposed later. | High: route prefix mismatch can force fallback despite backend route existence. Operator pages must stay read-only and redact ENV values. | P1 | File a Bug if reproduced in browser/API; `#275` must explain fallback, `#280` reviews safety boundary. |
| LiveGovernance `/live-governance` | Live candidate governance, monitoring, approvals, rollback evidence. | Artifact/report/fixture until a DB-backed live-candidate model exists. | Frontend tries `/api/live-candidates/governance`, `/api/live-candidates`, `/api/mvp/live-candidates`; no active backend route is registered in `main.py`. | No Phase 9 DB success IDs currently defined for this page. | High: it is historical governance/read-only evidence, not Phase 9 real-run proof. | P2 | Keep read-only; defer production/live expansion per `#281`; source explanation via `#275`. |
| HyperoptRuns `/hyperopt-runs` | Hyperopt run summaries, best params, artifact manifest, comparison hints. | Artifact/report or future DB-backed API. | Frontend tries `/api/hyperopt-runs` and `/api/mvp/hyperopt-runs`; no active backend hyperopt router is registered in `main.py`. | No current core Phase 9 DB success IDs unless future schema adds hyperopt tables. | Medium: can remain useful as prior-phase artifact display but cannot prove Phase 9 provider/backtest success. | P3 | Defer or mark non-core; do not block Phase 9 unless used by a later review criterion. |

## API And Database Reconciliation Matrix

| API surface | Backing store / source | Success evidence | Current risk |
| --- | --- | --- | --- |
| `GET /api/strategies` | `strategies` via `StrategyRepository`. | Response rows include `database` data_source with `strategy_id`. | Empty DB and fallback data must be distinguishable on page. |
| `GET /api/strategy-versions` | `strategy_versions` via `StrategyRepository`. | Response rows include `strategy_version_id` and parent/generation refs when present. | Version list is global; detail pages must reconcile the selected strategy. |
| `GET /api/strategy-generation-runs` | `strategy_generation_runs`. | `strategy_generation_run_id`, provider, model, status, counts, timestamps. | Provider/fake/DeepSeek distinction must be explicit; failed runs must persist. |
| `POST /api/strategy-generation-runs` | Generation service writes run, strategies, versions, and files. | API aggregate with run/strategy/version database IDs. | Real DeepSeek should wait for `#269` and `#280`; fake provider cannot be labeled real. |
| `POST /api/backtest-runs/local` | `backtest_runs` and `backtest_tasks` when preflight passes or blocks. | Created/blocked run/task IDs and actionable `BLOCKED` reasons. | Missing binary/data/file must not create success. |
| `GET /api/backtest-runs` | `backtest_runs`. | `backtest_run_id`, status, strategy version, artifact refs. | Matrix summary can look successful without task/result reconciliation. |
| `GET /api/backtest-tasks` | `backtest_tasks`. | `backtest_task_id`, run ID, pair, timeframe, status, config/result paths. | Task success without result is a Bug. |
| `GET /api/backtest-results` | `backtest_results`. | `backtest_result_id`, task/run/version IDs, metrics snapshot, result path. | Metrics without matching task/run/page row are a Bug. |
| `GET /api/ranking` | `strategy_scores` joined to strategy/version/result records. | `strategy_score_id`, version ID, optional backtest result ID, scoring version. | Score not tied to a real result cannot prove Phase 9 backtest success. |
| `GET /api/dry-run/management` | Local manifest/status files through `DryRunControlService`. | Manifest path, status snapshot path, dry-run-only safety flags. | Not a core strategy/backtest DB success; must stay local/dry-run only. |
| `GET /runtime/read-only` | Read-only runtime contract service. | Runtime artifact refs and read-only safety flags. | Frontend default base path likely requests `/api/runtime/read-only`, so fallback may be used. |
| `GET /runtime/operator-status` | Operator diagnostics service. | Diagnostic status, env-presence booleans, no env values. | Same route prefix mismatch risk; no control actions allowed. |
| `GET /api/mvp/...` | `debug_mvp_seed_payloads` fixture payloads. | Fixture source with blocked/non-core semantics. | Useful for QA setup only; never Phase 9 core success. |

## Required Follow-Up Mapping

| Gap class | Existing follow-up | Completion requirement |
| --- | --- | --- |
| Real provider key/config/call boundary | `#269`, `#280` | ENV-only config, no secret persistence, safe failure record, minimal real call only when required. |
| Provider output persisted to DB | `#270` | `generation_run`, `strategy`, and `strategy_version` rows reconcile with API and page. |
| Generated strategy file evidence | `#271` | Approved file path, readable file, validation status, row/file linkage. |
| Local backtest prerequisite states | `#272` | Freqtrade binary, market data, config, strategy file, permissions, and artifact path produce `READY`, `BLOCKED`, or `FAILED`. |
| Backtest result and score persistence | `#273` | `backtest_task`, `backtest_result`, and `strategy_score` rows reconcile with API and page. |
| Backtest/ranking frontend display | `#274` | Backtest and ranking pages display DB-backed rows and failure states. |
| Source markers and no-real-data explanations | `#275` | Every core page shows database/api_aggregate/fixture/fallback/mock/unknown and user action when not real. |
| Local QA seed/reset/dirty states | `#276` | QA can construct success, failed, blocked, dirty, and empty DB states safely. |
| Single real DeepSeek E2E | `#277` | One controlled run proves provider/API/DB/UI chain or records explicit blockers without leaking secrets. |
| Hourly local run design | `#330` | Design only; no production scheduler or live trading; pause/cancel, single-use lease, no concurrency, and fail-closed preflight are required. |
| Bug intake | `#279` | Any discovered mismatch becomes one structured Bug issue. |
| Deferred production/live scope | `#281` | Keep live trading, production deployment, complex queues, and multi-model expansion out of Phase 9. |

## Bug Triggers From This Audit

Create a Bug issue when any of these are reproduced:

- Page displays success while direct DB query has no matching row.
- Direct DB row exists but page does not show it after refresh.
- API returns `database` or `api_aggregate` with `core_data=true` but no database IDs.
- `fixture`, `fallback`, `mock`, or `unknown` appears as real provider/backtest/ranking success.
- Runtime/operator backend endpoint exists but frontend only reaches fallback because of route prefix mismatch.
- Strategy generation succeeds without `strategy_generation_run`, `strategy`, and `strategy_version` rows.
- Generated strategy page shows success while the file path is missing, unsafe, or unreadable.
- Backtest task shows `succeeded` with no `backtest_result`.
- Ranking row claims real score without a persisted `strategy_score` and traceable `backtest_result_id`.
- Any page, API, report, PR, Issue, log, screenshot, or DB payload exposes a real key, token, passphrase, or provider secret.

## QA Reconciliation Checklist

For each core page, QA must verify all three layers:

| Layer | Check |
| --- | --- |
| Browser | Refresh the page and record displayed source type, IDs, status, blocker, and artifact refs. |
| API | Call the page's endpoint candidates and verify source type, core_data, database_ids, and HTTP errors. |
| Database | Query the expected local table by displayed IDs and confirm rows exist or the page explains why they do not. |
| Negative state | Remove or omit one prerequisite and confirm the page/API shows `BLOCKED`, `FAILED`, `fixture`, `fallback`, or `unknown` instead of success. |
| Security | Confirm no real secret value appears in page text, API payload, logs, DB rows, GitHub issues, or PR text. |

## Acceptance For Issue #267

Issue `#267` is complete when this audit is merged and can be used to drive the
remaining Phase 9 work:

- every current page has an intended source, endpoint candidates, DB evidence,
  fallback risk, and priority;
- non-core pages are explicitly marked as artifact/read-only/deferred instead of
  real Phase 9 success;
- existing follow-up issues cover each page gap or define when a Bug must be
  created;
- DeepSeek remains uncalled and secret-free in this audit;
- live trading, real orders, production deployment, Freqtrade source edits, and
  unapproved queue infrastructure remain out of scope.
