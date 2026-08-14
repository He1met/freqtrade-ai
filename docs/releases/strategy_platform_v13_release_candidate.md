# Strategy Platform V1.3 canonical-only release candidate

Status: `LOCAL_RELEASE_CANDIDATE_PREPARED_NOT_STAGED`

Prepared on 2026-08-14 from GitHub `main` commit
`2606baa8f33bafc5493c24dc547c95a8b153fc8c` on local branch
`codex/v13-canonical-platform`.

This document does not authorize staging, committing, pushing, opening a pull
request, installing a database, starting a service, running research, or trading.

## 1. Remote collision and baseline evidence

- GitHub `main`, local `HEAD`, and the existing local `origin/main` ref were all
  `2606baa8f33bafc5493c24dc547c95a8b153fc8c` when the branch was created. No
  `fetch` or `pull` was run.
- `codex/v13-canonical-platform` did not exist locally or on GitHub before the
  local branch was created. It remains local-only.
- Five Draft PRs were open. None contains the #714-#724 46-table canonical-only
  platform scope. The two V1.3-named PRs are separately classified:
  - #703: legacy Task 1 market migration evidence only; it does not define the
    canonical manifest, genesis, API, UI, or runtime chain.
  - #712: no-trade runtime prototype explicitly bound to
    `freqtrade_ai_design_lab` / v47. It is historical/prototype evidence and not
    the canonical production path.
- The remote branches behind #694/#705/#708/#710 are merged historical foundation,
  migration, and owner-control prototypes. The #699/#707 branches remain legacy
  evidence tracks, not canonical production dependencies.

## 2. Exact dirty ownership manifest

The pre-RC-document baseline contained exactly 9 tracked modifications and 63
untracked files. Every path below belongs to V1.3 canonical-only scope; no unrelated
user file was found. This release-candidate document is one additional untracked
documentation file and is assigned to commit group A.

### 2.1 Tracked modifications: 9/9

| Path | Owner / scope | Commit group |
| --- | --- | --- |
| `docs/product/strategy_platform_v1_design.md` | legacy design pointer and canonical authority notice | A |
| `frontend/package.json` | canonical Playwright script registration | F |
| `frontend/src/App.tsx` | six canonical page routes | F |
| `frontend/src/layout/AppLayout.tsx` | canonical/legacy navigation context without breaking legacy return links | F |
| `frontend/src/layout/navigation.ts` | canonical navigation and legacy reclassification | F |
| `frontend/src/main.tsx` | canonical stylesheet entry | F |
| `frontend/src/styles/formal-workspace.css` | 1280x720 legacy Local Strategy Lab regression fix | F |
| `frontend/tests/desktopShell.test.mjs` | navigation contract update | F |
| `frontend/vite.config.ts` | canonical 8001 versus legacy 8000 longest-prefix proxy split | F |

### 2.2 Untracked backend implementation: 29/29

| Path | Owner / scope | Commit group |
| --- | --- | --- |
| `backend/app/canonical_v13/__init__.py` | independent package exports | A |
| `backend/app/canonical_v13/accounting.py` | ledger-writer simulated service | D |
| `backend/app/canonical_v13/activation_readiness.py` | market/window/bundle activation gate | B |
| `backend/app/canonical_v13/api.py` | exact 13-route API and split reader/control factories | E |
| `backend/app/canonical_v13/bundles.py` | immutable research bundle preview/activation | B |
| `backend/app/canonical_v13/control_plane.py` | seven P0 configuration lifecycle and snapshot authority | B |
| `backend/app/canonical_v13/deployment_approval.py` | independent approval writer | D |
| `backend/app/canonical_v13/deployment_control.py` | deployment/runtime identity writer | D |
| `backend/app/canonical_v13/dto.py` | command/projection/receipt DTO separation | E |
| `backend/app/canonical_v13/execution_common.py` | execution lineage guard shared without cross-writer DML | D |
| `backend/app/canonical_v13/fill_service.py` | fill-writer simulated service | D |
| `backend/app/canonical_v13/genesis.py` | empty-database genesis, offline DDL/owner/ACL evidence | A |
| `backend/app/canonical_v13/intake.py` | latest-only controlled intake and artifact-only dedupe | B |
| `backend/app/canonical_v13/manifest.py` | 46-table/domain/writer/reader authority | A |
| `backend/app/canonical_v13/market.py` | market profile/artifact/receipt/snapshot authority | B |
| `backend/app/canonical_v13/market_acquisition.py` | no-network acquisition port and evidence receipt | B |
| `backend/app/canonical_v13/models.py` | independent `CanonicalBase` and 46 canonical tables | A |
| `backend/app/canonical_v13/optimization.py` | post-qualified controlled re-submission | C |
| `backend/app/canonical_v13/order_service.py` | order-writer simulated service | D |
| `backend/app/canonical_v13/production.py` | standalone production composition root; no legacy bootstrap | E |
| `backend/app/canonical_v13/reconciliation.py` | reconciliation-writer and digest idempotency | D |
| `backend/app/canonical_v13/research_authorization.py` | one-shot research authorization receipts | C |
| `backend/app/canonical_v13/research_evaluation.py` | scoring, hard-gate qualification, optimizer gate | C |
| `backend/app/canonical_v13/research_execution.py` | explicit no-trade execution authorization boundary | C |
| `backend/app/canonical_v13/research_validation.py` | static/lookahead/ephemeral research plan and attempts | C |
| `backend/app/canonical_v13/risk_service.py` | intent/risk-writer simulated service | D |
| `backend/app/canonical_v13/runtime_contract.py` | frozen runtime launch/observation receipt contract | D |
| `backend/app/canonical_v13/runtime_reader.py` | narrow frozen canonical runtime reader | C |
| `backend/app/canonical_v13/signal_service.py` | signal-writer simulated service | D |

