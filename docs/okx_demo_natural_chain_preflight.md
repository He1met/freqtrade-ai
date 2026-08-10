# OKX Demo natural-chain preflight contract

This contract separates deploy-time proof from the final natural Demo window.
It does not repair or bypass a current business blocker.

## Layer 1: isolated PostgreSQL, zero orders

Run:

```bash
make natural-chain-preflight
```

The default command starts a disposable PostgreSQL cluster with real roles,
migrations, ACLs, triggers, and `SECURITY DEFINER` functions. It inserts only
synthetic contract fixtures into random schemas in that temporary cluster. It
does not read canonical runtime state, load exchange credentials, call OKX, or
create an exchange order. The cluster is stopped and removed after the run.

CI may use an already disposable PostgreSQL service, but the operator must make
the isolation claim explicit and the database name must contain `preflight` or
`test`:

```bash
backend/.venv/bin/python scripts/okx_demo_natural_chain_preflight.py \
  --database-url "$POSTGRES_PREFLIGHT_URL" \
  --external-isolated-cluster
```

Never pass the canonical `freqtrade_ai` database or a PostgreSQL cluster that
shares global roles with the canonical runtime. The preflight intentionally
exercises role hardening and therefore requires a disposable cluster, not just
a separate schema in the canonical cluster.

## Coverage and failure classification

| Failure class | Isolated proof | Final natural-window proof |
| --- | --- | --- |
| `PREFLIGHT_INFRASTRUCTURE_BLOCKED` | PostgreSQL binaries/service, admin connection, and test dependencies exist | Not applicable |
| `SCHEMA_ACL_SECURITY_DEFINER` | Current migrations install exact owner/function ACLs; runtime direct DML and forged attestation writes are denied | Canonical migration/ACL verification after deployment |
| `SNAPSHOT_SIGNAL_BINDING` | Instrument, market, account, session, digest, expiry, and pinned-account bindings accept the valid fixture and fail closed on invalid evidence | A fresh natural evaluation binds the actual OKX Demo snapshot bundle; the present `market snapshot binding is invalid` remains a blocker until separately fixed |
| `RECEIPT_LINEAGE_DEPLOYMENT_POLICY` | ACTIONABLE receipt, signal digest, candidate approval, active deployment, deployment set, risk policy, and execution lineage agree | Natural ACTIONABLE receipt and lineage IDs agree with canonical persisted rows |
| `DEMO_READINESS_RECONCILIATION_GUARD` | `OKX_DEMO`, `real_orders=false`, complete risk checkpoint, fresh recovered reconciliation, and guard digests are required | Canonical readiness is `READY/RECOVERED/UNIQUE/RUNNING` immediately before submission and after terminal reconciliation |
| `WRITER_LEASE_FENCING` | Only one writer/lease winner; stale owner/fence is rejected; checkpoint recovery cannot recapture a signal | The existing unique canonical writer owns the naturally created approval through terminal handling |
| `RISK_BUDGET_DECISION_IDEMPOTENCY` | Owner-mediated budget initialization/reservation, decision rows, replay identity, contention, rejection, and zero `exchange_orders` | One natural Demo intent has one risk decision and at most one simulated exchange order; retry does not duplicate it |
| `CONTRACT_GATE_FAILED` | One or more named tests failed; inspect the per-category JSON and optional JUnit report | Deployment is blocked |

Layer 1 success means the code and database contract are eligible for normal
review. It is not permission to deploy, restart, trigger a signal, or submit an
order.

## Layer 2: deployment-time natural OKX Demo acceptance

This PR does not execute Layer 2. After a separately authorized merge,
migration, deployment, and controlled restart, wait for the existing 15-minute
cadence. Do not manufacture, replay, manually trigger, or accelerate a signal.
`NO_ACTION` remains a valid no-order outcome and the observation continues.

Only a naturally occurring `ACTIONABLE` evaluation may traverse the existing
canonical owner-mediated path. Before it proceeds, re-verify `OKX_DEMO`,
`real_orders=false`, unique writer, readiness, reconciliation, deployment and
risk policy digests, lease fencing, budget, and idempotency. Acceptance requires
one simulated Demo order at most, its terminal fill/cancel state, and a final
reconciliation with no unexplained order, fill, position, or budget drift.

Any missing/stale evidence, ACL change, policy mismatch, duplicate identity,
unexpected DML capability, live/real-order flag, or need for broader privilege
is fail-closed and stops the acceptance. It must never be converted into a
manual signal or order.
