# Canonical V1.3 production no-trade research

本 runbook 描述可调用但默认 fail-closed 的 production research slice。它只使用 48-table
canonical database，不连接 legacy/v47，不访问 Keychain/OKX，不启动 long-lived runtime，也不产生
signal、trade intent、order、fill 或 real-funds 行为。

`research_gate_attempts` 与 append-only `research_gate_receipts` 是 production static/lookahead
唯一权威。gate attempt 创建时绑定当时已 ACCEPTED 且 fresh 的 bundle/market snapshot，以及
TARGET/WINDOW snapshot、market profile、strategy artifact、release/image/worker source digests。
freshness 只限制新 attempt 的创建；完成 receipt 永久绑定 frozen lineage，active pointer 更新或
时间流逝不得使其失效或替换 lineage。需要新数据时必须创建新 gate attempt。
每次创建还必须携带 batch/version-scoped `idempotency_key`；同 key 同请求返回同一 attempt，
同 key 异请求 fail closed。lease 过期的 orphan 原子转为 `BLOCKED/GATE_LEASE_EXPIRED`，如需重验
必须使用新 key 创建新 attempt，不得覆盖或搬运旧 receipt。

`validation_plans` 必须显式引用同一个 PASSED attempt 的 STATIC 与 LOOKAHEAD v3 receipt IDs，
并保持相同 frozen lineage。旧 v1/v2 或评论证据不 backfill、不复制、不迁移为 v3。只有 API
投影可声明 `validation_eligible`；UI 不自行推导 PASS/BLOCKED。

Existing 46-table installations first run the additive owner-only upgrade:

```bash
python backend/scripts/canonical_v13_bootstrap.py gate-receipts-apply
python backend/scripts/canonical_v13_bootstrap.py gate-receipts-verify
```

该升级只增加两表、两个 validation-plan FK column、两个 FK index 与三个约束 trigger；guard
function 归 schema owner 且 PUBLIC/service roles 无 EXECUTE。升级不创建或回填任何 gate、plan、
backtest、score 或 qualification 行。

## 1. 两类执行器必须分开

- `SimulatedResearchExecutor` 只用于 `ISOLATED_TEST` fixture；其 metrics 是调用方显式测试输入，
  绝不是 production backtest evidence。
- `FreqtradeProductionResearchAdapter` 只接受 `PRODUCTION_RESEARCH` one-shot receipt。它通过 pinned
  OCI image 调用 Freqtrade worker；容器固定 `--rm --network none --read-only --cap-drop ALL`，无
  credential/exchange/order/database writer capability，并限制 CPU、memory、PID、tmpfs、timeout 和
  combined output。
- container 使用外层 launchd service 当前的非 root UID/GID，以只读方式访问 exact inputs；若
  orchestrator 本身为 root 则在启动 OCI runtime 前 `BLOCKED_SANDBOX_ROOT_IDENTITY`。
- strategy artifact、frozen bundle、market snapshot files、validation plan 都在外部 orchestrator
  中按 canonical digest 验证，再以 read-only mount 交给 sandbox。sandbox environment 不继承 host
  environment，因此拿不到 DSN、API key、Keychain 或 exchange credential。
- sandbox 只返回 exact JSON envelope。外部 orchestrator 关闭 sandbox 后，分别使用 validation、
  scoring、qualification writer 的独立事务持久化 receipt；scoring 入口不 import/call qualification，
  PostgreSQL ACL 也不能写 qualification tables。

## 2. control-plane activation 不等于 worker execution

control/API 只提供这些 canonical endpoints：

```text
POST /api/canonical-v13/research/gates/attempts
POST /api/canonical-v13/research/gates/attempts/{id}/claim
POST /api/canonical-v13/research/gates/attempts/{id}/static-receipts
POST /api/canonical-v13/research/gates/attempts/{id}/lookahead-receipts
GET  /api/canonical-v13/research/gates
GET  /api/canonical-v13/research/gates/{id}
POST /api/canonical-v13/research/validation-plans
POST /api/canonical-v13/research/authorizations
POST /api/canonical-v13/research/authorizations/{id}/consume
POST /api/canonical-v13/research/authorizations/{id}/revoke
POST /api/canonical-v13/research/attempts
GET  /api/canonical-v13/research/validation-plans/{id}
POST /api/canonical-v13/research/scores
POST /api/canonical-v13/research/qualifications
```

