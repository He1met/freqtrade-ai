# Canonical V1.3 Phase 9 controlled execution

本 runbook 是 #724 的唯一 Phase 9 操作入口。所有阶段严格串行；上一阶段的 immutable receipt、
exact lineage、前后计数和 rollback evidence 未被证明为 `READY` 时，下一阶段必须 `NO_OP`。

永久边界：仅 `OKX_DEMO`、`demo_only=true`、`allow_real_funds=false`；严禁 `OKX_LIVE`、真实资金、
withdraw、输出凭据、第二个 order writer、绕过人工 approval、伪造 fixture 或放宽 freshness/risk。

## Frozen handoff

每次操作都必须重新从 current canonical PostgreSQL 核对以下 exact handoff，不能从本文推断 current：

| evidence | immutable identity |
|---|---|
| strategy version | `67169d28-ac80-432e-acd5-ffd2202b6cf7` |
| qualification | `a47ee71b-b757-4512-9e88-7522098b7e9d` |
| configuration bundle | `c837818a-af53-57c0-b5d0-22a7cafacf1f` |
| market snapshot | `be151746-ca10-4f01-962e-739552ee772f` |
| qualification digest | `987041fa910a92ee695535ef0ca1fc4a65e5173b11582aecfe12463f972b74e4` |
| bundle digest | `91634b332a9ae5a5d99463f37fa6ef67c821de33109f855d3735e74dde009ee8` |
| market digest | `f15fe60f3dc810f71bb17cf272a65c4eefeadfe284bd9d1114068992f15980b9` |

截至本次 current-state 只读核对，exact bundle 及 canonical schema 中不存在可执行的正式 risk
policy/budget source；`CANONICAL_PHASE9_RISK_BUDGET_SOURCE_UNSET` 与
`CANONICAL_RISK_POLICY_LINEAGE_UNSET` 因而是 B 的 accepted-decision 和 C 的硬阻塞。legacy
`freqtrade_ai` policy/budget 不绑定上述 exact lineage，禁止回退使用。不得调用 Phase 9 写入口发明金额、
重置额度或把 research quality limits 当 execution budget；必须先由独立、已批准的 canonical
configuration 变更交付 frozen policy、额度、expiry、policy digest 与 exact-lineage source receipt，
随后重新从第 1 节开始验收。

严格链为：

`qualification → human approval → deployment → long-lived runtime → natural signal → intent → central risk reservation → canonical order writer → Demo order → fill → ledger → reconciliation`

`control_activation`、`ephemeral_research`、`long_lived_runtime`、`order_writer` 的 process identity、
PostgreSQL capability、LaunchAgent label 与 lifecycle 均不同。research 永远 network-none；runtime
只读 canonical lineage 并输出 signal receipt，不持有 signal/order writer；只有独立 signal writer
可持久化 signal，只有 `canonical_order_writer` 可执行唯一 Demo POST。

## 1. Current main、release 与只读门

先证明 `origin/main`、detached clean release HEAD、成功的 post-main 3/3 CI 完全一致。禁止从 Codex
worktree 启动服务。然后用 API、UI、只读数据库事务核对 `HEALTHY/READY/TRADING_DISABLED`、
`ACTIVE_DEPLOYMENT_UNSET` 和 execution-domain 全零计数：

```bash
PHASE9_IDS=(
  --qualification-decision-id a47ee71b-b757-4512-9e88-7522098b7e9d
  --strategy-version-id 67169d28-ac80-432e-acd5-ffd2202b6cf7
  --configuration-bundle-id c837818a-af53-57c0-b5d0-22a7cafacf1f
  --market-snapshot-id be151746-ca10-4f01-962e-739552ee772f
)
backend/.venv/bin/python backend/scripts/canonical_v13_bootstrap.py verify-research-provisioned
backend/.venv/bin/python backend/scripts/canonical_v13_bootstrap.py phase9-readiness \
  --stage QUALIFICATION_HANDOFF "${PHASE9_IDS[@]}"
backend/.venv/bin/python backend/scripts/canonical_v13_bootstrap.py phase9-schema-verify
```