### 2.3 Untracked backend tests: 16/16

| Path | Contract | Commit group |
| --- | --- | --- |
| `backend/tests/test_canonical_v13_activation_readiness.py` | activation coverage/freshness/no-side-effect gate | B |
| `backend/tests/test_canonical_v13_api.py` | route/DTO/OpenAPI/error/transaction/role split | E |
| `backend/tests/test_canonical_v13_bundles.py` | bundle determinism and activation | B |
| `backend/tests/test_canonical_v13_control_plane.py` | seven P0 lifecycle and dynamic allocations/caps | B |
| `backend/tests/test_canonical_v13_design_authority.py` | Phase 0 design authority and legacy reclassification | A |
| `backend/tests/test_canonical_v13_genesis.py` | empty database, repeat no-op, identity and isolation | A |
| `backend/tests/test_canonical_v13_intake.py` | latest-only intake and artifact-only dedupe | B |
| `backend/tests/test_canonical_v13_integration.py` | source-layout import, production composition, proxy/runbook | E |
| `backend/tests/test_canonical_v13_manifest.py` | manifest/FK/index/type/DDL/ACL/single-writer contracts | A |
| `backend/tests/test_canonical_v13_market.py` | market evidence and snapshot binding | B |
| `backend/tests/test_canonical_v13_optimization.py` | qualified-baseline optimization re-submission | C |
| `backend/tests/test_canonical_v13_research_authorization.py` | one-shot authorization/revoke/consume | C |
| `backend/tests/test_canonical_v13_research_evaluation.py` | scoring/qualification hard gates | C |
| `backend/tests/test_canonical_v13_research_validation.py` | static/lookahead/no-network validation attempts | C |
| `backend/tests/test_canonical_v13_runtime_chain.py` | approval through reconciliation simulated chain | D |
| `backend/tests/test_canonical_v13_runtime_reader.py` | frozen reader and no legacy fallback | C |

### 2.4 Untracked design/runbook: 2/2 baseline plus this RC document

| Path | Owner / scope | Commit group |
| --- | --- | --- |
| `docs/product/strategy_platform_v13_canonical_design.md` | #715 canonical-only design authority and #716-#724 gates | A |
| `docs/runbooks/strategy_platform_v13_canonical_genesis.md` | future empty-database/owner/ACL/API rollout order | E |
| `docs/releases/strategy_platform_v13_release_candidate.md` | release ownership, commit, PR, CI and rollout plan | A |

### 2.5 Untracked frontend: 16/16