这些 endpoint 不启动 Freqtrade。CLI 的 `plan/authorize/consume/revoke/start/status/score/qualify`
只是 loopback API client；API base 必须显式为 `http://127.0.0.1:<port>` 或 localhost。command body
从 operator-controlled absolute JSON file 读取，不接受 DSN 参数，也不输出 DSN。

```bash
cd backend
FREQTRADE_AI_CANONICAL_V13_API_BASE_URL=http://127.0.0.1:8011 \
  python scripts/canonical_v13_research.py status --id <validation-plan-uuid>
```

`worker-execute` 是另一个显式动作，只消费已启动 RUNNING attempt 的 exact consumption receipt；
它不会解析 active pointer、选择 latest plan、重用授权或启动 service。

`gate` 是与 `worker-execute` 分离的前置动作。它只通过 canonical reader 读取调用方给出的 exact
strategy/target/bundle/market lineage，先执行不加载策略代码的 static AST validation；static PASS 后，
才在同一类 network-none、read-only、nonroot OCI sandbox 中对 frozen required-window set 执行
Freqtrade `lookahead-analysis`。它通过 audited loopback API 创建 planless gate attempt 并原子提交
typed receipt，但不创建 validation plan/attempt/authorization，也没有任何 writer DSN；validation
writer credential 只存在于 API service。每个 required window 必须在输出中以 exact
window key/digest 出现一次，聚合 signal count、bias 和 status 必须与逐 window evidence 一致；缺失、
重复或漂移均 fail closed。

public market apply 同时冻结 exact target 的 OKX exchange metadata artifact。它只调用 allowlist
中的 `/api/v5/public/instruments` 与 `/api/v5/public/position-tiers`，不读取 credential；响应按 pinned
Freqtrade `2026.6` / CCXT `4.5.61` 合同规范化、以 SHA-256 定址，并通过 immutable
artifact/inspection/receipt 绑定 validated market profile、TARGET/WINDOW snapshots、market snapshot
与 configuration bundle。reviewed plan digest 包含 metadata contract 与 adapter identity。

one-shot worker 只读接收该 artifact，并继续以 `--network none` 运行。它使用 Freqtrade 明确支持的
`validate=False` offline adapter path 创建 OKX exchange，再注入已验证的 market 与 leverage tiers。
worker 不接收 DB URL、OKX credential、socket 或 network capability。metadata 缺失、过期、混入其他
target、digest 漂移或 tier 不完整时一律 `BLOCKED`，不得推断或合成数值。

Lookahead worker output v3 对 `BLOCKED` 强制携带一个 allowlisted failure code。Pinned Freqtrade
日志只在 `/work` tmpfs 内有界捕获；不会输出或持久化原始日志。若 baseline trades 少于 Freqtrade
要求的 `minimum_trade_amount=10`，receipt 精确记录 `LOOKAHEAD_INSUFFICIENT_TRADES`、实际
trade count 与 required count，继续保持 `has_bias=null` / signals `0`；其中 signals `0` 是 unset
placeholder，不得解释为真实零信号。该结果既不是 bias PASS，
也不是策略失败。其他 process/export/ambiguous/internal 情况使用各自稳定 reason code，禁止统一压成
无原因的 BLOCKED envelope。

authorization command 只接受 `ttl_seconds`（1..900），`authorized_at`、`expires_at`、consume/revoke
时间全部由 loopback API 的 UTC clock 生成，caller 不能回填时间来绕过过期门。
start 与 worker 都会先从 immutable `audit_events` 重建 exact authorization/consumption receipt；
只提供一个自洽但未持久化的 digest envelope 不能启动或执行 attempt。

## 3. formal 顺序

每个 strategy version × research target 必须串行执行：

1. 外层 static AST validator 验证 canonical artifact，失败立即停止；
2. pinned Freqtrade lookahead analyzer 产生 exact digest receipt，`has_bias=true/UNKNOWN` 都不能创建
   READY plan；
