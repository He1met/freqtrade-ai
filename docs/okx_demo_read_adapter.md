# OKX_DEMO read adapter

Issue #445 adds a GET-only adapter for normalized OKX Demo evidence. The
offline implementation and tests do not prove authenticated OKX Demo
integration.

## Supported resources

Public resources:

- SWAP instruments;
- ticker, candles, and orderbook;
- mark and index prices;
- funding rate and open interest.

Authenticated read resources:

- account configuration and balances;
- positions and leverage information;
- SWAP trading fees;
- order query by `ordId` or `clOrdId`.

There are no place-order, cancel-order, amend-order, leverage-write, transfer,
or withdrawal methods.

## Stable evidence boundary

Every successful result uses schema version `1` and includes:

- `execution_target=OKX_DEMO`;
- `source=okx_demo_rest`;
- resource name;
- fetch and exchange timestamps;
- expiry time and stale flag;
- whether the request required Demo authorization;
- normalized records validated by a resource-specific Pydantic model.

Expired evidence returns `BLOCKED/STALE_DATA` and cannot be represented as
`READY`. Missing authorization also returns `BLOCKED`. HTTP, timeout, network,
rate-limit, business, and invalid-response failures are mapped to structured
errors without copying response bodies, credentials, or signatures.

Instrument conversion uses returned `ctVal`, `ctValCcy`, `lotSz`, and `minSz`.
It rounds requested underlying units down to a valid lot and rejects quantities
below the exchange minimum. No contract size is hard-coded.

## Credential provider boundary

The adapter depends on the narrow `OkxDemoCredentialProvider` protocol.
Authenticated methods are blocked until #443 supplies the Keychain-backed
implementation. Authorization headers are passed directly to the injected
transport and never enter snapshots or errors.

The provider must return exactly the four OKX authorization headers, using
their canonical case, with non-empty values and no control characters. Any
extra header, alternate casing, provider-supplied Demo header, or invalid value
is blocked before transport. The adapter is the sole owner of
`x-simulated-trading: 1`.

Exact instrument and order queries also reconcile response identity before
normalization. A mismatched `instId`, `ordId`, or `clOrdId` is
`FAILED/INVALID_RESPONSE`, never `READY`.

## Current acceptance state

The offline adapter, constructed response coverage, normalization, redaction,
expiry, and error mapping can be accepted now. Real Demo reads remain
`NOT_RUN/BLOCKED` until #443 is complete and the #444 authenticated canary
confirms Demo identity and permissions. Issue #445 must not be closed based
only on the offline tests.
