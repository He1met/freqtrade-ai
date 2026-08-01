# OKX Demo risk authorization chain

Issue #446 adds one PostgreSQL transaction for:

`TradeIntent -> RiskDecision -> ApprovedExecution`

The chain is restricted to `OKX_DEMO`. It is an offline authorization result,
not an exchange order. `ApprovedExecution.order_submission_authorized` is
database-constrained to `FALSE`, and the later claim path must re-check target
authorization before #447 can submit anything.

## Automatic Demo candidate approval

`OKX_DEMO` does not require a per-candidate human click. After the immutable
generation, backtest, and scoring stages have completed, the full-chain worker
may call `FullChainRepository.auto_approve_candidate`. The decision actor is
fixed to `system:okx-demo-auto-promotion`, expires after five minutes, and is
accepted only when the existing version validation, positive net profit,
drawdown, trade-count, net-of-costs, out-of-sample, and walk-forward market-state
gates all pass.

The candidate digest, promotion policy version, system actor, hard-gate summary,
target, and `allow_real_funds=false` are persisted in the
`CANDIDATE_APPROVAL` checkpoint. A successful decision resumes the same
DB-backed research job at `SIGNAL` without creating a second attempt or
re-running DeepSeek. Missing or changed evidence remains fail-closed. This
automatic decision is not available to `OKX_LIVE` and does not itself authorize
an exchange order.

The durable `demo_automation` manifest also records standing authorization for
automatic strategy selection, controlled Demo order submission, and local
service recovery. Those permissions are capabilities, not bypasses: an order
still requires fresh trusted snapshots, a passing risk decision, position and
notional limits, the unique writer, target-scoped idempotency, and healthy
reconciliation. Missing evidence blocks automatically. The manifest cannot be
weakened to `OKX_LIVE`, real funds, optional risk checks, multiple writers, or
non-reconciled writes.

## Research promotion into the Demo runtime

The DB-backed research worker opens a `RESEARCH` full-chain run and prepares
the `GENERATION` checkpoint before a real DeepSeek call. A successful response
can advance only by loading the exact persisted generation, strategy version,
backtest result, and score database rows under the same ResearchJob lease.

Promotion requires a plan declared before validation execution: one independent
OOS window and at least three non-overlapping walk-forward windows covering
persisted bull, bear, and range market moves. Every window has its own
BacktestRun/Task/Result, Freqtrade execution ID, manifest, and config/result/
strategy/market-data checksums. Validation results never replace the primary
StrategyScore. A single-result trade slice, request label, aggregate ranking
score, fixture, or manually supplied JSON cannot become production promotion
evidence. Missing, ambiguous, overlapping, or drifted evidence closes the
candidate path as `BLOCKED`.

Only the locked `OKX_DEMO` automation policy may approve a passing candidate.
The worker then releases its lease and reclaims the same attempt at `SIGNAL`;
the deployment continuation loads the immutable approval and publishes
idempotently. A database partial unique index permits at most one ACTIVE
deployment for the target. Neither step enables order submission or real
funds.

## Required evidence

Every request must carry a consistent:

`Strategy -> StrategyVersion -> BacktestRun -> BacktestTask -> BacktestResult -> StrategyScore`

lineage. The version must be validated, the run and task must have succeeded,
the result must point to both, and the score must match the configured scoring
version and minimum threshold.

Instrument, market, and account evidence is first written by the internal
OKX Demo read-adapter boundary to `okx_demo_trusted_snapshots`. The attested
factory owns a private, non-serializable capability with a frozen session ID,
pinned account fingerprint, proof, and expiry. Snapshot writes accept only that
capability plus the factory's normalized product; session/fingerprint values
are never request parameters. Authorization
callers supply only three opaque `snapshot_ids`; they cannot supply or self-sign
content, digests, or references. The service loads the database rows and
recomputes both the canonical content digest and attested snapshot identity. It
also requires an immutable `api_aggregate`/core-data record plus
`OKX_DEMO`, `okx_demo_rest`, a non-stale resource attestation, and authenticated
account evidence. Instrument content binds the SWAP contract value, currency,
lot, minimum size, tick, and linear/inverse shape. Market content binds the
instrument and reference-price timestamps. Account content binds net/isolated
mode, exposure, open positions, and leverage. Missing, forged, stale, or
inconsistent evidence creates a durable redacted `BLOCKED` decision without a
budget reservation or permission.

