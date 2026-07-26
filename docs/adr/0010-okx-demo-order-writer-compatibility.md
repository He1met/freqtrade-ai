# ADR-0010: OKX Demo SWAP compatibility and single order writer

- Status: Accepted; controlled canary implemented, authenticated run remains separately gated
- Date: 2026-07-27
- Issue: [#444](https://github.com/He1met/freqtrade-ai/issues/444)
- Prerequisite: [#443](https://github.com/He1met/freqtrade-ai/issues/443) Keychain boundary

## Decision

Use a project-owned OKX REST/WebSocket adapter as the sole order writer for
`OKX_DEMO`. Freqtrade remains the strategy, backtest, and signal engine. Agent
Trade Kit can be used for read-only investigation, but it must not write orders
while the project adapter is active.

The controlled Demo canary is implemented behind a one-shot explicit
`--allow-demo-order` gate and the #443 Keychain-only credential boundary.
Implementation and offline tests do not prove a real Demo order lifecycle:
formal acceptance stays `BLOCKED` until an authorized run records redacted
place/query/cancel/final-zero evidence.

## Frozen versions and evidence

Observed on 2026-07-27:

| Component | Version | Relevant behavior | Result |
| --- | --- | --- | --- |
| Freqtrade | `2026.5` | OKX supports futures + isolated and declares `net_only`; its OKX adapter does not declare `supports_demo_trading` | Do not set Freqtrade `exchange.demo_trading=true` for OKX |
| CCXT | `4.5.56` | `okx.set_sandbox_mode(True)` adds `x-simulated-trading: 1`; generic sandbox guidance requires enabling it immediately after construction | Technically capable, but not reached through Freqtrade 2026.5's Demo path |
| OKX Agent Trade Kit CLI/MCP | npm stable `1.2.8` | `--demo`, `--json`, read-only mode, SWAP tools, timeout, and non-zero exit on item `sCode` failure | Read-only allowed; write path not selected |
| OKX API v5 | current docs on 2026-07-27 | Demo REST is `https://openapi.okx.com` with `x-simulated-trading: 1`; Demo WS is `wss://wspap.okx.com:8443/ws/v5/...` | Required contract |

Version commands:

```bash
/Users/shenjianpeng/freqtrade_venv/bin/freqtrade --version
/Users/shenjianpeng/freqtrade_venv/bin/python -c 'import ccxt; print(ccxt.__version__)'
npm view okx-trade-cli version dist-tags --json
npm view okx-trade-mcp version dist-tags --json
```

Primary evidence:

- [OKX API v5: Demo Trading Services](https://www.okx.com/docs-v5/en/#overview-demo-trading-services)
- [OKX API v5: REST authentication](https://www.okx.com/docs-v5/en/#overview-rest-authentication)
- [OKX Agent Trade Kit repository](https://github.com/okx/agent-trade-kit)
- [OKX Agent Trade Kit 1.2.8 release](https://github.com/okx/agent-trade-kit/releases/tag/v1.2.8)
- [CCXT sandbox mode manual](https://github.com/ccxt/ccxt/wiki/manual#testnets-and-sandbox-environments)
- [Freqtrade exchanges: OKX futures/isolated](https://www.freqtrade.io/en/stable/exchanges/#okx)
- [Freqtrade FAQ sandbox boundary](https://www.freqtrade.io/en/stable/faq/#can-i-use-a-sandbox-account)

## Compatibility matrix

| Capability | OKX API | Agent Kit 1.2.8 | CCXT 4.5.56 | Freqtrade 2026.5 | Project decision |
| --- | --- | --- | --- | --- | --- |
| Demo REST | Shared REST host + mandatory Demo header | Yes via `--demo` | Yes via `set_sandbox_mode(True)` | OKX Demo rejected by validation | Project adapter |
| Demo public/private/business WS | Dedicated `wspap.okx.com` URLs | REST-oriented trade toolkit; not the bot market-data WS owner | CCXT Pro has OKX WS support | OKX WS enabled, but no supported OKX Demo activation | Project adapter owns explicit Demo URLs |
| `SWAP` | Supported | `swap_*` tools | `swap=true` | Futures mapping supported | Supported |
| `isolated` | `tdMode=isolated` | Exposed on contract orders | Supported | Explicit supported pair | Required |
| one-way position | `posMode=net_mode` | Account mode tool supports `net_mode` | `posSide=net` semantics | OKX adapter declares `net_only` | Required initially |
| JSON | JSON API | `--json` raw output | Python objects | CLI/log abstractions | Persist normalized, redacted JSON evidence |
| HTTP exit | HTTP can succeed while business item fails | CLI sets exit 1 on failed item `sCode` | Raises mapped errors | Maps CCXT errors | Check HTTP, top-level `code`, and every `sCode` |
| Timeout/retry | Rate limits and transient codes exist | Configurable timeout and retry hints | Network exceptions/retry support | Retrier wrappers | Write timeout means unknown outcome; query by `clOrdId` before any resubmit |

## Response and retry contract

A write is successful only when all conditions are true:

1. transport and HTTP status succeeded;
2. payload is valid JSON;
3. top-level `code == "0"`;
4. `data` is non-empty;
5. every result item has `sCode == "0"`;
6. the request used a predetermined OKX-legal `clOrdId` (1-32
   case-sensitive alphanumeric characters);
7. every result contains a non-empty `ordId` and the same `clOrdId` as the
   request.

HTTP 200 with any non-zero `sCode` is `FAILED`, never success.

Read-only operations may retry network timeouts, HTTP 429/5xx, or documented
transient OKX codes with bounded exponential backoff and jitter. A write timeout
or transient response is an unknown outcome: query by the deterministic
`clOrdId` first. Never blindly repeat `place-order`.
If the caller did not persist a legal deterministic `clOrdId` before the
write, the retry decision is `BLOCKED_MISSING_CLORDID`; it must not claim that
reconciliation is possible.

## Implemented canary sequence

The canary uses the smallest valid `BTC-USDT-SWAP` contract size,
derived from the current instrument response (`ctVal`, `lotSz`, `minSz`,
`tickSz`) rather than hard-coded.

1. Assert `ExecutionTarget=OKX_DEMO`, `allow_real_funds=false`, and exactly one writer.
2. Assert the Keychain-injected credentials are attested Demo credentials with no withdrawal permission.
3. Query account configuration and require `posMode=net_mode`.
4. Query the instrument and derive the valid contract count and price precision.
5. Submit a far-from-market, post-only isolated limit order with a unique deterministic `clOrdId`.
6. Validate top-level `code`, every `sCode`, `ordId`, and `clOrdId`.
7. Query the order, cancel it, query it again, then reconcile orders, fills, and positions.
8. Require no open order, no fill, and no residual position. If it unexpectedly fills, use the reduce-only cleanup path and report the canary `FAILED`.
9. Persist only hashed identifiers and an artifact ID.

## Prohibited combinations

- Freqtrade `exchange.demo_trading=true` with OKX on Freqtrade 2026.5.
- Freqtrade, Agent Kit, and the project adapter sharing write authority.
- Production WS URLs for a Demo private/account/order stream.
- A private REST request without `x-simulated-trading: 1`.
- `OKX_LIVE`, `--live`, `--no-demo`, real-fund fallback, or automatic Demo-to-Live switching.
- Cross margin, hedge mode, SPOT, dated FUTURES, or an unapproved instrument in this phase.
- Retrying a timed-out order without first reconciling its `clOrdId`.
- Treating process exit code 0, HTTP 200, top-level `code=0`, or a rendered UI state alone as proof of a successful order.

## Reproduce

Offline (default, expected exit `2` while the canary is blocked):

```bash
python3 scripts/okx_demo_compatibility.py
echo $?
```

On a clean machine, a missing command or a non-zero version command (including
`import ccxt` failing because CCXT is absent) is reported as `NOT_INSTALLED`.
That is a missing prerequisite, so the default diagnostic remains
`BLOCKED` with exit `2`, matching this reproduction contract.

Optional credential-free public REST probe:

```bash
python3 scripts/okx_demo_compatibility.py --probe-public-rest
echo $?
```

Focused tests:

```bash
cd backend
.venv/bin/pytest -q tests/test_okx_demo_compatibility.py
```

Exit codes:

- `0`: all requested checks passed;
- `1`: a compatibility or probe check failed;
- `2`: prerequisites blocked an authenticated Demo canary.

The diagnostic never makes authenticated calls and has no order-writing code.
It is safe to run on a clean machine and reports only credential presence.

## Go / No-Go

- **GO**: implement the project-owned OKX Demo REST/WS adapter as the only writer.
- **GO**: keep Freqtrade for strategy/backtest/signal generation.
- **GO**: permit Agent Kit only in read-only diagnosis unless the project adapter is stopped and a separate future approval explicitly changes ownership.
- **GO**: run the one-shot canary only with explicit operator authorization after all Keychain and account-attestation gates pass.
- **NO-GO**: close #444 as fully accepted until the canary records redacted `ordId`, `clOrdId`, query, cancel, and zero-residual evidence.