3. validation writer 从 frozen WINDOW snapshot 创建 exact plan，required windows 不能为空；
4. control writer 为预先生成的 exact `attempt_id` 创建 `PRODUCTION_RESEARCH` one-shot authorization；
5. control writer consume 一次；validation writer 以同一 attempt ID 启动；
6. one-shot sandbox 对 exact required-window set backtest；optional window 不可替代 required window；
7. validation writer 原子持久化 terminal attempt/window receipts；
8. scoring writer 只写一个 `target_scores` receipt；
9. qualifier writer先逐 required window 执行 hard gates，再读取 minimum score。任何 hard gate 失败
   都是 `REJECTED/REQUIRED_WINDOW_GATE_FAILED`，高分不能覆盖；0 个 QUALIFIED 是有效结果；
10. 只有 persisted `QUALIFIED` decision 才能作为 optimization baseline。

Lookahead 结果语义只有一个权威：positive observations 且 `has_bias=false` 才是 PASS；
`has_bias=true` 才是 bias FAILED。Freqtrade 空 export、observations 不足、结果不唯一、工具非零退出或
worker exception 都是 `LOOKAHEAD_BLOCKED` 与 `validation_eligible=false`，不得进入 validation plan
或 backtest。BLOCKED envelope 只允许固定 `failure_stage/failure_code/tool_return_code`、stdout/stderr
SHA-256 digest 和 allowlisted 脱敏 detail；不得包含源码、路径、UUID、credential 或原始日志。

worker、score、qualification 任一步缺失或 receipt/digest/lineage 漂移均保持 `BLOCKED/UNKNOWN`；不得由
URL、UI 或 caller 本地推断。UI 只读取 canonical chain projection，显示 plan/attempt/score/decision
的服务端状态和脱敏 digest。

## 4. dynamic targets 与 cap=6

target set 和 per-target cap 只能来自 frozen TARGET/GENERATION snapshots。batch planner 要求候选映射
与 target set 精确等集；同一 strategy version 不得跨 target 重复。每个 target 独立按其 persisted
`candidate_cap` 串行 chunk，不存在代码默认 target 或默认总数。

当前单 target release profile 的 cap 必须显式为 6。未来对 31 个已入库 strategy versions 的只读
规划结果应精确为 `6,6,6,6,6,1` 六批；本 release/任务不执行这些 production backtests。

## 5. worker 显式环境

`worker-execute` 的 reader/validation/scoring/qualification PostgreSQL DSN 必须分别使用
`freqtrade_ai_v13_api_login`、`freqtrade_ai_v13_validation_login`、
`freqtrade_ai_v13_scoring_login`、`freqtrade_ai_v13_qualification_login`，不得把 NOLOGIN capability
role 当成连接身份。除此之外还要求每项显式设置：

```text
FREQTRADE_AI_CANONICAL_V13_RESEARCH_EXECUTION_ENABLED=PRODUCTION_RESEARCH_NO_TRADE_V1
FREQTRADE_AI_CANONICAL_V13_RESEARCH_OCI_RUNTIME=<absolute executable path>
FREQTRADE_AI_CANONICAL_V13_RESEARCH_IMAGE=<image>@sha256:<64 lowercase hex>
FREQTRADE_AI_CANONICAL_V13_MARKET_ARTIFACT_ROOT=<absolute immutable root>
FREQTRADE_AI_CANONICAL_V13_RESEARCH_WORKSPACE_ROOT=<absolute private temp root>
FREQTRADE_AI_CANONICAL_V13_RESEARCH_CPU_LIMIT=1.0
FREQTRADE_AI_CANONICAL_V13_RESEARCH_MEMORY_MB=<256..4096>
FREQTRADE_AI_CANONICAL_V13_RESEARCH_TIMEOUT_SECONDS=<30..3600>
FREQTRADE_AI_CANONICAL_V13_RESEARCH_OUTPUT_BYTES=<4096..8388608>
FREQTRADE_AI_CANONICAL_V13_RESEARCH_PIDS_LIMIT=<16..256>
FREQTRADE_AI_CANONICAL_V13_RESEARCH_TMPFS_MB=<32..512>
```

