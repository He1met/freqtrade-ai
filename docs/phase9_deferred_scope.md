# Phase 9 Deferred Scope And Phase 10 Boundary

Phase 9 is an Operational Readiness phase. It proves that local real-run
evidence is trustworthy: real provider integration, database-backed pages,
strategy files, local backtests, persisted results, scoring, and QA
reconciliation.

This document lists work that must remain deferred until a later Phase 10 or a
separate productionization review explicitly approves it.

## Scope Guard

Deferred work must not be hidden inside Phase 9 Feature, Task, Test, Docs, or
Security issues. If a Phase 9 PR needs one of these items to pass, the correct
result is usually `BLOCKED`, not an opportunistic implementation.

## Deferred Items

| Deferred item | Why it is deferred | Required evidence before a future issue |
| --- | --- | --- |
| Live trading | Phase 9 validates local product readiness, not real-money trading. | Phase 9 review accepted, dry-run evidence reviewed, explicit live-risk approval. |
| Real orders | Any order path can create financial loss and must not be part of readiness validation. | Dedicated risk model, exchange safety review, manual approval workflow, kill-switch design. |
| Real funds or account trading | Phase 9 does not validate profitability or account safety. | Separate production trading policy, account permissions review, capital limits, audit trail. |
| Production deployment | Phase 9 is local/dev/test evidence only. | Deployment architecture, rollback, monitoring, secrets, backups, and incident process. |
| Automatic live deployment | The current phase forbids unattended promotion into live systems. | Human approval gates, deploy freeze policy, rollback tests, audit evidence. |
| Live bot start/stop/deploy controls | These controls are outside the real-run local validation contract. | Explicit live-control issue, UI/permission model, fail-safe controls, security sign-off. |
| Connecting to an exchange for real trading | Phase 9 may use local data and local dry-run design, not exchange trading. | Exchange credential governance, network boundary, rate-limit and permission review. |
| Switching dry-run into live mode | Phase 9 must preserve dry-run/live separation. | Separate live-mode change request, safety review, explicit config migration plan. |
| Production queue infrastructure | Redis, Celery, Kafka, RabbitMQ, worker pools, or similar systems are not needed to prove the local chain. | Queue design, failure semantics, replay/idempotency plan, ops ownership. |
| Complex multi-model platform | DeepSeek can validate the first real provider; broad provider orchestration is not required. | Provider abstraction review, cost/rate-limit policy, evaluation matrix, test strategy. |
| Production monitoring and alerting | Phase 9 can define evidence and local status, but not operate production. | SLOs, alert routes, dashboards, on-call expectations, incident response. |
| Complex permissions/RBAC | Readiness can be verified without a full permission platform. | Role model, threat model, admin workflows, audit requirements. |
| Profit validation or strategy approval | Phase 9 proves data flow, not market viability. | Evaluation policy, benchmark data, review board, risk constraints. |
| Freqtrade source-code modification | The project must reuse Freqtrade through adapters. | Upstream contribution plan or explicit fork-governance approval. |
| Production database mutation | Phase 9 must use local/dev/test database targets. | Backup/restore plan, migration policy, data retention, access controls. |

## What Phase 9 May Do Instead

Phase 9 may safely perform or design:

- real LLM provider preflight and one controlled minimal real API call when an
  approved issue requires it;
- local database writes in explicit local/dev/test targets;
- local strategy file writes under approved directories;
- local backtest preflight and execution only when dependencies are present;
- local dry-run readiness and controlled dry-run design;
- fixture/fallback/mock/unknown visibility for QA;
- structured Bug issue creation for mismatched evidence.

## Required `BLOCKED` Behavior

If a deferred item appears necessary during Phase 9, the issue should record:

- what deferred capability was requested;
- why Phase 9 cannot implement it;
- which local evidence can still be collected;
- what future approval or design document is required;
- whether a new Phase 10 or productionization issue should be created.

Examples:

- Missing production queue: keep the issue local-only and document the queue
  design gap, rather than adding Redis or Celery.
- Need live exchange connection: return `BLOCKED` and document that Phase 9 only
  permits local data and non-live validation.
- Desire for hourly generation: write the local controlled design, but do not
  ship production scheduling or live deployment.

## Bug Triggers

Create a Bug or Security issue if any Phase 9 work:

- starts or enables live trading;
- places, simulates as real, or prepares real orders;
- stores or prints real keys, tokens, cookies, secrets, or passphrases;
- writes to a production, shared, remote, or unknown database;
- introduces Redis, Celery, Kafka, RabbitMQ, worker pools, or production queues
  without a separate approved issue;
- modifies Freqtrade source code;
- presents fixture, fallback, mock, or unknown data as real database-backed
  success;
- claims production readiness or profitability from Phase 9 evidence.

## Future Issue Entry Criteria

A deferred item may become a future issue only when it has:

- a named phase or milestone outside Phase 9;
- explicit user approval or review issue approval;
- a safety boundary section;
- rollback or fail-closed behavior;
- QA acceptance criteria;
- evidence that the local Phase 9 chain is already working or explicitly
  blocked for reasons unrelated to the deferred work.

Until then, the item remains outside Phase 9 scope.
