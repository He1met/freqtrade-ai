# Phase 5 Dry-run / FreqUI Plan

## Status

Phase 5 planning is initialized. Phase 4 Hyperopt parameter optimization has
passed final acceptance, and post-Phase-4 cleanup has closed stale planning
state.

Phase 5 is the first stage that approaches runtime operation. This plan does
not authorize live trading, real order placement, production deployment, or
unrestricted exchange connectivity. Any dry-run work must stay behind explicit
Issue scope, ENV-only secret handling, fail-closed preflight checks, and audit
records.

## Phase Definition

Phase 5 = Dry-run / FreqUI runtime management.

The goal is to make local dry-run readiness, controlled dry-run configuration,
read-only status display, and FreqUI entry points manageable from this project
without turning the project into a trading engine.

Freqtrade remains responsible for exchange connectivity, dry-run execution,
FreqUI, REST API, Telegram, and trading runtime behavior. This project owns only
the management layer around profiles, preflight checks, safe config generation,
artifacts, status summaries, frontend presentation, and acceptance evidence.

## Safety Gates

Phase 5 work must satisfy these gates before any PR can merge:

- Live trading stays disabled.
- Real order placement stays disabled.
- API keys, secrets, tokens, and passphrases are ENV-only and must never be
  written to code, YAML, JSON fixtures, database rows, reports, logs, Issues, or
  PR descriptions.
- Missing `freqtrade`, local user data, required ENV variables, or local
  dependencies must produce `BLOCKED`, not fabricated success.
- Any command construction must reject live-mode or real-order flags.
- FreqUI must be reused through links and metadata; this project must not
  reimplement FreqUI.
- No Redis, Celery, Kafka, RabbitMQ, worker pool, deployment, or production
  operation is introduced in Phase 5.
- Phase 6 live-candidate or deployment work remains out of scope.

## Issue Sequence

| Order | Issue | Purpose | Initial State |
| --- | --- | --- | --- |
| 1 | #152 | Dry-run / FreqUI safety boundary and execution plan | In Progress |
| 2 | #153 | Freqtrade dry-run local prerequisites and risk preflight | Backlog |
| 3 | #154 | DryRunProfile schema and runtime variable lock | Backlog |
| 4 | #155 | Controlled Freqtrade dry-run CLI command construction | Backlog |
| 5 | #156 | Dry-run config generation and ENV-only secret preflight | Backlog |
| 6 | #157 | Dry-run artifact manifest and status archive | Backlog |
| 7 | #158 | Dry-run read-only status snapshots and event parsing | Backlog |
| 8 | #159 | FreqUI entry configuration and read-only link boundary | Backlog |
| 9 | #160 | Dry-run / FreqUI runtime management page | Backlog |
| 10 | #161 | PR #127 runtime API and fallback contract split decision | Backlog |
| 11 | #162 | Phase 5 offline smoke acceptance script | Backlog |
| 12 | #163 | Phase 5 final acceptance review | Backlog |

Epic #151 remains an XL aggregation issue and is not a direct development
target. After #152 is merged, #153 should become the next Ready item.

## Dependency Rules

- #153 must complete before implementation issues rely on real local
  prerequisites.
- #154 defines the schema contract that #155, #156, #157, and #158 consume.
- #155 must use fake executor tests by default and must not start real dry-run.
- #156 owns ENV-only secret preflight and redaction rules.
- #157 owns the audit manifest shape for dry-run management actions.
- #158 owns read-only status DTOs and event parsing, not trading control.
- #159 owns FreqUI link metadata and disabled / blocked states.
- #160 depends on backend DTO and FreqUI metadata shapes being stable.
- #161 decides whether PR #127 ideas should become new small PRs; PR #127
  itself must not be merged directly.
- #162 forms the offline acceptance path after core pieces exist.
- #163 can start only after #152-#162 are complete or explicitly documented as
  superseded / blocked.

## PR #127 Carry-Forward Policy

PR #127 remains a draft and conflicting monolithic PR. Phase 5 may reuse ideas
from it only through separate Issues or small PRs:

- Freqtrade adapter compatibility checks can be handled after #153 or #155.
- Runtime MVP API and seed data need a separate design decision before
  implementation.
- Frontend fallback behavior must not be changed without a clear product
  contract.
- Chinese UI localization should be a standalone UI copy PR if still desired.

The cleanup decision remains: do not merge PR #127 directly.

The Phase 5 split decision for #161 is recorded in
[phase5_pr127_split_decision.md](phase5_pr127_split_decision.md). It keeps
#157-#160 as the authoritative dry-run / FreqUI contracts and rejects carrying
PR #127's runtime API, seed script, fallback replacement, localization, proxy,
or adapter changes into Phase 5 as a bundled PR.

## Read-only Status Snapshot Contract

#158 adds the backend-only read model for dry-run runtime status. It does not
start dry-run, connect to an exchange, call FreqUI, or add trading controls.

Stable DTOs:

- `DryRunStatusSnapshot` records status, profile, strategy, exchange, pair,
  timeframe, `dry_run`, balance summary, open trades summary, recent events,
  fail-closed reasons, `last_updated`, and `artifact_manifest_path`.
- `DryRunEvent` records timestamp, event type, severity, message, source, and
  redacted details.

Supported read-only sources:

- controlled fixture JSON used by tests and later offline smoke coverage;
- dry-run artifact manifests from #157, using the latest `status_snapshots`
  entry when present;
- controlled local JSON exported by local tooling or a future read-only adapter.

Fail-closed behavior:

- missing files return a `BLOCKED` snapshot;
- malformed JSON returns a `FAILED` snapshot;
- artifact manifests without `status_snapshots` return an explicit empty
  `SKIPPED` snapshot unless the manifest itself is already `BLOCKED` or
  `FAILED`;
- `SUCCESS` or `RUNNING` status requires explicit `dry_run=true`;
- `dry_run=false` always returns `FAILED`;
- secret-shaped keys and log text are redacted before they enter DTO output.

## Validation Baseline

Docs-only work:

```bash
git diff --check
```

Backend work:

```bash
cd backend && . .venv/bin/activate && pytest
python3 -m compileall backend/app backend/tests scripts
git diff --check
```

Frontend work:

```bash
cd frontend && npm run build
git diff --check
```

Phase 5 smoke target after #162:

```bash
python3 scripts/smoke_phase5.py --offline --tmp-dir /tmp/freqtrade-ai-phase5-smoke
```

The Phase 5 smoke must be offline by default. It must not start real dry-run,
connect to an exchange, download K lines, or place orders.

## Phase 6 Readiness

Phase 5 completion may allow Phase 6 planning only after final review. It must
not silently widen into live trading or deployment.

Phase 6 planning must separately define human approval, risk checks, deployment
records, rollback, monitoring, and live-candidate governance before any live
trading work is attempted.
