# Post Phase 4 Cleanup

## Status

Phase 4 Hyperopt parameter optimization has passed final acceptance. PR #149
merged the final review and closed #138.

Phase 4 completion evidence from PR #149:

- `python3 scripts/smoke_phase4.py --offline --tmp-dir /tmp/freqtrade-ai-phase4-smoke`: PASS.
- `cd backend && . .venv/bin/activate && pytest`: PASS, `183 passed`.
- `python3 -m compileall backend/app backend/tests scripts`: PASS.
- `cd frontend && npm run build`: PASS.
- `git diff --check`: PASS.

Phase 4 remains a local research and parameter-optimization phase. It does not
enable dry-run, live trading, exchange connectivity, K-line downloads, real
orders, production deployment, or queue infrastructure.

## Epic #128

Epic #128 was reviewed after PR #149. All Phase 4 child issues are complete:

| Issue | PR | Result |
| --- | --- | --- |
| #129 | #140 | Done |
| #130 | #141 | Done |
| #131 | #142 | Done |
| #132 | #143 | Done |
| #133 | #144 | Done |
| #134 | #145 | Done |
| #135 | #146 | Done |
| #136 | #147 | Done |
| #137 | #148 | Done |
| #138 | #149 | Done |

#128 was closed after the completion summary was posted, and its Project status
was set to Done.

## PR #139

PR #139 was an early Phase 4 planning PR. Its main content is now superseded:

- Phase 4 Epic and child issues already exist and are complete.
- The initial planning content is superseded by
  [phase4_hyperopt_design.md](phase4_hyperopt_design.md) and
  [phase4_acceptance.md](phase4_acceptance.md).
- PR #149 recorded final acceptance results for #129-#138 and PRs #140-#148.

The useful remaining part of #139 was the roadmap correction that defines:

- Phase 4 as Hyperopt parameter optimization.
- Phase 5 as Dry-run / FreqUI runtime management.
- Phase 6 as live-candidate and deployment management.

That correction is absorbed into this cleanup record through
[roadmap.md](roadmap.md). PR #139 was closed as stale and should not be merged
directly.

## PR #127

PR #127 remains a large draft PR and must not be merged directly. It is
currently conflicting and mixes unrelated runtime, backend adapter, frontend,
localization, and smoke-data directions.

### Scope Observed

PR #127 touches 27 files with backend adapter changes, a runtime MVP API, a
runtime seed script, frontend API loading changes, mock-data removal, Chinese UI
copy changes, Vite proxy config, and page display changes.

### Keep Or Reopen As Small PRs

- Freqtrade CLI adapter compatibility for newer Freqtrade behavior:
  investigate separately whether `backtesting` should support
  `--backtest-directory`, whether `list-data` should use `--trading-mode`, and
  whether `--timeframes` is still correct for the supported Freqtrade version.
- Backtest config pricing defaults:
  if real local Freqtrade 2026.5 requires explicit `entry_pricing` /
  `exit_pricing`, add them in a focused backend config PR with tests and without
  changing unrelated frontend behavior.
- Exported zip result handling:
  Phase 3/4 result parsers handle JSON payloads, but PR #127 adds archive
  materialization from Freqtrade exported backtest zips. Keep this as a focused
  adapter compatibility PR if real local Freqtrade output requires it.
- Frontend Chinese localization:
  potentially useful, but it should be a dedicated UI copy PR. It should not be
  bundled with runtime API, adapter changes, or seed scripts.
- Runtime MVP API and seed data:
  may be useful for local demos, but it needs its own design issue because it
  introduces a new runtime data contract and local seed command.

### Discard From PR #127 As-Is

- The monolithic combination of adapter changes, runtime API, seed script,
  localization, fallback changes, and Vite proxy config.
- Any change that would make fallback data appear as live runtime data.
- Any path that runs real Freqtrade, touches real exchange data, or depends on
  user runtime state without a clear fail-closed contract.
- Direct replacement of the current controlled fallback contract without a
  product decision.

### Current Fallback / API 404 Position

The current frontend keeps controlled fallback data and reports the source as
`fallback` when API data is unavailable. PR #127 tries to move toward local
runtime API data and empty fallback data. That is a separate product and API
contract decision, not a Phase 4 cleanup change.

If API 404 noise still matters, handle it through a focused frontend/API issue
that defines the desired behavior before changing fallback semantics.

## Phase 5 Readiness

The project can enter Phase 5 planning only. Phase 5 planning should create a
separate Epic and execution issues before any implementation.

Phase 5 planning must explicitly decide:

- Whether Dry-run / FreqUI runtime management is in scope.
- How to gate any dry-run capability behind manual review and safety controls.
- Whether any PR #127 runtime API ideas should be carried forward as small,
  separate issues.
- How to keep real exchange connection, K-line download, live trading, and real
  order placement disabled until explicitly approved.

## Boundaries Still In Force

- No dry-run implementation.
- No live trading implementation.
- No real exchange connection.
- No real K-line download.
- No real order placement.
- No real API key, secret, or passphrase.
- No Freqtrade source-code modification.
- No Redis, Celery, Kafka, or RabbitMQ.
- No Phase 5 implementation in this cleanup PR.