`QUALIFICATION_HANDOFF` 只接受上表 exact IDs/digests；仓库中存在其他 QUALIFIED 行不会被误选，
但任何 handoff drift、越界 ACTIVE deployment/runtime 或 execution side effect 均阻塞。

## 2. Backup、migration、ACL 与 restore

按 [production bootstrap](strategy_platform_v13_production_bootstrap.md) 在仓库外 `0700` 目录创建
custom-format backup，记录 SHA-256，并恢复到全新
`freqtrade_ai_v13_restore_<lowercase_identity>`。restore 必须通过 owner、manifest、ACL 和行计数核对。

Phase 9 additive upgrade 将 manifest 提升至 52 tables，并新增：

- `execution_risk_budget_authorizations`
- `execution_risk_reservations`
- `execution_attestations`
- `order_writer_leases`

以及 approval/deployment/runtime/signal 的唯一性约束。apply 前先锁定 execution boundary 并要求旧
Phase 9 表为空；upgrade actor、DDL、ACL 和 manifest 以 immutable audit receipt 绑定。

```bash
export FREQTRADE_AI_CANONICAL_V13_UPGRADE_ACTOR='operator:<explicit-identity>'
backend/.venv/bin/python backend/scripts/canonical_v13_bootstrap.py phase9-schema-apply
python scripts/canonical_v13_api_service.py provision-phase9
backend/.venv/bin/python backend/scripts/canonical_v13_bootstrap.py verify-phase9-provisioned
```

8 个 Phase 9 LOGIN 必须 distinct、同一 canonical database、只继承一个 capability。不得把
`SELECT ... FOR UPDATE` 需求转换为额外 ACL。`phase9-schema-rollback` 只允许所有 Phase 9 表仍为空
时使用；产生 A 阶段记录后，恢复方案必须是 stop services + forward recovery 或已验 backup restore，
不能删除证据行。

## 3. Release acceptance

只在 PR 3/3、review/comments/threads、mergeability 和 post-main 3/3 全部通过后，将 release 精确
固定到新 main。按既有安全流程先迁移/配置 principals，再 restart API/UI。restart 后必须证明：

- `/healthz` 为 `HEALTHY / TRADING_DISABLED`；`/readyz` 为 `READY`；
- API/UI 各一个 loopback process，无 legacy fallback；
- 14 个 service LOGIN identity 均 distinct；
- runtime readiness 仍 `TRADING_DISABLED / ACTIVE_DEPLOYMENT_UNSET`；
- credential reads、signals、intents、orders、fills、ledger、reconciliation 均为 0。

任一不满足都回滚 release/service 到已接受 SHA，并停止，不设置 active deployment。

## 4. A — NO_ORDER_SOAK

1. 由明确的人类 actor 为 exact qualification 创建 approval；重放必须返回同一 digest。
2. 以 approval 创建 Demo-only deployment；此时仍 `PENDING`。
3. 准备 runtime 的 secret-free plist：

   ```bash
   python scripts/canonical_v13_phase9_service.py prepare \
     --service long_lived_runtime --stage NO_ORDER_SOAK \
     --release-digest <sha256-of-canonical-v13-release-colon-main-sha> \
     --deployment-id <exact-deployment-id> \
     --deployment-capability-digest <exact-capability-digest> \
     --image-digest <accepted-runtime-image-digest>
   ```

4. 人工比对返回的 plan digest 后执行 `confirm --plan-digest <exact>`。只有 launchd loaded、fresh
   lease、holder PID alive 三者同时成立才可将 supervisor observation 写入 canonical DB，并把
   deployment 转为 `ACTIVE`。
5. 运行 no-order soak；runtime 无 order capability，signal/intent/risk/order/fill/ledger/reconciliation
   全部保持 0。执行 restart/recover，证明单 runtime、generation fencing、无 expired orphan。
6. `phase9-readiness --phase9-stage NO_ORDER_SOAK` 必须 `READY`。

LaunchAgent plist 不含 DSN 或交易凭据。runtime 与 API/control/research/order writer label 不得复用。

## 5. B — SIGNAL_RISK_SHADOW

order writer 保持 stopped/unloaded，DB writer lease 为 0。runtime 从 exact frozen strategy/bundle 与
新鲜 public market evidence 生成 deterministic natural-signal receipt；独立
`canonical_signal_writer` 验证 receipt digest、runtime heartbeat、exact deployment 后才持久化。

