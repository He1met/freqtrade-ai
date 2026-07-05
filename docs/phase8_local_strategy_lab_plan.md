# Phase 8 Local Strategy Lab Plan

## Current Status

Phase 7 engineering upgrade and scalable operation readiness has passed final
acceptance. Phase 8 is now opened as a separately scoped local validation phase
through Epic issue `#232`.

Phase 8 does not mean live trading, profit validation, production deployment, or
automatic bot operation. It means proving that the project can be used as a
normal local software product: the user can act from the page, the backend API
can perform the action, the database can persist the result, and QA can
reconcile the page, API, database, files, and artifacts.

## Phase 8 Definition

Phase 8 is Local Strategy Lab / local real-run validation.

The target user workflow is:

1. Enter a strategy idea on the page.
2. Generate a strategy through the backend.
3. Persist the strategy, strategy version, and generation run in the database.
4. Write the generated strategy file into an approved local runnable directory.
5. Show database-backed strategies, versions, file status, and generation
   status on the page.
6. Trigger a local backtest from the page.
7. Persist backtest runs, tasks, results, artifact references, and failure or
   blocked reasons in the database.
8. Show database-backed backtest results and strategy scores on the page.
9. Preserve data after page refresh.
10. Show the source of each core record as database/API, fixture, fallback, or
    unknown.
11. Check dry-run readiness only after the strategy generation and backtest
    chain is proven.
12. Allow controlled local dry-run only when readiness, safety, and manual
    approval gates are satisfied.

## What Must Become Real

Phase 8 must move these surfaces from "available in pieces" to "provable as a
local product workflow":

- Frontend pages must call backend APIs for core actions.
- Backend APIs must write durable database records for core success paths.
- Page success must be backed by database rows and traceable IDs.
- Strategy generation must not only live in frontend state or memory.
- Strategy versions, generated files, generation runs, backtest tasks, backtest
  results, and scores must be linked.
- Backtest success must not be represented by fake result rows unless clearly
  marked as fixture-only.
- Missing local dependencies must produce `BLOCKED`, not a fake success state.
- Data displayed after refresh must come from API/database reads, not local
  component state.

## Data Source Rules

| Source | Core acceptance use | Required behavior |
| --- | --- | --- |
| Real database data | Allowed | Show traceable database identifiers and API source. |
| Backend API aggregate data | Allowed if traceable | Include database IDs, artifact/file refs, and source metadata. |
| Local fixture data | Not core success | Mark as `fixture`; use only for tests and debug display. |
| Frontend fallback data | Not core success | Mark as `fallback`; never hide the backend/API failure. |
| Unknown source | Not accepted | Show `BLOCKED`, `FAILED`, or unknown-source warning. |

Page success with no database record is a failure. Backend success with no page
visibility is a failure. Fallback or fixture data presented as real database
evidence is a failure.

## Phase 8 Issue Plan

| Issue | Title | Initial Project status |
| --- | --- | --- |
| `#232` | `[EPIC][Phase 8] Local Strategy Lab 本地真实运行验证` | Backlog |
| `#233` | `[Design][Phase 8] Local Strategy Lab 总体设计与验收口径` | Ready |
| `#234` | `[Backend][Phase 8] API/DB 数据真实性契约与来源标识` | Backlog |
| `#235` | `[Test][Phase 8] 本地测试数据库 reset/seed/dirty-data 能力` | Backlog |
| `#236` | `[Backend][Phase 8] 策略想法提交到生成记录与策略版本落库` | Backlog |
| `#237` | `[Backend][Phase 8] 策略文件写入可运行目录与可运行性验证` | Backlog |
| `#238` | `[Frontend][Phase 8] 页面提交策略想法并展示生成状态` | Backlog |
| `#239` | `[Backend][Phase 8] 页面触发本地回测任务与 fail-closed 前置检查` | Backlog |
| `#240` | `[Backend][Phase 8] 回测 artifact 解析入库与任务/策略版本追踪` | Backlog |
| `#241` | `[Backend][Phase 8] 策略评分与排行榜真实数据库链路` | Backlog |
| `#242` | `[Frontend][Phase 8] 策略/版本/回测/评分真实数据展示` | Backlog |
| `#243` | `[Frontend][Phase 8] 数据来源标识与 fallback/fixture 防冒充` | Backlog |
| `#244` | `[Test][Phase 8] 页面/API/数据库三方对账 E2E 验收` | Backlog |
| `#245` | `[Security][Phase 8] 本地 dry-run readiness 预检与 BLOCKED 展示` | Backlog |
| `#246` | `[DevOps][Phase 8] 本地受控 dry-run 启停边界与状态快照` | Backlog |
| `#247` | `[Review][Phase 8] Local Strategy Lab 阶段验收与安全边界审查` | Backlog |