| Path | Owner / scope | Commit group |
| --- | --- | --- |
| `frontend/playwright.canonical-v13.config.ts` | isolated canonical Playwright profile | F |
| `frontend/src/api/canonicalV13Client.ts` | exact canonical root and one-fetch client | F |
| `frontend/src/api/canonicalV13Types.ts` | runtime-validated canonical DTO types | F |
| `frontend/src/pages/canonicalV13/CanonicalConfigurationPage.tsx` | P0 configuration projection/editor | F |
| `frontend/src/pages/canonicalV13/CanonicalMarketDataPage.tsx` | market evidence projection | F |
| `frontend/src/pages/canonicalV13/CanonicalOptimizationPage.tsx` | optimization empty/blocked/results projection | F |
| `frontend/src/pages/canonicalV13/CanonicalResearchPage.tsx` | independent research/runtime readiness views | F |
| `frontend/src/pages/canonicalV13/CanonicalStatePanel.tsx` | shared fail-closed state rendering | F |
| `frontend/src/pages/canonicalV13/CanonicalStrategiesPage.tsx` | strategy catalog/detail projection | F |
| `frontend/src/pages/canonicalV13/CanonicalSubmissionPage.tsx` | controlled latest-only intake command | F |
| `frontend/src/pages/canonicalV13/canonicalV13Model.ts` | strict URL and unknown-enum contracts | F |
| `frontend/src/styles/canonical-v13.css` | canonical page styles, selector-scoped | F |
| `frontend/tests/canonicalV13.e2e.ts` | empty/BLOCKED/URL/enum browser contracts | F |
| `frontend/tests/canonicalV13Client.test.mjs` | route/runtime DTO/error client contracts | F |
| `frontend/tests/canonicalV13Model.test.mjs` | URL/state/no-default contracts | F |
| `frontend/tests/helpers/vite.canonical-v13.e2e.config.ts` | isolated Vite E2E server | F |

## 3. Proposed minimal commit stack

No group is staged. A future staging authorization must use `git add --` followed
only by the exact paths listed for that group; `git add -A`, `git add .`, and
`git add --all` remain forbidden.

### Group A — design and schema authority

Proposed subject: `feat(v13): establish canonical schema authority`

Files: every A path in section 2, including manifest/models/genesis, three schema
tests, the two product design files, and this RC document.

Dependencies: none beyond `origin/main@2606baa8`.

Acceptance:

- `test_canonical_v13_design_authority.py`
- `test_canonical_v13_genesis.py`
- `test_canonical_v13_manifest.py`
- offline PostgreSQL DDL/owner/ACL compilation and exact allowlist

Rollback boundary: reverting A removes the independent canonical schema authority
without touching legacy metadata/migrations. Groups B-F cannot remain without A.

### Group B — intake, P0 control plane, market and activation

Proposed subject: `feat(v13): add canonical intake and control plane`

Files: every B path in section 2.

Dependencies: A.

Acceptance: focused intake/control-plane/market/bundle/activation tests; genesis
business rows remain zero until explicit commands; no validation/runtime/order rows.

Rollback boundary: revert B before A. No production data exists, so local rollback
is code-only. It must never be used to delete a real database.

### Group C — no-trade research and optimization

Proposed subject: `feat(v13): add authorized no-trade research pipeline`

Files: every C path in section 2.

Dependencies: A and B.

Acceptance: research authorization, validation, evaluation, optimization and frozen
reader tests; static/lookahead executor remains no-network/no-credential/no-order.

Rollback boundary: revert C before B/A. Terminal receipts remain append-only in any
future real environment; code rollback must not rewrite evidence.

### Group D — isolated Demo execution services

Proposed subject: `feat(v13): define capability-separated demo runtime chain`

Files: every D path in section 2.

Dependencies: A-C and an explicit qualified lineage in tests only.

Acceptance: `test_canonical_v13_runtime_chain.py`, single-writer static checks, and
zero real exchange/service activity.

Rollback boundary: revert D independently before C. This commit defines contracts
and simulators only; it grants no production approval or runtime authority.

### Group E — standalone API and production composition contract

Proposed subject: `feat(v13): expose standalone canonical API`

Files: every E path in section 2.

Dependencies: A-D.

Acceptance: API and integration tests, exact 13 OpenAPI operations, split reader/
control connection factories, error redaction, source-layout import, and runbook
static contract.

Rollback boundary: revert E removes the deployable API entrypoint and runbook while
leaving domain code unmounted. Legacy `app.main` remains unchanged throughout.

### Group F — canonical UI and legacy-safe routing

Proposed subject: `feat(frontend): add canonical V1.3 workspace`

Files: every F path in section 2.

Dependencies: E API prefix/DTO contract.

Acceptance: frontend Node suite, TypeScript/Vite build, isolated canonical Playwright,
and default repository Playwright including legacy routes.

