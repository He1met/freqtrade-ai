# Phase 6 Live Candidate Governance Plan

## Status

Phase 6 is in planning.

Phase 6 means live-candidate and deployment governance. It does not authorize
live trading execution, automatic order placement, automatic live bot startup,
or production deployment.

Phase 5 Dry-run / FreqUI runtime management has passed final acceptance. PR
#175 was merged, #163 was closed, and `docs/phase5_acceptance.md` records the
accepted Phase 5 scope. PR #127 has been closed without merge after the split
decision in `docs/phase5_pr127_split_decision.md`.

## Phase Definition

Phase 6 covers the governance layer required before any future live or
production work can be considered:

- live candidate qualification;
- risk checklist and fail-closed preflight;
- human approval records;
- deployment records;
- rollback plans;
- read-only runtime and alert summaries;
- read-only frontend visibility;
- offline governance smoke testing;
- final Phase 6 acceptance.

Phase 6 is not:

- direct live trading;
- automatic order placement;
- production deployment execution;
- a live bot control plane;
- an expansion of Phase 5 dry-run management into live trading.

## Safety Boundaries

All Phase 6 work must keep these boundaries:

- Do not place real orders.
- Do not start live trading.
- Do not commit real API key, secret, or passphrase values.
- Do not write secrets into code, config, databases, logs, reports, issues, PRs,
  or documentation.
- Do not modify Freqtrade source code.
- Do not introduce Redis, Celery, Kafka, or RabbitMQ unless a later Phase 7
  scope explicitly allows it.
- Do not perform production deployment.
- Do not bypass human approval.
- Do not implement automatic live bot startup.
- Do not merge or cherry-pick code from PR #127.

## PR #127 Closeout

PR #127 was a draft, conflicting, monolithic PR. It mixed adapter compatibility,
runtime API work, a seed script, frontend fallback changes, Simplified Chinese
UI copy, and Vite proxy changes.

The Phase 5 split decision concluded that PR #127 should not be merged directly
and should not be cherry-picked as a bundle. Useful ideas must return through
new, narrowly scoped Issues with their own acceptance criteria and PRs.

## Project Queue

| Issue | Project status | Purpose |
| --- | --- | --- |
| [#176](https://github.com/He1met/freqtrade-ai/issues/176) | Backlog | Phase 6 Epic for live-candidate and deployment governance |
| [#177](https://github.com/He1met/freqtrade-ai/issues/177) | Ready | Design plan for Phase 6 governance |
| [#178](https://github.com/He1met/freqtrade-ai/issues/178) | Backlog | `LiveCandidateProfile` schema and entry-condition lock |
| [#179](https://github.com/He1met/freqtrade-ai/issues/179) | Backlog | Live-candidate risk checklist and fail-closed preflight |
| [#180](https://github.com/He1met/freqtrade-ai/issues/180) | Backlog | Human approval record and state machine |
| [#181](https://github.com/He1met/freqtrade-ai/issues/181) | Backlog | `DeploymentRecord` schema and rollback plan |
| [#182](https://github.com/He1met/freqtrade-ai/issues/182) | Backlog | Read-only runtime monitoring and alert summary DTOs |
| [#183](https://github.com/He1met/freqtrade-ai/issues/183) | Backlog | Read-only frontend page for approvals and deployment records |
| [#184](https://github.com/He1met/freqtrade-ai/issues/184) | Backlog | Phase 6 offline governance smoke |
| [#185](https://github.com/He1met/freqtrade-ai/issues/185) | Backlog | Phase 6 acceptance review |

Only #177 is Ready at planning start. All other Phase 6 Issues remain Backlog
until the design Issue is completed and reviewed.

## Intended Dependency Order

1. Complete the design plan in #177.
2. Define `LiveCandidateProfile` and entry-condition locking in #178.
3. Add risk checklist and fail-closed preflight in #179.
4. Add human approval records and state machine in #180.
5. Add `DeploymentRecord` and rollback plan in #181.
6. Add read-only runtime monitoring and alert summary DTOs in #182.
7. Add the read-only frontend page in #183.
8. Add Phase 6 offline governance smoke in #184.
9. Run the final Phase 6 acceptance review in #185.

## Acceptance Path

Phase 6 can be considered complete only when:

- all child Issues are completed through focused PRs;
- the offline governance smoke passes;
- backend tests pass for backend-affecting work;
- frontend build passes for frontend-affecting work;
- `git diff --check` passes;
- docs clearly state that Phase 6 governance does not authorize live trading
  execution, automatic order placement, or production deployment;
- Project #3 marks the Phase 6 child Issues and Epic as Done after review.