缺任何值、image 未 pin、runtime/path/symlink 不安全、四个 DB locator/identity 不分离，均在启动
sandbox 前 `BLOCKED`。本 runbook 不授权配置这些值；恢复任务必须先更新 release pin 和完成独立
provisioning/attestation。

static/lookahead gate 只需要 canonical reader DSN，并使用独立 activation：

```text
FREQTRADE_AI_CANONICAL_V13_LOOKAHEAD_EXECUTION_ENABLED=PRODUCTION_LOOKAHEAD_NO_TRADE_V1
```

其余 OCI image、runtime、market root、workspace root 与资源上限变量和上表相同。示例 command file
包含 exact lineage、可重试 key 与已验收 release/image/source identity，不含 plan 或 validation attempt：

```json
{
  "idempotency_key": "<batch-v3>:<strategy-version-uuid>",
  "release_commit": "<40-char-commit>",
  "executor_image_digest": "<sha256>",
  "worker_source_digest": "<sha256>",
  "lineage": {
    "strategy_version_id": "<uuid>",
    "research_target_id": "<uuid>",
    "configuration_bundle_id": "<uuid>",
    "configuration_bundle_digest": "<sha256>",
    "market_snapshot_id": "<uuid>",
    "market_snapshot_digest": "<sha256>"
  }
}
```

调用形式为 `python scripts/canonical_v13_research.py gate --command-file <absolute-json>`。生产执行前
仍须逐项复核 accepted release/image pin、freshness、单执行锁和零副作用；本命令存在不代表这些门已满足。

### 5.1 canonical worker image

仓库内 `containers/canonical-v13-research/Containerfile` 是 production research worker 的唯一构建
入口。其 `FROM` 必须固定到 reviewed Freqtrade platform manifest digest，不能以 `stable`、
`latest` 或其他 mutable tag 作为 authority。构建后必须重新记录派生镜像的 immutable digest，
验证 `linux/arm64`、空 `ENTRYPOINT`、固定 worker path，并用 adapter 的完整 security flags 只运行
`preflight`；不得用该验收步骤运行 `backtest`。

worker 只接受 canonical request/bundle/plan/strategy 和 `/input/market-*.data`。它先逐项验证
canonical JSON、SHA-256、no-trade capability、target/window lineage，再在 `/work` 中生成临时
Freqtrade data/config/result。任何解析、执行或输出漂移都只返回 exact `BLOCKED` envelope，不输出
host environment、input 内容或底层异常文本。

## 6. optimization 与重新验证

`create_optimization_run` 只接受 persisted `QUALIFIED` baseline。trial 不能覆盖 baseline version；
选中 trial 必须先经 controlled submission 创建新的 `UNVALIDATED` strategy version，且
`execution_authorized=false`。新 version 必须从 static、lookahead、exact plan、one-shot backtest、
score、qualification 全链重新执行；不得复制 baseline score/qualification 或直接 promotion。

## 7. 首回测前的独立门

以下全部有 exact evidence 才能执行第一个 production attempt：

1. clean release checkout pin 到包含本 contract 的 remote `main` SHA，main push CI 全绿；
2. authority upgrade 与 gate-receipt additive upgrade 均为 `CURRENT/ACCEPTED`，48 tables，旧 broad writer 零 ACL/membership；
3. 六个 API/research LOGIN exact membership 与同库 locator 验证通过；
4. canonical market artifact 文件存在、root-relative locator、size/digest/coverage/freshness 全匹配；
5. frozen bundle/target/window/scoring/quality snapshots 与 exact digest 已人工复核；
6. pinned OCI image digest、worker contract、resource limits 和 network-none attestation 已复核；
7. static 与 lookahead receipts 已通过；exact plan READY；attempt-specific authorization 未过期且未
   consume/revoke；
8. `TRADING_DISABLED`、无 runtime/signal/order/fill side effect，并有 rollback/incident owner。

任一门缺失时为 `NO_OP/BLOCKED`。authority rollback 只在十一张 research 表仍全空时允许；一旦产生
任何 research row，不能回授 broad writer，必须设计新的显式迁移。
