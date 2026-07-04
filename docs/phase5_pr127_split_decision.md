# Phase 5 PR #127 Split Decision

## Status

Issue #161 audits PR #127 for Phase 5 carry-forward only. PR #127 remains a
draft, conflicting, monolithic PR and must not be merged directly.

PR #127 mixes these unrelated directions:

- Phase 3 local backtest adapter compatibility changes.
- A new backend MVP runtime API.
- A local runtime seed script.
- Frontend fallback and API-loading behavior changes.
- Simplified Chinese UI copy changes.
- Vite development proxy configuration.

This decision does not implement any runtime API, change fallback behavior,
start dry-run, connect to exchanges, download K lines, place orders, store
secrets, or modify Freqtrade source code.

## Phase 5 Decision Summary

| PR #127 area | Decision | Follow-up |
| --- | --- | --- |
| Backend MVP runtime API | Do not carry into Phase 5 as-is | New design Issue required before any implementation |
| Runtime seed script | Do not carry into Phase 5 as-is | New demo-fixture Issue required if still needed |
| Frontend fallback replacement | Do not carry into Phase 5 as-is | Keep controlled fallback with visible `source=fallback` |
| API 404 / page-switch retry reduction | Keep as a possible focused frontend reliability task | New Issue required, no runtime API bundled |
| Simplified Chinese UI copy | Keep as possible standalone UI copy task | New Issue required, no API or runtime changes bundled |
| Vite proxy config | Do not carry into Phase 5 as-is | New local-dev Issue required if a backend API contract exists |
| Backtest adapter compatibility | Not Phase 5 scope | Separate maintenance Issue outside #161 if real local Freqtrade requires it |

## Keep

The following ideas are worth preserving, but only as separate, small Issues
with their own acceptance criteria:

- A focused frontend reliability task can reduce repeated API 404 attempts if
  it keeps fallback state explicit and does not introduce a hidden runtime API.
- A focused UI copy task can localize page labels and display helpers without
  touching API contracts, fallback semantics, seed scripts, or backend adapter
  behavior.
- A separate local-dev task can add Vite proxy defaults only after the backend
  endpoint contract is stable.
- Backtest adapter compatibility observations from PR #127 may be useful for
  Phase 3 maintenance, but they are not part of Phase 5 dry-run / FreqUI
  management.

## Drop From Phase 5

The following PR #127 pieces should not be carried into Phase 5:

- `backend/app/api/mvp_runtime.py` as a broad runtime data endpoint.
- `scripts/seed_runtime_mvp.py` as a local runtime seed command.
- Deleting controlled frontend mock data in favor of empty fallback data.
- Bundling frontend localization with runtime API or fallback behavior changes.
- Vite proxy changes bundled with runtime data API work.
- Any behavior that presents fixture, fallback, or seeded data as live runtime
  state.

Phase 5 already has dedicated contracts for dry-run runtime management:

- #157 records dry-run artifact manifests and fail-closed states.
- #158 defines read-only dry-run status snapshots and events.
- #159 defines FreqUI link metadata and disabled / blocked boundaries.
- #160 displays dry-run / FreqUI management state in the frontend.

Adding a parallel MVP runtime API from PR #127 would duplicate and blur those
contracts.

## Fallback Contract

The current frontend fallback contract should remain:

- Fallback data is controlled fixture data.
- Pages must expose that fallback data is being used.
- Missing backend APIs must not be presented as live runtime data.
- Empty fallback data is not automatically safer if it hides whether data is
  missing, blocked, or intentionally fixture-backed.

Any future fallback change needs a dedicated Issue that defines:

- which endpoints are required;
- whether route-level request caching is needed;
- how `api` versus `fallback` source is shown;
- what users see for `BLOCKED`, `FAILED`, `SKIPPED`, and unavailable states;
- how the change avoids implying live trading or real dry-run execution.

## Runtime API Boundary

Do not implement PR #127's MVP runtime API under #161. If a backend runtime API
is still desired after Phase 5 acceptance, it needs a new design Issue that
answers:

- whether it is a read-only aggregation layer or a control surface;
- which Phase 5 DTOs it returns;
- how it distinguishes fixture, artifact, local JSON, and unavailable sources;
- where it reads data from without relying on real user runtime state;
- how it redacts secret-shaped values;
- why it is not duplicating FreqUI or Freqtrade REST API;
- what offline tests and smoke checks prove.

Until that design exists, #157-#160 remain the authoritative Phase 5 runtime
management contracts.

## Seed Script Boundary

Do not carry `scripts/seed_runtime_mvp.py` into Phase 5 as-is. A seed script may
be reconsidered only as a demo-fixture task if it:

- writes only under a caller-provided tmp directory;
- never reads real exchange credentials or user runtime state;
- produces explicit fixture metadata;
- cannot be confused with real dry-run evidence;
- is validated by offline tests;
- does not replace #162 smoke coverage.

## Final #161 Outcome

PR #127 should be closed or left unmerged after this audit. Useful ideas must
return through new, narrowly scoped Issues. No code from PR #127 should be
cherry-picked into Phase 5 without one of those Issues and its own PR.
