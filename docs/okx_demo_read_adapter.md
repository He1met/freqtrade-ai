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
The only public production entry point, `create_attested_okx_demo_read_adapter`,
freezes the allowlisted environment and first runs #443's authenticated
`/account/config` preflight against that same credential snapshot. It returns a
real-network adapter only after the account fingerprint, exact permissions, Futures
account level, and `net_mode` all match. A stale pin paired with another Demo
key therefore blocks before any target private read. The session signs only
`GET` with an empty body and cannot select another target or REST origin.

Ordinary `OkxDemoReadAdapter` construction is an offline normalizer that accepts
only already-built response values; it has no transport injection parameter and
cannot compose or wrap `UrllibOkxReadTransport`. The production factory returns
the read-only `OkxDemoReadClient` protocol while its real transport and
attestation session remain closure-local. The session expires after 60 seconds
and revalidates every
`account_config` response against the original fingerprint and account-safety
contract. Identity drift permanently revokes the session, so every later
private read blocks before transport. Identity drift, expiry, factory close,
and write failure persist a signed revoke reason through the attestor-owned
database function; failure to persist that revoke permanently blocks the
writer. Other private reads are allowed only inside that TTL.

Observation timestamps may be at most five seconds ahead of the local
timezone-aware clock. Funding settlement timestamps are treated as schedules,
not observation freshness: they must stay within 24 hours of now and remain
ordered, while snapshot freshness anchors to `received_at`.

The factory is used only inside the short-lived OKX adapter child. It does not
read Keychain, dotenv, or inherited shell credentials itself; the controlled
startup boundary supplies a 32-byte proof key solely for signing the canonical
session payload, and the factory drops that key from its retained credential
environment immediately. Authorization
headers are passed directly to the injected transport and never enter snapshots
or errors.

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
expiry, and error mapping can be accepted now. #443 provides the temporary
credential-bearing process and GET-only signer boundary; it does not by itself
prove a real authenticated read. Real Demo reads remain `NOT_RUN/BLOCKED` until
the #444 authenticated canary confirms Demo identity and permissions. Issue
#445 must not be closed based only on the offline tests.
