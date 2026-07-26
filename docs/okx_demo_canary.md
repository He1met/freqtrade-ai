# OKX Demo minimal order canary

Issue [#444](https://github.com/He1met/freqtrade-ai/issues/444) owns one
controlled, one-shot order lifecycle against `OKX_DEMO`. The implementation is
safe by default: without explicit authorization it returns `BLOCKED` before
Keychain access, child-process creation, network access, or artifact creation.

## Operator command

```bash
make okx-demo-canary
# BLOCKED: no Keychain read and no network

make okx-demo-canary CANARY_FLAGS=--allow-demo-order
# May proceed only when all Keychain and account gates pass.
```

The command starts a short-lived child with only the four fixed OKX Demo
Keychain values and non-secret target selectors. It does not inherit proxy
variables, custom CA paths, shell exchange credentials, or dotenv values. The
HTTP client is fixed to `https://openapi.okx.com`, disables proxies and
redirects, and adds `x-simulated-trading: 1` to every request.

## Exact lifecycle

1. Acquire the single-writer lock and persist a unique legal `clOrdId` intent.
2. Attest the pinned Demo account, exact `read_only,trade` permissions, Futures
   account mode, and `net_mode`.
3. Require zero pending `BTC-USDT-SWAP` orders and zero isolated net position.
4. Read live instrument metadata and bid price.
5. Derive the minimum legal size and a tick-aligned buy price 5% below bid;
   reject non-live instruments, invalid decimals, or notional above 2,000 USDT.
6. Submit exactly one isolated/net/post-only limit buy.
7. Validate HTTP 200, top-level `code=0`, the single item `sCode=0`, and matching
   `clOrdId`/`ordId`; query the exact order contract.
8. Cancel it, then poll the exact order until `canceled` or `mmp_canceled`.
9. Require zero pending orders, zero fills for that order, and zero position.

A write timeout is an unknown outcome. The command queries the pre-persisted
`clOrdId` first and never blindly repeats a placement. A cancel may be retried
once only after reconciliation proves the order remains cancelable. A cleanup
placement is never blindly repeated.

## Unexpected fill

Any partial/full fill or final residual position is a canary failure. The
command first cancels the remaining original order, persists a cleanup intent,
then submits one opposite-side `reduceOnly` market order for the signed net
position. It queries that cleanup order by its own `clOrdId` and verifies the
position is zero.

- cleanup verified: `FAILED / UNEXPECTED_FILL_CLEANED`;
- cleanup or original-order final state not verified:
  `RECOVERY_REQUIRED / NONTERMINAL_OUTCOME_REQUIRES_RECOVERY`.

Cleanup success never converts the canary to `PASSED`.

## Evidence and recovery

Artifacts live under `.freqtrade-ai/runtime/okx-demo-canary/`. Allowed fields
are status, execution target, artifact ID, instrument, fixed sequence enums,
reason code, and SHA-256 hashes of order identifiers. Raw identifiers,
credentials, account identity, request signatures, response bodies, and remote
error messages are prohibited.

A prior `RESERVED`, `RUNNING`, `UNKNOWN`, or `RECOVERY_REQUIRED` artifact
preempts new-canary creation. A reservation known to precede the write is
closed without network. Otherwise the next explicitly authorized invocation
derives the original deterministic `clOrdId`, queries and cancels only that
order, reconciles fills and positions, and returns without placing a new order.
Only a proven terminal artifact allows a later invocation to create another
canary. Operators must not delete a nonterminal artifact to bypass recovery.
Repeated recovery updates the bounded `recovery_attempt_count` and
`recovery_last_attempt_at` fields. Recovery phase names are idempotent set-like
sequence values, so repeated attempts cannot grow the evidence sequence beyond
the parent process limit.

Offline tests cover success, per-item failures, contract mismatch, write
timeouts, cancel polling, unexpected long/short exposure, reduce-only cleanup,
single-writer history, artifact failure, fixed Demo routing, and redaction.
They prove implementation behavior only. A real Demo run remains `NOT_RUN` or
`BLOCKED` until explicitly authorized and its redacted artifact is inspected.