Rollback boundary: revert F removes only canonical UI/proxy/navigation integration
and restores the legacy frontend; backend canonical modules remain standalone.

## 4. Draft pull request package

Proposed title:

`feat(v1.3): add canonical-only strategy platform release candidate`

Proposed base/head: `main` <- `codex/v13-canonical-platform`

Draft status: `true`

Proposed body:

```markdown
## Outcome

Introduces the independent Strategy Platform V1.3 canonical-only release candidate:
46-table manifest/genesis, latest-only intake, seven P0 configuration domains,
market evidence, authorized no-trade research, scoring/qualification/optimization,
capability-separated Demo contracts, standalone API, and six canonical UI routes.

Tracks #714. Implements local code/design scope for #715, #716, #717, #718,
#719, #720, #721, #722, #723, and #724. These Issues remain open until their
independent real-environment gates are accepted.

## Canonical boundary

- independent `CanonicalBase`; no legacy `Base.metadata` or v47 bootstrap
- dedicated empty PostgreSQL database identity; no legacy fallback
- reader/control and runtime/writer capabilities remain separated
- legacy application remains operational and does not auto-mount or auto-install
  canonical genesis

## Legacy and prototype reclassification

- #694/#696/#698: historical foundation patterns only
- #705/v47 and #703: legacy/design-lab migration evidence
- #708/#710: control-plane plan/executor prototypes
- #712: v47 no-trade runtime prototype
- #699/#707: superseded legacy migration/activation tracks, not canonical blockers

## Verification

- canonical backend: 128 passed
- safe full backend: 1844 passed, 214 environment/PostgreSQL skips,
  1 macOS Keychain mutation test explicitly deselected
- frontend Node: 174 passed
- TypeScript/Vite build: passed
- canonical Playwright: 5 passed
- default isolated Playwright: 44 passed, 4 profile-gated skips
- compileall, secret scan, tracked/untracked diff checks: passed
- offline PostgreSQL: 46 tables, 79 FKs, 42 checks, 43 uniques,
  73 indexes, 47 timezone-aware DateTimes, 20 JSON columns, 914 ACL statements

## Still BLOCKED

- no real PostgreSQL genesis/owner/ACL/backup/reverse-proxy evidence
- production target/allocation/per-target cap and fresh market/window data are UNSET
- no real static/lookahead/backtest/score/qualification/optimization execution
- no production approval, credentials, OKX, service/runtime, signal, or order activity

This PR does not authorize migration, activation, backtest, runtime, or trading.
```

## 5. CI matrix and release checklist

| Gate | Command/profile | Required result | Meaning / exclusion |
| --- | --- | --- | --- |
| Canonical backend | `pytest -q backend/tests/test_canonical_v13_*.py -p no:cacheprovider` | 128 passed | SQLite/domain/API and offline compiler contracts only |
| Full backend safe profile | dotenv disabled, offline, no DB URL; deselect macOS Keychain mutation node | 1844 passed; 214 expected skips; 1 explicit deselect | does not run PostgreSQL contracts or touch Keychain |
| Frontend Node | `npm test` | 174 passed | models/client/legacy shell contracts |
| Frontend build | `npm run build` | `tsc && vite build` pass | existing >500 kB chunk warning recorded, not hidden |
| Canonical browser | `npm run test:e2e:canonical-v13` | 5 passed | isolated Vite and mocked canonical API |
| Repository browser | offline default Playwright with explicit dependency Python | 44 passed; 4 profile-gated skips | temporary SQLite acceptance server, canonical plus legacy paths |
| PostgreSQL offline | manifest tests and dialect compilation | 46-table/DDL/index/timezone/JSON/FOR UPDATE/ACL exact | not a real PostgreSQL acceptance |
| Compile | `python -m compileall backend/app backend/tests scripts` | pass | pycache directed outside/re-cleaned |
| Secret scan | `python scripts/scan_secrets.py` | pass | no secret-shaped source values |
| Diff | tracked `git diff --check` plus per-untracked no-index check | pass | not a substitute for lint |

Release review must additionally confirm:

- exact base/head and all six commit groups in dependency order;
- no staged path outside section 2;
- no generated `dist`, cache, trace, runtime lock, dry-run config, or credential file;
- CI terminal on the proposed commit SHA, not an earlier run;
- review threads resolved without widening DB/runtime/trade authority;
- Issue #714 and #715-#724 remain open unless independently accepted.

