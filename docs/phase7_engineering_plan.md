# Phase 7 Engineering Plan

## Current Status

Phase 6 live-candidate and deployment governance has passed final acceptance.
PR `#194` has been merged, Review issue `#185` is closed, and
`docs/phase6_acceptance.md` records the accepted state.

Phase 6 accepted capabilities include:

- `LiveCandidateProfile` and locked live-candidate inputs.
- Fail-closed risk preflight.
- Human approval record and state machine.
- `DeploymentRecord` and rollback plan.
- Read-only runtime monitoring DTOs.
- Read-only frontend page for approvals and deployment records.
- Phase 6 offline governance smoke.

The Phase 6 acceptance does not authorize live trading, real order placement,
exchange connectivity, production deployment, deployment executor work, live bot
start / stop / deploy controls, real K-line downloads, or Phase 7
implementation.

## Phase 1-6 Cleanup

Before opening Phase 7 planning, the live GitHub state was checked:

- Open PRs: none.
- PR `#127`: closed, not merged. It must not be reopened, cherry-picked, or
  merged as part of Phase 7 planning.
- Phase 6 Epic `#176`: already closed and Project status `Done`.
- Phase 6 Review `#185`: closed by PR `#194` and Project status `Done`.

The following legacy Epic / aggregation issues were still open even though
their phase acceptance documents already covered the accepted scope:

- Phase 1 legacy Epics `#15` through `#28`.
- Phase 3 Epic `#102`.

These legacy issues were commented, closed, and marked `Done` in GitHub
Project #3. The cleanup comments reference:

- `docs/phase1_acceptance.md`
- `docs/phase3_acceptance.md`

No uncertain or genuinely unfinished open issue was closed during this cleanup.

## Phase 7 Definition

Phase 7 means engineering upgrade and scalable operation readiness.

Phase 7 includes:

- CI/CD and repeatable validation.
- Audit logs and governance events.
- Read-only monitoring contracts.
- Operator dashboard.
- Runtime read-only API contract.
- Secret scanning and configuration safety checks.
- Worker / queue architecture design.

Phase 7 does not mean:

- Direct production deployment.
- Automatic live trading.
- Real order execution.
- Live bot start / stop / deploy controls.
- Exchange connectivity.
- Real K-line downloads.
- Bypassing human approval.

## First-Stage Priorities

Phase 7 should begin with engineering foundations that reduce ambiguity and
make later runtime work auditable:

1. Runtime read-only API contract.
2. Operator local readiness / status.
3. Audit log / governance event.
4. GitHub Actions CI.
5. Secret scanning.
6. Worker / queue design only, without Redis / Celery implementation.

## Phase 7 Issue Plan

| Issue | Title | Initial Project status |
| --- | --- | --- |
| `#195` | `[EPIC][Phase 7] 工程化升级与规模化运行` | Backlog |
| `#196` | `[Docs][Phase 7] Phase 1-6 收口清理与 Phase 7 工程化规划` | Ready / In Progress |
| `#197` | `[Backend][Phase 7] Runtime Read-only API Contract` | Backlog |
| `#198` | `[Backend][Phase 7] Operator Status API 与本地诊断入口` | Backlog |
| `#199` | `[Backend][Phase 7] Audit Log schema 与 governance event 归档` | Backlog |
| `#200` | `[DevOps][Phase 7] GitHub Actions CI：backend pytest / frontend build / smoke` | Backlog |
| `#201` | `[Security][Phase 7] Secret scanning 与配置安全检查增强` | Backlog |
| `#202` | `[Design][Phase 7] Worker / Queue 架构设计方案` | Backlog |
| `#203` | `[Frontend][Phase 7] Operator Dashboard：系统状态、fallback 状态、smoke 状态、artifact 链接` | Backlog |
| `#204` | `[Test][Phase 7] Phase 7 engineering smoke` | Backlog |
| `#205` | `[Review][Phase 7] Phase 7 工程化验收` | Backlog |

Only `#196` should be Ready for the initial planning cleanup PR. All other
Phase 7 issues should remain Backlog until their predecessors are complete.

## Execution Order

Phase 7 should proceed in this order:

1. Close the planning cleanup issue `#196`.
2. Implement the runtime read-only API contract in `#197`.
3. Add operator status and local diagnostics in `#198`.
4. Add audit log and governance event archival in `#199`.
5. Add GitHub Actions CI in `#200`.
6. Add secret scanning and configuration safety checks in `#201`.
7. Produce worker / queue design only in `#202`.
8. Add the operator dashboard in `#203`.
9. Add Phase 7 engineering smoke in `#204`.
10. Complete Phase 7 review and Epic closeout in `#205` and `#195`.

## Worker / Queue Boundary

Worker / queue work is design-only at the start of Phase 7.

Allowed:

- Architecture tradeoff document.
- Task state model.
- Retry and idempotency design.
- Audit and artifact relationship design.
- Decision criteria for a future implementation.

Not allowed in planning:

- Redis implementation.
- Celery implementation.
- Kafka implementation.
- RabbitMQ implementation.
- Worker service startup.
- Production queue deployment.

## Acceptance Approach

Phase 7 should keep the repository fail-closed and evidence-driven:

- Backend work runs `cd backend && . .venv/bin/activate && pytest`.
- Python code changes run `python3 -m compileall backend/app backend/tests scripts`.
- Frontend work runs `cd frontend && npm run build`.
- All changes run `git diff --check`.
- Once `#204` lands, review uses
  `python3 scripts/smoke_phase7.py --offline --tmp-dir /tmp/freqtrade-ai-phase7-smoke`.

Planning-only PRs may skip backend pytest and frontend build if they only modify
docs and GitHub metadata, but the PR must state that reason.

## Safety Boundary

Every Phase 7 issue must preserve these boundaries:

- Do not execute real orders.
- Do not start live trading.
- Do not connect to a real exchange.
- Do not download real K-line data.
- Do not commit real API keys, secrets, tokens, or passphrases.
- Do not write secrets to code, configuration, databases, logs, documents, or
  test fixtures.
- Do not modify Freqtrade source code.
- Do not implement a production deployment executor.
- Do not automatically start a live bot.
- Do not bypass human approval.
- Do not introduce Redis, Celery, Kafka, or RabbitMQ implementation in planning;
  design discussion only is allowed.

