# Phase 2 Architecture and Governance Review

## Verdict

The current architecture is reasonable for the Phase 2 goal: improving strategy
research quality before any dry-run or live-trading work. The backend has clear
boundaries for provider integration, strategy rendering, Freqtrade CLI access,
result parsing, persistence, failure reasons, lineage, static review, and
scoring. The frontend is still lightweight, but its API boundary and controlled
fallback path are adequate for the remaining Phase 2 display work.

Phase 2 can be completed with the existing issue plan if the remaining work
keeps the current sequence:

1. `#84` enhance scoring and elimination rules.
2. `#85` display scoring breakdown in Ranking.
3. `#86` add the Phase 2 offline smoke script.
4. `#87` run the Phase 2 acceptance review.

## Architecture Assessment

### Backend

The backend layering is directionally correct:

- `adapters/freqtrade/` owns all Freqtrade CLI, config, data-index, result, and
  strategy-file boundaries.
- `schemas/` owns validation and DTO contracts.
- `repositories/` owns persistence behavior.
- `services/` owns orchestration and domain workflows.
- `spikes/` keeps exploratory local Freqtrade proof work out of production
  service paths.

This is the right shape for Phase 2 because it prevents provider logic,
Freqtrade command construction, and persistence rules from being mixed into one
large service.

The main architectural risk is scoring. `StrategyScoringService` still uses the
Phase 1 MVP formula and `metrics_snapshot` as a flexible payload. That was
acceptable for Phase 1, but `#84` should introduce a versioned Phase 2 scoring
contract instead of mutating the old formula in place.

Recommended `#84` direction:

- Create a new scoring version, for example `phase2-quality-v1`.
- Keep component scores explicit and documented.
- Add elimination status and reasons as structured fields in the snapshot or a
  dedicated schema before exposing them to the frontend.
- Include validation/static-review/failure-reason signals without requiring real
  Freqtrade execution.
- Preserve old ranking behavior for existing `phase1-mvp-v1` scores.

### Freqtrade Boundary

The adapter boundary is appropriate. `FreqtradeCliRunner` uses an allowlist of
commands and options, `FreqtradeConfigBuilder` rejects credential-shaped keys,
and the real backtest spike does not download candles or connect to exchanges.

The current blocker remains environmental: `user_data/data` has no local market
data beyond `.gitkeep`, so the real Freqtrade spike correctly reports
`BLOCKED`. This is not an architecture failure.

### LLM Provider Boundary

The provider boundary is reasonable for Phase 2:

- fake provider remains the default path for tests and smoke commands.
- real provider reads credentials from ENV only.
- response content is validated through `StrategyBlueprint`.
- tests can use mock HTTP clients without real network calls.

This should stay as a boundary. Do not spread vendor-specific payload parsing
into generation services or API handlers.

### Frontend

The frontend structure is still simple but acceptable:

- `api/client.ts` normalizes backend/mock data into page-friendly shapes.
- `data/mock.ts` provides controlled fallback data.
- pages consume `useMvpData()` instead of calling fetch directly.

For `#85`, avoid adding scoring business rules to the Ranking page. The page
should display a backend-provided breakdown and elimination reasons. It can use
fallback fixtures, but should not infer elimination status from total score.

## Issue Management Assessment

The current Phase 2 issue management is mostly sound:

- Completed issues `#76` through `#83` each map to one merged PR.
- Remaining issues are ordered naturally by dependency.
- `#87` is correctly a Review issue and should not move to Ready before `#84`
  through `#86` finish.
- `#75` is an Epic with `XL` size and should not be directly developed.

Recommended adjustments:

1. Promote `#84` to Ready next.
2. Keep `#85` in Backlog until `#84` defines the response shape.
3. Keep `#86` in Backlog until `#84` and `#85` are merged, then make it Ready.
4. Keep `#87` in Backlog until the Phase 2 smoke command exists and passes.
5. Consider closing or explicitly superseding old Phase 1 Epic issues that are
   still open, because they add noise to open issue triage.

## Can Phase 2 Complete?

Yes, Phase 2 can complete without changing the current architecture. The
remaining work is incremental:

- scoring rules and structured reasons,
- frontend ranking explanation,
- smoke coverage,
- final acceptance review.

The main completion risk is not code structure. The main risk is scope creep:
starting Hyperopt, dry-run, live trading, real exchange connections, K-line
download, or production job queues before the Phase 2 research-quality loop is
accepted.

## Optimization Backlog

These optimizations are useful but should not block Phase 2:

- Add GitHub Actions for backend pytest, frontend build, compileall, and
  `git diff --check`.
- Add a short architecture decision record for the provider boundary and
  scoring-version policy.
- Add a fixture dataset specifically for smoke tests while keeping real market
  data out of the repository.
- Add API route documentation for failure reasons, lineage, and ranking once
  `#84` stabilizes the score breakdown contract.
- Add frontend tests after the Ranking breakdown UI exists.