Repository lint/format tooling is `NOT_CONFIGURED`; do not report diff-check as lint.
Backend packaging remains the repository source-layout/PYTHONPATH contract, not a wheel.

## 6. Future real empty-database rollout checklist — do not execute here

Every checkbox is independent evidence. A missing receipt is `UNKNOWN/BLOCKED`.

### Change control and recovery

- [ ] explicit real-database authorization and named operator/change window
- [ ] exact new database host/name proves it is not `freqtrade_ai` or
  `freqtrade_ai_design_lab`
- [ ] recoverable backup/snapshot and restore owner/receipt
- [ ] old databases and v47 evidence remain intact and read-only
- [ ] rollback fence prevents API/service startup on partial/unknown state

### Roles and empty database

- [ ] `canonical_schema_owner` is NOLOGIN
- [ ] every manifest reader/writer role exists without inherited composite privilege
- [ ] provisioning role has reviewed DDL/GRANT/owner-transfer authority only
- [ ] database-wide inventory proves no user tables/views/materialized views/sequences/
  functions/custom types outside the allowed empty state
- [ ] offline DDL/owner/ACL render digest matches the reviewed release commit

### Genesis transaction and independent verification

- [ ] dry-run is performed only in a disposable new PostgreSQL 16 database
- [ ] one transaction performs genesis identity + 46 tables, exact ACL, then owner
  transfer; any failure rolls back the whole transaction
- [ ] first install reports business rows 0 and `TRADING_DISABLED`
- [ ] exact repeat install is a no-op; partial/drift install is blocked, never repaired
- [ ] reader and each writer role independently prove only the required SELECT/DML
  surface; runtime identity verification does not scan unrelated tables

### API and routing

- [ ] canonical reader and control DSNs use different roles on the same new database
- [ ] legacy `DATABASE_URL` is not used by the canonical process
- [ ] standalone canonical API starts only after genesis/ACL acceptance
- [ ] reverse proxy sends `/api/canonical-v13` only to canonical and all other
  `/api` traffic remains on legacy
- [ ] 13-route smoke proves reader/control split, empty/BLOCKED projections, error
  redaction, and no auto-activation

### Controlled data entry and activation

- [ ] latest-only source inventory is frozen; every source strategy becomes an
  independent strategy v1; only artifact bytes dedupe
- [ ] intake performs no import, validation, backtest, or execution
- [ ] production TARGET and per-target allocation/cap are explicitly reviewed
- [ ] WINDOW, GENERATION, DIVERSITY, QUALITY_QUALIFICATION, SCORING, and
  RESEARCH_AGGREGATE are independently validated with no cycle/default fallback
- [ ] fresh canonical market acquisition/inspection/receipt and exact target/window
  coverage are accepted
- [ ] research bundle preview digest/id is reviewed before explicit activation

### First research and Demo runtime — separate authority

- [ ] separate one-shot authority explicitly permits the first real static/lookahead/
  backtest attempt; sandbox/network/credential/exchange/writer evidence is retained
- [ ] dynamic required-window results, scorer, hard-gate qualifier, and optimization
  lineage are independently accepted; high score cannot override a hard gate
- [ ] separate human approval permits Demo deployment only after `QUALIFIED`
- [ ] runtime reader, launcher identity, heartbeat/capability/rollback receipts pass
- [ ] signal/risk/order/fill/ledger/reconciliation writers pass independently using
  fake/simulated exchange before any credential-bearing Demo action
- [ ] any credential/OKX/service/order activity receives a new explicit authorization;
  live/real-funds capability remains forbidden

## 7. Explicit authorization ledger

The next Git actions require four separate user authorizations:

1. **Stage**: exact group/path list only; this does not authorize a commit.
2. **Commit**: approved staged group and message only; this does not authorize push.
3. **Push**: exact local branch to exact remote; this does not authorize a PR.
4. **Draft PR**: one Draft PR with the reviewed base/head/title/body; this does not
   authorize ready-for-review, merge, Issue closure, or deployment.

Real environment authority is completely separate from Git publication:

- database inventory/backup/genesis/owner/ACL;
- API/service startup and reverse-proxy change;
- latest-only real intake and production P0/market activation;
- each real research/backtest/qualification/optimization attempt;
- approval, credentials/OKX, Demo runtime, signal/order/fill/accounting activity.

No authority in one list implies authority in the other.
