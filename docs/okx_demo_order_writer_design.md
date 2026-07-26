# OKX_DEMO SWAP order writer design

Issue #447 owns the only project process that may submit exchange writes. This
document freezes the implementation boundary while #444 and #446 are landing;
it deliberately does not define another `TradeIntent`, `RiskDecision`, or risk
policy.

## Dependency boundary

- #444 owns durable client-order identity, write-response classification, and
  `RECOVERY_REQUIRED` semantics. The writer must import the shared
  implementation extracted from the merged canary; it must not fork it.
- #445 owns the fixed Demo origin, direct/no-redirect HTTP boundary, frozen
  credential bundle, account attestation, read normalization, and
  `InstrumentSpec`.
- #446 owns the concrete approved risk result and immutable trusted-snapshot
  lineage. #447 claims that persisted result and rejects any mismatch in the
  approval, canonical, policy, payload, or snapshot identities.
- Existing `TradeIntent`, `RiskDecision`, `ExchangeOrder`, `ExchangeFill`, and
  `ExchangePosition` remain the persistence lineage. #447 does not create
  replacement lineage models.

## Controlled authorization

The normal application manifest keeps `order_submission_enabled=false`.
Starting backend, worker, frontend, Freqtrade, Agent Trade Kit, or the read
adapter never enables the writer.

The production server factory exposes a controlled lifecycle writer, never the
raw authenticated POST transport. The production transport has no importable
capability token and accepts only the exact bridge created from an
`OkxDemoReadClient`; the recorded test harness always targets
`https://offline.invalid` and cannot address OKX Demo. One explicitly
controlled call may construct a short-lived
`OrderSubmissionAuthorization` with all of the following immutable values:

- `execution_target_id=OKX_DEMO`;
- `allow_real_funds=false`;
- `simulated_trading=true`;
- `order_submission_enabled=true`;
- one writer-instance identity and one expiry;
- one approved execution identity.

The authorization is scoped to one approved order lifecycle and may be reused
only for that approval's cancel, amend, recovery, or risk-reducing cleanup
while it remains unexpired; it cannot be stored as a global flag. `OKX_LIVE`,
missing/expired authorization, a different approval, or an inherited shell
flag fails before DB lease acquisition, credential access, or network
activity.

This is an application safety boundary, not a sandbox against arbitrary
malicious Python already executing inside the process. Such code could bypass
the project and open its own socket; OS account isolation and credential
access control remain responsible for that threat.

## Approval adapter

The writer consumes a structural `ApprovedExecution` interface. The eventual
#446 adapter must provide:

- approval, trade-intent, and risk-decision database IDs;
- `execution_target_id`, approval time, expiry, and policy identity;
- deterministic legal `client_order_id`;
- SWAP instrument, side, `posSide=net`, `tdMode=isolated`, order type,
  contracts, and optional limit price;
- `reduce_only` and optional attached TP/SL values;
- a stable idempotency digest.

The interface contains no method that can approve itself. The #447 adapter
only converts the already-approved #446 value into an immutable writer
command. Missing, expired, target-mismatched, reused, or inconsistent approval
evidence is fail-closed after #446 is integrated.

## Single-writer persistence

The writer migration adds two writer-owned tables after the #446 migration:

1. `okx_order_writer_leases`
   - primary key `execution_target_id`, constrained to `OKX_DEMO`;
   - holder-token SHA-256, generation, acquired/heartbeat/expiry timestamps;
   - one active row plus PostgreSQL transaction/advisory locking;
   - SQLite uniqueness exercises the same repository behavior in unit tests.
2. `okx_order_write_attempts`
   - FK to the existing `exchange_orders` row;
   - operation: `SET_LEVERAGE`, `PLACE`, `CANCEL`, `AMEND`, or `CLOSE`;
   - state: `PREPARED`, `ACKNOWLEDGED`, `REJECTED`,
     `RECOVERY_REQUIRED`, `RESIDUAL_CLOSE_REQUIRED`, or `RECONCILED`;
   - deterministic operation identity, request digest, bounded attempt count,
     last-attempt timestamp, and sanitized snapshots;
   - unique operation identity and at most one nonterminal operation per order.

`PREPARED` is committed before the network write. No database transaction is
held during a network request. A process crash therefore leaves a durable
operation that the next writer must reconcile before any new operation.
Lease expiry never converts an unresolved operation to success and never
authorizes a second placement.
Every attempt stores the lease generation as a fencing token. A replacement
writer atomically adopts an unresolved attempt at the new generation; the
expired holder can no longer transition it. Reusing the same human-readable
instance name does not reuse the per-process random holder token.

## State machine

```text
PREPARED
  | explicit exchange rejection
  +------------------------------> REJECTED
  |
  | validated single-item acknowledgement
  +------------------------------> ACKNOWLEDGED -> RECONCILED
  |
  | timeout, invalid JSON, incomplete acknowledgement, identity mismatch
  +------------------------------> RECOVERY_REQUIRED
                                      |
                                      | query original clOrdId / reqId
                                      +-------------> RECONCILED
```

