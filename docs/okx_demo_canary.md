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
   `X-Provider-Authorization` header，single-use consent）。它只创建带有
   `CONTROLLED_CANARY_NON_PRODUCTION` 的 attestation request，不接受调用方的
   品种、数量或价格覆盖。
2. 唯一 canonical runtime 在同一 coordination lock 下调用已 attested 的
   `capture_execution_attestation`，实时持久 instrument/market/account 快照。
   该 execution-only bundle 不读取 candles、不生成 signal evaluation，且仍
   复用 read adapter 的 fingerprint、expiry、source 与原子快照持久化边界。
   backend API 不读取 Keychain，也不自行构造快照。
3. 主任务/受控 operator 在确认 runtime 成功后调用
   `POST /api/okx-demo/canary/finalize`（沿用同一 `Idempotency-Key`）创建无
   `StrategyVersion`、`CandidateApproval`、
   `StrategyDeployment` 或 `SignalEvaluation` 的非生产 lineage；随后只可使用
   #595 DB-backed one-shot grant 和唯一 runtime writer。

如果旧的 #603 signal-bundle 请求及其一次性 retry 已经以
`BLOCKED/CANARY_SNAPSHOT_BLOCKED` 终止，原 `/canary/prepare` 会继续保持
fail-closed。经 operator 单次授权后，可用全新的 `Idempotency-Key` 调用
`POST /api/okx-demo/canary/prepare-execution-only`。该入口只接受明确的
`INVALID_SIGNAL_BUNDLE`/非 retryable 终态（以及保存异常类型的旧记录），把只读的
旧 `ResearchJob` IDs 写入新请求的 `supersedes_job_ids` lineage，并要求新的
`EXECUTION_ONLY` runtime attestation；旧记录、pending 请求、active grant、writer
attempt 或既有 `TradeIntent` 仍会直接阻断。相同 key 可安全重放，第二个 fresh key
或已存在的 fresh entry 不会创建第二个 runtime handoff。

如果 runtime 的 attestation read 以可重试的 `OkxReadAdapterError` 终止，原始
`ResearchJob` 会保持 `BLOCKED`，只保存脱敏异常类型与 retryability。operator
可用新的 `Idempotency-Key` 调用 `POST /api/okx-demo/canary/retry` 创建最多一个
successor handoff；该入口不重置或删除原记录、不创建 grant，也不允许第二个
pending request。`UNAUTHORIZED`、身份漂移、无效响应等明确终态不会被重试，
retry successor 失败后也不会继续链式重试。

如果 `prepare-execution-only` 已经返回
`SUCCESS/CANARY_SNAPSHOTS_READY`，但短 TTL 快照在 finalization 前过期，不能
修改原 job、延长 `expires_at` 或借用旧快照。operator 必须使用新的
`Idempotency-Key` 调用 `POST /api/okx-demo/canary/refresh-execution-only`；该
入口只接受唯一、无 TradeIntent/ApprovedExecution/grant/writer attempt/order 的
`FRESH_EXECUTION_ONLY` 成功 handoff，并创建带有
`entry_kind=FRESH_EXECUTION_ONLY_REFRESH`、`refresh_of_job_id` 和继承加源 job
的 `supersedes_job_ids` 的新 ResearchJob。唯一 canonical runtime 再捕获
`EXECUTION_ONLY`（不读 candles/strategy/signal）快照；operator 用同一 refresh
key 重放该入口以完成 finalize。原 job 的 status、request_hash、request_payload
和 evidence 永远不变；pending/未知/重复 refresh、非 Demo manifest、active
 grant、任何 writer 或订单状态均 fail-closed。

refresh 在创建 successor 或 finalize 时若只是暂时拿不到 canonical runtime 的
transaction-scoped advisory lock，会返回非终态
`WAITING_FOR_RUNTIME_ATTESTATION`，且不会把该结果缓存为同一
`Idempotency-Key` 的终态错误；待 reconciliation 释放锁后可继续使用原 key 重试。
创建阶段的该等待不代表已创建新的 ResearchJob；finalize 阶段只引用既有 successor，
也不延长任何快照 TTL。stale/invalid/unknown 证据、非 Demo 配置、active
grant/writer/order 等真实安全失败仍返回并缓存
`409/BLOCKED`，其他 operator 入口的错误缓存语义不变。

