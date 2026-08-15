# Canonical V1.3 fresh public market rollout

This runbook is the no-trade production path for one fresh
`BTC-USDT-SWAP / 15m` candle artifact. It does not read or register any legacy
market directory. The previously inventoried 715 legacy files are exclusion
evidence only.

## Frozen authority and blockers

- Source: only `GET https://www.okx.com/api/v5/market/history-candles`.
- Capability: `PUBLIC_MARKET_DATA_ONLY`; credential, account, private API,
  exchange writer, signal, and order capabilities are `NONE`.
- Target identity comes from an exact validated `TARGET` snapshot UUID+digest.
- Range, minimum rows, warmup, integrity margin, and freshness age come from an
  exact validated `WINDOW` snapshot UUID+digest. No CLI fallback exists.
- The original P0 WINDOW v1 intentionally lacks the three acquisition policy
  fields. It must not be edited or replayed under its v1 idempotency keys.
  Create a new audited WINDOW version through the canonical control API with
  explicit `warmup_closed_candles`, `integrity_margin_closed_candles`, and
  `freshness_max_age_seconds`. Create a new `RESEARCH_AGGREGATE` version bound
  to that WINDOW version and the other five exact P0 versions. Until both are
  validated, acquisition is `BLOCKED_WINDOW_ACQUISITION_MARGIN_UNSET`.

Recommended first explicit policy is 400 warmup candles, 8 integrity-margin
candles, and 3600 seconds maximum freshness. Select an `end_at` aligned to the
latest fully closed 15-minute candle when the new WINDOW draft is reviewed.
The required 30-day window remains 2,880 candles; the downloader extends the
start by exactly 408 candles from the frozen payload.

Prepare and review that immutable update with the dedicated no-trade command.
The end bound has no clock-derived default and must be supplied explicitly:

```bash
backend/.venv/bin/python scripts/canonical_v13_market_window_rollout.py plan \
  --end-at 2026-08-14T10:45:00Z
```

After verifying that this is a fully closed 15-minute candle, use `apply` with
the exact same argument. The command reads the canonical configuration catalog,
requires one exact production profile per P0 kind and a validated latest
version, then creates only a new WINDOW version and a new dependent
RESEARCH_AGGREGATE version through the audited control API. Content-bound
idempotency keys make an exact replay a no-op. An unreviewed latest draft,
duplicate profile authority, missing snapshot, changed request, non-no-trade
health response, or unexpected bundle readiness blocks before continuing.
Until a fresh market snapshot is separately accepted, the final preview must
remain `BLOCKED / MARKET_SNAPSHOT_UNSET` with no bundle or activation row.

## Preconditions

1. Release checkout is clean and exactly equals `origin/main`.
2. The dedicated database is `freqtrade_ai_v13`; never use `freqtrade_ai` or
   `freqtrade_ai_design_lab`.
3. The canonical API is `HEALTHY/READY/TRADING_DISABLED`. The client never reads
   Keychain or receives a DSN; the already-running API keeps its request-scoped
   control connection private.
4. The server-configured artifact root is an absolute canonical data directory. It and every
   existing parent below it must be real directories, not symlinks.
5. Record exact TARGET/WINDOW snapshot UUIDs and digests from the reader
   projection. Review the new WINDOW payload and its audit/idempotency receipts.

## Dry review and execution

The command has no implicit target or range defaults. First request and review
the complete server-derived plan and its digest:

```bash
backend/.venv/bin/python scripts/canonical_v13_fresh_market.py plan \
  --target-snapshot-id TARGET_UUID \
  --target-snapshot-digest TARGET_SHA256 \
  --window-snapshot-id WINDOW_UUID \
  --window-snapshot-digest WINDOW_SHA256 \
  --target-key btc-usdt-swap-15m
```

Then bind the reviewed digest on apply:

```bash
backend/.venv/bin/python scripts/canonical_v13_fresh_market.py apply \
  --target-snapshot-id TARGET_UUID \
  --target-snapshot-digest TARGET_SHA256 \
  --window-snapshot-id WINDOW_UUID \
  --window-snapshot-digest WINDOW_SHA256 \
  --target-key btc-usdt-swap-15m \
  --expected-plan-digest PLAN_SHA256 \
  --profile-key production-v13-okx-public-btc-usdt-swap-15m \
  --scope-key production-research-v13
```

The short-lived client calls only the loopback canonical API and has no
credential/DSN surface. The API paginates with a fixed timeout, finite retry count, and an
IP-rate-limit interval. It accepts only confirmed candles, normalizes UTC,
deduplicates exact repeats, rejects conflicting duplicates and any interval
gap, writes deterministic NDJSON mode 0600 under a SHA-256-bearing
root-relative locator, then appends artifact, inspection, receipt, and sealed
snapshot rows in one database transaction. An existing identical file and
exact database lineage are an idempotent replay; differing bytes are blocked.

## Binding and acceptance

After the command returns `ACCEPTED`, use the canonical API to preview a bundle
with all seven exact validated P0 snapshots (including the revised WINDOW and
RESEARCH_AGGREGATE) plus the returned market snapshot. Review the prospective
bundle ID and digest, then call the audited activation endpoint with that exact
path ID/digest. Do not activate if preview is not `READY`.

Acceptance requires:

- market inventory and snapshot projections show the exact artifact, accepted
  receipt, target, coverage, and digest;
- persisted activation readiness is `READY` at the configured freshness age;
- repeat acquisition returns both replay flags true and does not add rows;
- API/UI restart preserves the same IDs and digests;
- validation attempts, scores, qualifications, optimization, deployments,
  runtime, signals, orders, fills, ledger, and reconciliation remain zero.

Only after these gates may a separate, explicitly authorized first-backtest
phase begin. This runbook never starts research or trading.