Only a top-level non-zero OKX code or a single item with an explicit non-zero
`sCode` is a known rejection. HTTP 200 with missing/non-array/empty/multiple
`data`, missing `sCode`, missing IDs, invalid JSON, or identity mismatch is an
unknown write outcome.

`RECOVERY_REQUIRED` is sticky. The next invocation recovers the same operation
identity and returns; it does not perform a new placement in that invocation.
Recovery phase names are set-like, while attempt count and last-attempt time
are updated in fixed fields.

A terminal close order with non-zero remaining position becomes
`RESIDUAL_CLOSE_REQUIRED`, not success. A later explicitly authorized call for
the same unexpired approval reads the residual position and creates a new
deterministic `reduceOnly` cleanup order (`C1` through `C3`). The original
`clOrdId` is never resent. One unresolved cleanup continues to freeze opening
writes; exhaustion stays blocked for operator review rather than silently
unlocking the writer.
Each cleanup must exactly equal the original approved quantity minus the
exchange-reported cumulative fills across prior close attempts. An unexplained
position increase or incomplete fill evidence is blocked and requires a new
approval.

## Order operations

- Leverage: query the isolated net-mode leverage first; submit an attested
  `set-leverage` request only on mismatch, then query again. Unknown outcomes
  are recovered by reads and are never reposted blindly.
- Limit: tick-aligned price and lot-aligned contracts at or above `minSz`.
- Market: no limit price; contracts still use `lotSz`/`minSz`.
- Cancel: acknowledgement is not terminal; query until canceled or another
  proven terminal state.
- Amend: deterministic `reqId`; timeout is reconciled by querying the original
  order and comparing requested price/size, never by blind resubmission.
- Attached TP/SL: submitted only in the original `attachAlgoOrds`; trigger
  prices are tick-aligned and child client IDs are deterministic.
- Close: isolated net position only, implemented as an opposite-side
  `reduceOnly` market order with exact current contracts; completion requires
  original/close order reconciliation and zero position.

Every response requires HTTP success, top-level business success, exactly one
item, item `sCode=0`, and matching operation identity. Persisted evidence is
allowlisted and never contains credentials, signatures, account identity, raw
responses, or remote error text.

## Precision and lifecycle verification

The writer uses the attested #445 read client to obtain a live
`InstrumentSpec`. It rejects suspended instruments, non-finite decimals,
contracts below `minSz`, contracts not divisible by `lotSz`, and limit/trigger
prices not divisible by `tickSz`. `ctVal` is retained in normalized evidence so
contracts and underlying units cannot be confused.

## Database writer boundary

The active schema plus the lease and write-attempt journal tables are owned by
the existing `freqtrade_ai_attestor` NOLOGIN/NOINHERIT owner role. The runtime
`freqtrade` role receives schema `USAGE` plus only table `SELECT`, `INSERT`, and
`UPDATE`; it receives no
`DELETE`, `TRUNCATE`, `REFERENCES`, `TRIGGER`, sequence `UPDATE`, table
ownership, or schema `CREATE`. PUBLIC and unexpected grantees receive no
writer DML. Readiness also fails if either protected role has a delegated
member, or if writer table/sequence ownership, ACLs, indexes, foreign keys, or
critical stale-holder/fencing checks differ from the recorded definitions.

A successful lifecycle is based on reconciled exchange state, not a POST
acknowledgement:

- placement query matches target, instrument, IDs, side, net/isolated mode,
  order type, price, size, and reduce-only semantics;
- fills are ingested idempotently by exchange fill ID;
- positions are upserted only from authenticated normalized reads;
- cancel/amend/close finish only after their corresponding read-side state is
  proven;
- any unresolved state remains `RECOVERY_REQUIRED` and blocks another writer.

## Test matrix

- pure state-machine and precision tests;
- recorded write transport tests for every malformed HTTP 200 shape and
  timeout/reconciliation branch;
- limit, market, cancel, amend, TP/SL, and close lifecycle tests;
- two-run crash recovery asserting placement POST count remains one;
- process lock plus DB lease contention and stale-lease recovery;
- SQLite repository/state tests;
- PostgreSQL migration, constraints, unique/partial indexes, transaction
  rollback, and concurrent lease acquisition;
- default-disabled and `OKX_LIVE` zero-DB/zero-network tests;
- secret scan and sanitized persistence assertions.

PostgreSQL writer tests create a uniquely named temporary database and remove
it in `finally`. They snapshot the pre-existing protected roles, memberships,
and the admin database's extensions before the test and require the same
state afterward. If the protected roles are absent or temporary-database
creation is unavailable, the PostgreSQL gate reports `NOT_RUN` instead of
falling back to the business database or claiming full isolation.
