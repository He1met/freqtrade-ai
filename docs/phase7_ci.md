# Phase 7 CI

## Scope

The GitHub Actions workflow in `.github/workflows/ci.yml` provides the Phase 7
repeatable validation baseline. It runs on pull requests targeting `main` and
on pushes to `main`.

The workflow runs:

- `cd backend && pytest`
- `python -m compileall backend/app backend/tests scripts`
- `cd frontend && npm ci && npm run build`
- `git diff --check origin/<base>...HEAD`
- `python scripts/smoke_phase6.py --offline --tmp-dir "$RUNNER_TEMP/freqtrade-ai-phase6-smoke"`

The smoke command is intentionally limited to the existing offline fixture path
until the Phase 7 engineering smoke is added by issue `#204`.

## Safety Boundary

CI must not require API keys, exchange secrets, passphrases, production
database URLs, or live operator credentials. The workflow does not configure
secret environment variables.

The CI smoke path must remain offline and fixture based:

- No live trading.
- No real order placement.
- No real exchange connection.
- No real K-line download.
- No real Freqtrade live or dry-run startup.
- No production deployment execution.
- No secret value logging.
- No Freqtrade source modification.

## Failure Handling

Dependency installation, backend tests, frontend build, whitespace checks, and
offline smoke failures are blocking CI failures. These failures should be
reported directly in the pull request instead of being bypassed or hidden.
