# OKX Demo canary（直连入口已永久封禁）

旧的 Issue [#444](https://github.com/He1met/freqtrade-ai/issues/444) 直连
transport 生命周期已退役。`python -m app.adapters.okx_demo.demo_canary`、
`make okx-demo-canary` 以及 `--allow-demo-order` 都只返回
`BLOCKED / DIRECT_CANARY_DISABLED_USE_CANONICAL_RUNTIME_ONE_SHOT_GRANT`，不会
读取 Keychain、创建子进程、写 artifact、访问网络或触碰交易所。此封禁是
fail-closed 边界，不是可通过参数绕过的确认步骤。

> 当前账户为 `long_short_mode`（双向持仓）；任何 canary 都必须先通过当前
> `OKX_DEMO`、账户 fingerprint/权限、ATTESTED/RECOVERED/UNIQUE/READY、完整
> risk-chain、最新空订单/仓位对账、最小风险上限和幂等 journal 等硬门。

## Operator command

```bash
make okx-demo-canary
# BLOCKED: direct transport canary is permanently retired
```

受控 canary 只能由唯一 canonical LaunchAgent/runtime 通过 DB-backed
one-shot submission grant 驱动：`TradeIntent/ApprovedExecution`（明确标记
`CONTROLLED_CANARY_NON_PRODUCTION`）→ grant → writer → `ExchangeOrder` →
回报/成交 → reconciliation → restart idempotency。全局
`order_submission_enabled=false` 必须保持不变；没有完整候选可信链时保持
`BLOCKED`，不得用历史快照、mock 或 HTTP 200 冒充成功。

### Strategy-independent execution-chain canary

为只验收 writer→交易所回报→本地持久化→reconciliation→重启幂等而不伪造
策略晋级，受支持入口为：

1. `POST /api/okx-demo/canary/prepare`（operator token、`Idempotency-Key` 与
   `X-Provider-Authorization: once`）。它只创建带有
   `CONTROLLED_CANARY_NON_PRODUCTION` 的 attestation request，不接受调用方的
   品种、数量或价格覆盖。
2. 唯一 canonical runtime 在同一 coordination lock 下调用已 attested 的
   `capture_trusted_signal_bundle`，实时持久 instrument/market/account 快照。
   backend API 不读取 Keychain，也不自行构造快照。
3. 主任务/受控 operator 在确认 runtime 成功后调用
   `POST /api/okx-demo/canary/finalize`（沿用同一 `Idempotency-Key`）创建无
   `StrategyVersion`、`CandidateApproval`、
   `StrategyDeployment` 或 `SignalEvaluation` 的非生产 lineage；随后只可使用
   #595 DB-backed one-shot grant 和唯一 runtime writer。

该入口仍固定 `OKX_DEMO`、`simulated_trading=true`、`allow_real_funds=false`、
`order_submission_enabled=false`、`BTC-USDT-SWAP` 交易所最小合法数量及不超过
20 USDT 的 notional。grant 原子消费、超时/撤单/必要时 reduce-only 清理、终态
对账、许可撤销和重启不重复下单均必须有真实证据；它不能批准、部署或代表
任何 DeepSeek/社区策略候选。

## Historical lifecycle (offline evidence only)

下列内容仅描述退役实现的离线安全语义，不能作为当前可执行操作、验收或
权限授权依据。

1. Acquire the single-writer lock and persist a unique legal `clOrdId` intent.
2. （历史 net-mode 生命周期）Attest the pinned Demo account, exact
   `read_only,trade` permissions, Futures account mode, and `net_mode`.
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