## Determinism and idempotency

The service canonicalizes only authorization inputs. Free-form LLM text is
excluded and cannot influence permission. It persists:

- `canonical_hash`: SHA-256 of the canonical authorization payload;
- `policy_digest`: SHA-256 of the exact policy used for authorization;
- `approved_payload_hash`: SHA-256 of normalized order fields, lineage,
  trusted snapshot database identities/digests, and policy binding;
- `intent_id`: SHA-256 of target, input digest, policy digest, and
  idempotency-key digest;
- `client_order_id`: deterministic 32-character OKX-compatible ID;
- target-scoped idempotency uniqueness.

A retry reads an existing permission only when both input and policy digests
match and every persisted expiry is still current. Input/policy conflicts
durably block and revoke the old permission. Expired retries persist `EXPIRED`,
delete the permission row, and release its reservation. PostgreSQL advisory
locking serializes concurrent retries.

## Risk policy

The policy checks instrument, side, and order-type allowlists; market/limit
field combinations; net/isolated mode; leverage; contract lot/tick rules;
single-order notional; total exposure; maximum position count; price deviation;
and side-aware stop-loss/take-profit ordering. Linear SWAP notional is
`contracts * ctVal * price`; inverse SWAP notional is `contracts * ctVal`.
The risk budget starts from the trusted account exposure and position count,
then adds locally reserved approvals under a row lock.

An `APPROVED` result reserves notional and a position slot under a PostgreSQL
transaction/advisory lock plus `SELECT FOR UPDATE`. This prevents concurrent
requests from exceeding the shared budget. `REJECTED`, `BLOCKED`, and `EXPIRED`
results cannot retain an `ApprovedExecution`. Database checks and composite
foreign keys independently enforce the target, decision/intent relationship,
active claim contract, hashes, enums, and no-submission boundary.
The mandatory authorization schema for a permission is `RISK_V1`. Rows from
older schema versions migrate to `LEGACY/BLOCKED`; their permissions are
deleted and reservations released. PostgreSQL triggers reject changes to
critical intent fields while an `ACTIVE` permission exists and reject every
update/delete of a trusted snapshot row.

## One-time PostgreSQL ownership hardening

The application continues to use the single existing `DATABASE_URL` and the
single `freqtrade_ai` database. The runtime role remains `freqtrade`; no second
LOGIN role or database is created. After deploying schema `20260727_08`, run
the migration once through the local peer-admin identity:

```bash
make db-attestation-harden
```

This creates only the `NOLOGIN NOINHERIT` owner `freqtrade_ai_attestor`, stores
one random HMAC root key in macOS Keychain and its matching copy in an
attestor-owned secret table, and transfers the attestation tables and
SECURITY DEFINER functions to that role. The runtime has no access to the
secret table and can only execute signature-verifying create/write/revoke
functions. The one-shot command proves that the runtime and peer-admin
connections share the same PostgreSQL system identifier, database OID, port,
and local server before reading or creating the Keychain item. It also removes
runtime membership in the attestor role and all runtime/PUBLIC column grants.
Normal startup/readiness fails closed on direct or recursive role membership,
privileged attestor parents, table/column ACLs, function definition hashes, or
an unexpected `search_path`.

Every `ApprovedExecution` also stores three non-null snapshot IDs with
`ON DELETE RESTRICT` foreign keys to the trusted registry. Idempotent retries
and execution-side `claim_active_approval` calls lock and reload those
snapshots and their attested sessions. Revocation, expiry, identity drift,
missing evidence, or failed database readiness atomically removes the
permission, releases its reserved budget, and marks the intent/decision
`BLOCKED` or `EXPIRED`.

## Verification

From `backend/`:

```bash
python -m pytest -q tests/test_risk_chain.py
POSTGRES_WORKER_URL=postgresql+psycopg://... \
  python -m pytest -q tests/test_risk_chain_postgresql.py
```

The PostgreSQL gate uses a random isolated schema. It covers
`20260727_02/03/04/05/06/07 -> 20260727_08`, legacy-permission revocation, concurrent
budget and idempotency races, schema/trigger-definition tampering, immutable
trusted snapshots, and direct database authorization tampering.