随后依次创建 intent 和 central risk decision。risk authority 必须使用已正式授权且未过期的 budget，
原子保留 notional/order count；必须分别验证一条真实可达的 `RISK_ACCEPTED` 与一条安全拒绝合同，
但不得为测试消耗/重置/发明生产预算。所有重放返回原 receipt 且计数不变。

```bash
backend/.venv/bin/python backend/scripts/canonical_v13_bootstrap.py phase9-readiness \
  --stage SIGNAL_RISK_SHADOW "${PHASE9_IDS[@]}"
```

此阶段 orders/fills/ledger/reconciliation 必须始终为 0。

## 6. C — OKX_DEMO_CANARY

只有 A/B READY 且存在当前正式 risk budget 时才读取既有 Keychain。输出只能包含摘要/布尔值；不得
打印 environment、headers、raw account payload 或 subprocess command。fresh attestation 必须同时证明：

- 固定 `https://openapi.okx.com`、`x-simulated-trading: 1`、`OKX_DEMO`；
- account fingerprint 与 pin 匹配；当前 egress IP 在 allowlist；
- permissions 精确为 `read=true, trade=true, withdraw=false`；
- Futures/long-short/isolated 模式及目标 SWAP live；
- credential generation、instrument metadata、价格、余额/仓位/挂单/reconciliation 均新鲜；
- 最小交易所 size 对应 notional 不超过 remaining formal budget。

若缺 risk budget、预算已耗尽、市场/allowlist/credential 未知或 attestation 过期，立即 `BLOCKED`，
不得创建 order。禁止为了通过门禁创建或重置金额。

writer 使用独立 `canonical_order_writer` LOGIN 和独立 LaunchAgent：

```bash
python scripts/canonical_v13_phase9_service.py prepare \
  --service order_writer --stage OKX_DEMO_CANARY --enable-order-writer \
  --release-digest <sha256-of-canonical-v13-release-colon-main-sha>
python scripts/canonical_v13_phase9_service.py confirm \
  --service order_writer --plan-digest <exact>
```

先在 DB durable prepare order request 与 idempotency key，再且仅再执行一次 allowlisted
`POST /api/v5/trade/order`。timeout/unknown outcome 禁止再次 POST，只能用 GET order identity 恢复。
收到 fill 后，由 distinct fill/ledger/reconciliation writers 依次写 exact chain；每步重放 no-op，最后：

```bash
backend/.venv/bin/python backend/scripts/canonical_v13_bootstrap.py phase9-readiness \
  --stage OKX_DEMO_CANARY "${PHASE9_IDS[@]}"
```

## 7. D — recovery acceptance

先停止 order writer 并释放 exact DB lease，再验证：runtime restart generation、GET-only order replay、
fill/ledger/reconciliation duplicate replay、observability receipt、无 zombie、无悬挂 LaunchAgent/文件锁/
DB advisory lock。将这些摘要写入 append-only recovery acceptance receipt；不得存 raw secret/status payload。

```bash
python scripts/canonical_v13_phase9_service.py stop --service order_writer
python scripts/canonical_v13_phase9_service.py restart \
  --service long_lived_runtime --plan-digest <exact>
python scripts/canonical_v13_phase9_service.py recover --service long_lived_runtime
backend/.venv/bin/python backend/scripts/canonical_v13_bootstrap.py phase9-readiness \
  --stage RECOVERY_SOAK "${PHASE9_IDS[@]}"
```

只有 D `READY`、post-execution backup/restore verifier 通过、所有 receipts 可重算、服务唯一且没有锁或
未决 recovery，才可关闭 #724。然后汇总 Phase 0–9 current evidence，最后关闭 #714。

## Fail-closed recovery order

任何阶段失败：停止后置 writer/runtime → 只读盘点 exact lineage/计数/leases → 撤销 credential session →
GET-only 恢复未知 order → reconciliation → 验证 backup restore → 决定 forward retry 或 release rollback。
不得删除 canonical receipt、改写 qualification/approval、重发 unknown order 或切换到 live funds。