若该 refresh successor 自身也在 finalization 前过期，允许且仅允许一次额外的
有界重试 successor：新的 request 使用
`entry_kind=FRESH_EXECUTION_ONLY_REFRESH_RETRY`，`refresh_of_job_id` 指向过期
refresh job，并继承完整 `supersedes_job_ids` lineage。只有原 refresh evidence
明确包含 instrument/market/account 的过期时间才可创建该 successor；缺失或仍然
新鲜的 expiry 证据直接 fail-closed。达到两个 refresh successors 后不再创建新
job，所有旧 job 与 evidence 保持不可变，避免无限 refresh 链。

如果该有界链正好是在 #616 修复前因 PostgreSQL runtime-role 只读 lineage
锁权限失败、且最终 `FRESH_EXECUTION_ONLY_REFRESH_RETRY` handoff 也在
finalize 前过期，operator 只能使用一次全新的 `Idempotency-Key` 调用
`POST /api/okx-demo/canary/recover-execution-only`。该入口不是第三次普通
refresh：它要求历史中恰好一个 `FRESH_EXECUTION_ONLY` source、恰好两个按
depth=1/2 排列且均已成功的 refresh successor，最终 successor 必须保存明确
的 instrument/market/account 过期时间，并且旧 terminal history 的 ID 必须与
source 的 `supersedes_job_ids` 完全一致。它创建唯一
`entry_kind=FRESH_EXECUTION_ONLY_RECOVERY` successor，记录
`recovery_of_job_id`、完整累计 lineage 和固定
`recovery_boundary=PRE_616_FINALIZE_ACL_FAILURE`；jobs 15--19 的
status、request、hash 和 evidence 永远不变。

任一 pending/unknown history、第二个 recovery key、非 Demo manifest、active
grant、writer attempt、订单或持仓都会 fail-closed。recovery successor 仍须
由唯一 runtime 重新捕获全新的 execution-only attestation，再沿用原有
`finalize`、one-shot grant、writer、交易所终态和 reconciliation 门；不接受
旧快照、不会打开全局 submission，也不会形成无界 recovery 链。

schema `20260802_26` 将 canary 的三条敏感 lineage 写入收敛为唯一
`create_okx_demo_canary_lineage(jsonb)` 原子函数。函数固定由 NOLOGIN
`freqtrade_ai_attestor` 持有，使用 `SECURITY DEFINER` 与
`search_path=pg_catalog`；runtime role `freqtrade` 只有该函数的 `EXECUTE`，对
`trade_intents`、`risk_decisions`、`approved_executions` 及其 sequence 都没有
INSERT/UPDATE/DELETE/USAGE 权限。函数在同一事务内重验 Demo target、非生产
provenance、完整 hash、空 durable boundary、fresh reconciliation、full-chain
绑定、attested snapshots、TTL 与固定风险上限，并尝试取得与 service/runtime 相同
的 one-shot transaction advisory lock；任一不匹配或锁占用都整笔回滚。

如果唯一 `FRESH_EXECUTION_ONLY_RECOVERY` handoff 已经成功保存新快照，但其
lineage 原子写在提交前失败并回滚，且这批快照随后明确过期，只允许使用新的
`Idempotency-Key` 调用一次
`POST /api/okx-demo/canary/recover-post-persistence`。该入口按 immutable
shape/ancestry 识别源 job（不依赖固定数据库 ID），创建固定
`entry_kind=FRESH_EXECUTION_ONLY_POST_PERSISTENCE_RECOVERY`、
`recovery_boundary=POST_PERSISTENCE_LINEAGE_WRITE_FAILURE` 的唯一 successor，
再由 canonical runtime 捕获全新 attestation。第二个 successor、任何既有
intent/approval/grant/writer attempt/order/非零 position、未知 history 或复用旧
快照全部 fail-closed；jobs 15--20 保持不可变。

该入口仍固定 `OKX_DEMO`、`simulated_trading=true`、`allow_real_funds=false`、
`order_submission_enabled=false`、`BTC-USDT-SWAP` 交易所最小合法数量及不超过
20 USDT 的 notional。grant 原子消费、超时/撤单/必要时 reduce-only 清理、终态
对账、许可撤销和重启不重复下单均必须有真实证据；它不能批准、部署或代表
任何 DeepSeek/社区策略候选。

runtime 在成功持久化任一待处理的 execution-only attestation 后，会先提交并
释放 one-shot coordination advisory lock，再跳过该轮长的 observe/network cycle。
下一轮仍由同一个 runtime/writer 恢复常规 reconciliation；这只缩短 operator
finalize 的锁等待，不改变快照 freshness、OKX_DEMO、全局 submission=false 或
策略风险门。

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