Only `#233` should be Ready at phase start. All other Phase 8 issues should
remain Backlog until their predecessors are complete and their acceptance
criteria are still valid.

## Execution Order

Phase 8 should proceed in this order:

1. Complete the Local Strategy Lab design and acceptance contract in `#233`.
2. Implement API/database source and traceability contracts in `#234`.
3. Add local database reset, seed, dirty-data, and test-batch support in `#235`.
4. Persist strategy idea submission, generation runs, strategies, and versions
   in `#236`.
5. Write and validate local runnable strategy files in `#237`.
6. Add the page action for strategy idea submission and generation status in
   `#238`.
7. Trigger local backtest runs from page/API with fail-closed preflight in
   `#239`.
8. Parse and persist local backtest artifacts and result traceability in `#240`.
9. Tie scoring and ranking to real database `BacktestResult` rows in `#241`.
10. Display database-backed strategies, versions, files, backtests, results, and
    scores in `#242`.
11. Make source markers and fallback/fixture guards visible in `#243`.
12. Add page/API/database reconciliation E2E checks in `#244`.
13. Add dry-run readiness checks and `BLOCKED` display in `#245`.
14. Add controlled local dry-run only after readiness and human approval gates
    are satisfied in `#246`.
15. Close the phase with acceptance and security review in `#247` and Epic
    `#232`.

## Acceptance Approach

Phase 8 acceptance must be product-flow based, not command-only.

The required evidence is:

- Local backend starts.
- Local frontend starts.
- Frontend can reach backend APIs.
- Core pages do not white-screen.
- No severe console errors on the checked flow.
- No key API 404 or 500 on the checked flow.
- User can trigger strategy generation from the page.
- Strategy generation writes database records.
- Strategy file status is persisted and visible.
- User can trigger local backtest from the page when prerequisites are met.
- Backtest task/result records are persisted and traceable to strategy version
  and strategy file.
- Strategy scores are persisted and visible.
- Page refresh still shows database-backed records.
- QA can reconcile page, API response, and direct database query.
- Fixture, fallback, and unknown-source data are visible and not accepted as
  core success.
- Dry-run readiness reports `READY` or `BLOCKED` from real local checks.
- Any controlled dry-run evidence remains local, dry-run-only, redacted, and
  explicitly non-live.

## Safety Boundary

Every Phase 8 issue must preserve these boundaries:

- Do not execute real orders.
- Do not start live trading.
- Do not connect to a real exchange.
- Do not download real K-line data.
- Do not operate production, shared, remote, or unknown databases.
- Do not commit real API keys, secrets, tokens, or passphrases.
- Do not write secrets to code, configuration, databases, logs, documents,
  issues, pull requests, screenshots, or test fixtures.
- Do not modify Freqtrade source code.
- Do not implement Redis, Celery, Kafka, RabbitMQ, worker pool, or production
  queue infrastructure without a separate approved issue.
- Do not implement a production deployment executor.
- Do not automatically start a live bot.
- Do not provide live bot start / stop / deploy controls.
- Do not present fixture, fallback, or unknown-source data as real local
  database success.

## PR Scope Rules

Phase 8 must avoid large mixed PRs.

- One PR should close one approved issue.
- API, database, frontend, runtime, safety, and documentation changes should be
  split unless the issue explicitly defines a narrow combined acceptance path.
- High-risk dry-run execution work must not be bundled with normal API, UI, or
  seed work.
- Review issue `#247` must not be started until all required child issues are
  Done.

## Completion Definition

Phase 8 is complete only when `#247` confirms:

- the Local Strategy Lab core flow is page-operable;
- core success data is database-backed;
- page/API/database reconciliation is proven;
- refresh persistence is proven;
- fallback/fixture/unknown-source states are visible and do not pass as core
  success;
- local dry-run readiness is real or explicitly `BLOCKED`;
- controlled dry-run, if accepted, remains local-only and dry-run-only;
- no live trading, real orders, production deployment, secret persistence, or
  Freqtrade source modification has been introduced.
