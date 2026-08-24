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

执行额度只能由 C 阶段的 sealed probe receipt 推导：固定 exact QUALIFIED target
`BTC-USDT-SWAP`、`LONG_ONLY`、一单、30 分钟 one-shot policy，金额为交易所最小合约数量乘 linear
`ctVal` 再乘新鲜 mark。effective leverage 冻结为 authenticated current long leverage，且必须
`0 < current_long <= min(exact artifact leverage cap, exchange_max_leverage)`；当前 acceptance artifact
的 exact cap 为 12，系统不得自动 set leverage。禁止输入金额、重置额度、把 B 的
shadow acceptance 当 execution authority，或回退使用 legacy `freqtrade_ai` policy/budget。

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
  --qualification-decision-id ee55ba5c-1f9a-4647-8425-35ba58079048
  --strategy-version-id 11db1710-ddba-4d04-8f7a-b2214191366f
  --configuration-bundle-id b56c2263-ad33-5565-8875-7914a0e4b455
  --market-snapshot-id 09f630f9-9c6c-4375-9e7e-4a665e4cbc60
)
backend/.venv/bin/python backend/scripts/canonical_v13_bootstrap.py verify-research-provisioned
backend/.venv/bin/python backend/scripts/canonical_v13_bootstrap.py phase9-readiness \
  --stage QUALIFICATION_HANDOFF "${PHASE9_IDS[@]}"
backend/.venv/bin/python backend/scripts/canonical_v13_bootstrap.py phase9-schema-verify
```

`QUALIFICATION_HANDOFF` 只接受上表 exact IDs/digests；仓库中存在其他 QUALIFIED 行不会被误选，
但任何 handoff drift、`PENDING`/`ACTIVE` deployment 或 execution side effect 均阻塞。历史
approval/runtime receipt 与带完整 disable receipt 的 `DISABLED` deployment 保留且不阻塞新的 exact
handoff；不得删除历史行来制造全零。

## 2. Backup、migration、ACL 与 restore

按 [production bootstrap](strategy_platform_v13_production_bootstrap.md) 在仓库外 `0700` 目录创建
custom-format backup，记录 SHA-256，并恢复到全新
`freqtrade_ai_v13_restore_<lowercase_identity>`。restore 必须通过 owner、manifest、ACL 和行计数核对。

Phase 9 additive upgrade 将 manifest 提升至 56 tables，并新增：

- `execution_canary_probe_receipts`
- `execution_canary_risk_policies`
- `execution_risk_budget_authorizations`
- `execution_risk_reservations`
- `execution_attestations`
- `order_writer_leases`
- `order_dispatch_receipts`（exactly-one POST 的 immutable claim）
- `order_dispatch_outcome_receipts`（exactly-one POST/GET recovery outcome）

以及 approval/deployment/runtime/signal 的唯一性约束。apply 前先锁定 execution boundary 并要求旧
Phase 9 表为空；upgrade actor、DDL、ACL 和 manifest 以 immutable audit receipt 绑定。

```bash
export FREQTRADE_AI_CANONICAL_V13_UPGRADE_ACTOR='operator:<explicit-identity>'
backend/.venv/bin/python backend/scripts/canonical_v13_bootstrap.py phase9-schema-apply
backend/.venv/bin/python scripts/canonical_v13_runtime_image.py schema-apply
backend/.venv/bin/python backend/scripts/canonical_v13_bootstrap.py runtime-reader-acl-apply
backend/.venv/bin/python backend/scripts/canonical_v13_bootstrap.py runtime-reader-acl-verify
backend/.venv/bin/python backend/scripts/canonical_v13_bootstrap.py deployment-rollover-apply
backend/.venv/bin/python backend/scripts/canonical_v13_bootstrap.py deployment-rollover-verify
backend/.venv/bin/python backend/scripts/canonical_v13_bootstrap.py acceptance-trigger-apply
backend/.venv/bin/python backend/scripts/canonical_v13_bootstrap.py acceptance-trigger-verify
backend/.venv/bin/python backend/scripts/canonical_v13_bootstrap.py shadow-risk-acl-apply
backend/.venv/bin/python backend/scripts/canonical_v13_bootstrap.py shadow-risk-acl-verify
backend/.venv/bin/python backend/scripts/canonical_v13_bootstrap.py phase9-transition-apply
backend/.venv/bin/python backend/scripts/canonical_v13_bootstrap.py phase9-transition-verify
backend/.venv/bin/python backend/scripts/canonical_v13_bootstrap.py phase9-policy-renewal-apply
backend/.venv/bin/python backend/scripts/canonical_v13_bootstrap.py phase9-policy-renewal-verify
python scripts/canonical_v13_api_service.py provision-phase9
python scripts/canonical_v13_api_service.py provision-runtime-reader
backend/.venv/bin/python backend/scripts/canonical_v13_bootstrap.py verify-phase9-provisioned
```

升级顺序固定为 Phase 9 schema → runtime image → runtime-reader ACL → deployment rollover → acceptance
trigger → shadow-risk ACL → Phase B/C transition → policy renewal；rollback
必须严格反向执行。后层尚存在时，前层 rollback 必须返回明确 `BLOCKED_*_ROLLBACK_REQUIRED`，不得只改
manifest digest 留下 partial columns、trigger 或 ACL。

Phase 9 的 8 个 writer LOGIN（approval/deployment/signal/risk/order/fill/ledger/reconciliation）必须
distinct、同一 canonical database、只继承一个 capability；另有 1 个独立 runtime reader LOGIN。
全 manifest 共 15 个 distinct service LOGIN（2 API + 4 research + 8 Phase 9 writers +
1 runtime reader），其中 13 个是 writer LOGIN（control 1 + research 4 + Phase 9 8），另有 API reader
与 runtime reader 2 个只读 LOGIN。
`canonical_approval_writer` 是 sealed probe receipt、one-shot policy 与 budget authorization 的唯一
writer，`canonical_risk_writer` 只能写 intent、shadow/execution decision 与 execution reservation。
为校验 exact qualified target，`canonical_risk_writer` 读取 `research_targets`、`deployments`，并通过
独立 scoped ACL receipt 只读 `deployment_approvals` 与 `qualification_decisions`；后两项 upgrade 不改变
global manifest，且只允许 `SELECT`。它不获得这些表的 DML 权限，也不获得
signal/order/fill/ledger/reconciliation writer capability。
Phase 9 provision 完成后，`verify-research-provisioned` 与 API research repair preflight 仅在 8 个
Phase 9 LOGIN 组或单独 runtime LOGIN 组完整存在时组合该组并复核 exact membership/CONNECT；整组
不存在保持早期 research 合同，部分存在或权限漂移必须 fail closed。
不得把
`SELECT ... FOR UPDATE` 需求转换为额外 ACL。`phase9-schema-rollback` 只允许所有 Phase 9 表仍为空
时使用；产生 A 阶段记录后，恢复方案必须是 stop services + forward recovery 或已验 backup restore，
不能删除证据行。

schema rollback 在同一 PostgreSQL transaction 内只撤销 #781 对 surviving tables 的冻结 ACL 增量、
8 个 Phase 9 writer capability 与 runtime-reader capability 的数据库 `CONNECT`、唯一约束、扩展表及
manifest；predecessor 的既有 ACL 不得被改动。`PREVIOUS_READY` 必须重算完整 predecessor
surviving-table ACL，且上述 capability 仍有额外 table grant 或 `CONNECT` 时 fail closed。随后如需完整
退回旧 release，操作顺序固定为：先验证 ACL rollback receipt → 再撤销/删除 Phase 9 与 runtime LOGIN
及 membership → 最后删除对应 Keychain password/signer items；任一步失败都停止，禁止先删 Keychain
造成不可恢复的半完成状态。apply、rollback、reapply 与 replay 都必须返回可重算 receipt。
runtime-reader ACL rollover 的 rollback 入口为
`canonical_v13_bootstrap.py runtime-reader-acl-rollback`；它只撤销
`qualification_decisions` 的 runtime-reader `SELECT` 并恢复 predecessor manifest，不删除历史行。
完成 shadow-risk ACL rollback 后，acceptance trigger rollback 必须在其余既有层中最先执行
`canonical_v13_bootstrap.py acceptance-trigger-rollback`：仅当 trigger/signal 证据均为 0 时才删除本层
table/columns/functions，精确撤销 `canonical_signal_writer` 的四项后生 lineage `SELECT`；随后才允许
deployment rollover、runtime-reader、runtime image 与 Phase 9 schema 依次反向 rollback。任一证据非零或
ACL/trigger partial drift 都必须阻塞，禁止 `CASCADE`。
shadow-risk ACL rollback 为独立入口
`canonical_v13_bootstrap.py shadow-risk-acl-rollback`，只撤销上述两项 risk writer `SELECT`；partial ACL、
额外 DML 或 manifest drift 必须 fail closed。release rollback 时先撤销 shadow-risk ACL，再进入既有
acceptance-trigger/deployment/runtime/schema 反向顺序。

Phase B/C transition upgrade 将历史 intent/decision 按 immutable payload backfill 为
`TEST_SIMULATED` 或 `SIGNAL_RISK_SHADOW`，并以 `(signal_id,intent_mode)` 与
`(trade_intent_id,decision_mode)` 唯一键替代旧单列唯一键。apply/replay 必须复核行集合 digest、mode counts、
immutability triggers 与未变化的 global manifest；不得改写历史 JSON/digest。进入 C 后，同一个 exact
acceptance signal 必须另建 `intent_mode=EXECUTION` intent，shadow intent/decision 保留不变；execution
decision 与 reservation 只绑定 execution intent。存在同 signal 多 mode 证据后
`phase9-transition-rollback` 必须 fail closed，不能为回退而删除或合并历史。

Phase C policy renewal 只允许在旧 policy 已过 TTL 且尚无 risk budget、reservation 或 order 时，将旧行
追加终态 `EXPIRED` 证据后创建新的 `ACTIVE` policy。数据库以 partial unique index 保证同一 qualification
和 approval 最多一个 `ACTIVE` policy；旧 policy、probe 与 attestation 不删除、不覆盖。readiness 仅把
非 `EXPIRED` policy 绑定的 probe/attestation 计入当前严格链。存在第二条 policy 历史后
`phase9-policy-renewal-rollback` 必须 fail closed。

完成 schema/ACL rollback 且 API、canonical runtime 与 order writer LaunchAgent 均已 unload 后，只能用
以下窄入口清理 9 个固定 LOGIN 与 10 个固定 Keychain item；它不读取或删除 OKX credential：

```bash
python scripts/canonical_v13_api_service.py cleanup-phase9-provisioning
```

入口必须先证明 `PREVIOUS_READY`、Phase 9 affected rows 全零、无相关 PostgreSQL session、LOGIN 属性与
唯一 membership 精确匹配。数据库角色在一个 transaction 内先撤 membership 再删除，commit 并复核全缺失
后才逐项删除 Keychain。全缺失 replay 必须 `repeat_noop=true`；DB 已清理但 Keychain 删除中断时只允许继续
删除残留固定 item；角色或 Keychain 的其他 partial/drift 状态一律 `BLOCKED`。

## 3. Release acceptance

只在 PR 3/3、review/comments/threads、mergeability 和 post-main 3/3 全部通过后，将 release 精确
固定到新 main。按既有安全流程先迁移/配置 principals，再 restart API/UI。restart 后必须证明：

- `/healthz` 为 `HEALTHY / TRADING_DISABLED`；`/readyz` 为 `READY`；
- `/readyz.phase9_identities` 只投影 API 实际持有的 approval、deployment、risk 三个 writer
  identity；signal/order/fill/ledger/reconciliation identity 必须保持在各自独立进程，API 不读取也不投影；
- API/UI 各一个 loopback process，无 legacy fallback；
- 15 个 service LOGIN identity 均 distinct；
- runtime readiness 仍 `TRADING_DISABLED / ACTIVE_DEPLOYMENT_UNSET`；
- credential reads、signals、intents、orders、fills、ledger、reconciliation 均为 0。

任一不满足都回滚 release/service 到已接受 SHA，并停止，不设置 active deployment。

## 4. A — NO_ORDER_SOAK

0. 由 provisioner 复核 `scripts/canonical_v13_runtime_image.py schema-verify`，再从 clean exact
   accepted release 运行 `build`。构建必须使用 pinned base digest、`--pull=never --network=none`，并
   记录 immutable reference、source-tree/recipe/SBOM/config/manifest digest。人工核对后仅以
   `accept --immutable-reference sha256:<digest> --actor <human-operator>` 登记；mutable tag、research
   executor、source/release/platform/entrypoint/security/provenance 漂移均为 `BLOCKED`。`show
   --acceptance-id <uuid>` 返回的 receipt 是 runtime plan 唯一 image authority，裸 digest 不可替代。
1. 由明确的人类 actor 为 exact qualification 创建 approval；重放必须返回同一 digest。
2. 若存在旧 `ACTIVE` deployment，先证明其 runtime 已 `STOPPED` 且全局 ACTIVE order-writer lease 为 0，
   并从 accepted release 将 supervisor 的 exact STOP receipt、unloaded LaunchAgent、空 lease 与无 container
   组合为唯一 production stop observation：

   ```bash
   python scripts/canonical_v13_phase9_service.py confirm-runtime-stop-observation \
     --service long_lived_runtime --plan-digest <exact-stopped-plan-digest>
   ```

   该入口仅由 `canonical_deployment_writer` 把既有 runtime 行推进到 `STOPPED`，写入不可变 receipt；
   exact replay 必须 `repeat_noop=true`。任何仍 loaded 的 LaunchAgent、lease、container、writer lease、
   plan/lineage/digest 漂移都 fail closed。随后
   再由 `canonical_deployment_writer` 调用
   `POST /api/canonical-v13/phase9/deployments/{old_id}/disable`，命令只含新 exact
   `superseded_by_qualification_decision_id`、人工 actor 与 reason。该事务必须原子写入 `DISABLED`、
   target qualification、request/receipt digest 与时间；相同命令重放 `repeat_noop=true`，任何字段漂移、
   非 ACTIVE source、未停止 runtime 或 active writer lease 均 fail closed。禁止直接 SQL 改状态。
3. 以新 approval 创建 Demo-only deployment；此时仍 `PENDING`，全局最多一个 nonterminal deployment。
4. 准备 runtime 的 secret-free plist：

   ```bash
   python scripts/canonical_v13_phase9_service.py prepare \
     --service long_lived_runtime --stage NO_ORDER_SOAK \
     --release-digest <sha256-of-canonical-v13-release-colon-main-sha> \
     --deployment-id <exact-deployment-id> \
     --deployment-capability-digest <exact-capability-digest> \
     --runtime-image-acceptance-id <accepted-runtime-image-uuid>
   ```

5. 人工比对返回的 plan digest 后执行 `confirm --plan-digest <exact>`。只有 launchd loaded、fresh
   lease、holder PID alive 三者同时成立才可将 supervisor observation 写入 canonical DB，并把
   deployment 转为 `ACTIVE`。
   deployment rollover 允许同一 canonical process identity 在旧 runtime 已 `STOPPED` 后生成新的
   deployment-scoped runtime row；数据库仅对 `status <> 'STOPPED'` 的 identity 保持唯一，任何两个
   非停止 runtime 使用同一 identity 都必须 fail closed。禁止复用或改写旧 runtime 历史行。
6. 运行 no-order soak；runtime 无 order capability，signal/intent/risk/order/fill/ledger/reconciliation
   全部保持 0。执行 restart/recover，证明单 runtime、generation fencing、无 expired orphan。
7. `phase9-readiness --phase9-stage NO_ORDER_SOAK` 必须 `READY`。

LaunchAgent plist 不含 DSN 或交易凭据。runtime 与 API/control/research/order writer label 不得复用。

## 5. B — SIGNAL_RISK_SHADOW

order writer 保持 stopped/unloaded，DB writer lease 为 0。runtime 从 exact frozen strategy/bundle 与
新鲜 public market evidence 生成 deterministic natural-signal receipt；独立
`canonical_signal_writer` 验证 receipt digest、runtime heartbeat、exact deployment 后才持久化。

从 A 的正式 `STOPPED` observation 切换到 B 时，首个 worker heartbeat 只允许把 exact stopped
runtime 识别为 `BLOCKED_PHASE9_RUNTIME_OBSERVATION_PENDING`。supervisor 保持 fenced lease、容器与
order-writer-disabled 状态，但不得读取市场或生成 signal；operator 随后调用正式
`confirm-runtime-observation` 将同一 runtime identity 和新 generation/image observation 推进为
`HEALTHY`，下一 heartbeat 才能评估市场。只有完整 STOP receipt、exact image/launch/deployment lineage
匹配时该 pending 状态可重试；其他 lineage drift 必须终止 supervisor，禁止靠竞态抢写 observation。

随后依次创建唯一 intent 和唯一 `SIGNAL_RISK_SHADOW` decision。shadow authority 只依据 exact
qualified target 与 long-only Demo intent envelope，在同一 immutable receipt 中封存一条真实 accepted
baseline check，以及 server-derived `side=sell/posSide=short` deterministic rejected counterfactual check；
不得写第二个 signal/intent/risk row。两个 check 都必须明确
`order_submission_enabled=false`、`execution_authorized=false`，receipt 无 budget ID、无 reservation ID。
所有重放返回原 receipt 且计数不变。B 阶段的 probe receipt、policy、budget、reservation、attestation、
order writer lease、orders/fills/ledger/reconciliation 必须全部为 0；shadow 的 `RISK_ACCEPTED` 状态绝不
满足 C 或 order writer。

```bash
backend/.venv/bin/python backend/scripts/canonical_v13_bootstrap.py phase9-readiness \
  --stage SIGNAL_RISK_SHADOW "${PHASE9_IDS[@]}"
```

此阶段 orders/fills/ledger/reconciliation 必须始终为 0。

若 accepted release 重建了新的 immutable runtime image，旧 runtime 行与最新 `STOPPED` receipt 仍保留
旧 image/launch digest 作为历史证据。stopped-observation bridge 只可将这种“stable lineage 与 exact STOP
receipt 均匹配、仅新 plan image 不同”的状态归类为 pending；`confirm-runtime-observation` 必须从当前 live
supervisor/container receipt 原子更新 runtime image/launch digest。STOP receipt、identity、capability、bundle
或 qualification 的任何其他漂移仍必须 `BLOCKED_PHASE9_RUNTIME_EXACT_LINEAGE`。

### B.1 一次性 deterministic acceptance trigger

若 natural evaluator 已以 fresh signed receipts 证明长期存活、连续返回
`NATURAL_SIGNAL_NO_ACTION`，但等待自然入场会无限阻塞技术验收，可在独立人工授权后使用
control-plane 的 `ACCEPTANCE_SCHEDULED_TEST`。它只验证 signal 后半链，不是策略信号、盈利证明、fixture
或 qualification 输入：不得进入 research/backtest/score/qualification 指标，UI 必须显示“验收测试信号”。

先确认 runtime/order writer 均已正式停止且 execution-domain 计数可核算，再完成本层 schema apply/verify、
release/image acceptance，重建 `SIGNAL_RISK_SHADOW` runtime。POST 只接受 exact canonical IDs、人工 actor
与 idempotency key，不接受浏览器提供的时间；server 固定下一闭合 15m UTC boundary，TTL 2 分钟：

```text
POST /api/canonical-v13/phase9/acceptance-signal-triggers
GET  /api/canonical-v13/phase9/acceptance-signal-triggers?qualification_decision_id=<exact>
```

数据库 trigger 行为 append-only：同一 deployment 最多一条，idempotency exact replay 返回原 receipt，
第二 key、更新、删除、reset、renew、早到、过期、lineage drift 均 fail closed。receipt 必须绑定 exact
qualification/approval/deployment/runtime image/bundle/snapshot，并固定 `OKX_DEMO`、
`allow_real_funds=false`、`acceptance_only=true`、`LONG_ONLY`、`max_order_count=1`。只有 live runtime holder、
fresh runtime observation、order writer disabled 且 plan digest 精确时，supervisor 才可消费一次：
trigger 表与 runtime image acceptance 继续由既有 `canonical_control_writer` 单独写入；该 capability
仅增加读取 `qualification_decisions` 的必要上游 lineage 权限，不获得 signal/order writer 能力。升级旧的
已接受 trigger schema 时，`acceptance-trigger-apply` 只修复这一项 ACL，exact replay 必须返回
`ACCEPTED`；rollback 精确撤销该增量。
全局 bootstrap/backup/API verifier 只有在上述专属 verifier 同时返回 `ACCEPTED` 时，才把这一条
`SELECT` 组合进 expected grants；`PREVIOUS_READY`、partial ACL 或额外 DML 权限仍必须 fail closed。

```bash
python scripts/canonical_v13_phase9_service.py execute-acceptance-trigger \
  --service long_lived_runtime \
  --plan-digest <exact-signal-risk-shadow-plan-digest> \
  --acceptance-trigger-id <server-issued-trigger-id>
```

该命令从 runtime HMAC Keychain signer 生成 signed worker receipt，经独立 `canonical_signal_writer`
持久化 source_kind=`ACCEPTANCE_SCHEDULED_TEST` signal；exact replay 返回同一 signal 且不再次消费。随后仍由
正式 API/identity 创建唯一 long intent 与唯一 `SIGNAL_RISK_SHADOW` decision。readiness 必须同时证明
trigger→signal exact lineage、`natural_signal=false`、shadow 两项 check、orders 及后续表全 0；不得把该行
改写或展示为 `NATURAL_STRATEGY_SIGNAL`。

## 6. C — OKX_DEMO_CANARY

进入 C 前，production PostgreSQL 必须先接受订单 dispatch 状态约束升级；它只把 durable
`DISPATCHING` 加入既有订单状态机，不创建订单、不加载 writer，也不访问 OKX：

```bash
backend/.venv/bin/python backend/scripts/canonical_v13_bootstrap.py order-dispatch-status-verify
backend/.venv/bin/python backend/scripts/canonical_v13_bootstrap.py order-dispatch-status-apply
backend/.venv/bin/python backend/scripts/canonical_v13_bootstrap.py order-dispatch-status-verify
backend/.venv/bin/python backend/scripts/canonical_v13_bootstrap.py order-dispatch-status-apply
```

要求 `PREVIOUS_READY → UPGRADED(repeat_noop=false) → ACCEPTED →
ACCEPTED(repeat_noop=true)`。任何 partial constraint、非法 order status 或 in-flight
`DISPATCHING` rollback 都必须 fail closed，禁止手工 `ALTER TABLE`。

只有 A/B READY 后才可读取既有 Keychain。输出只能包含摘要/布尔值；不得
打印 environment、headers、raw account payload 或 subprocess command。fresh attestation 必须同时证明：

- 固定 `https://openapi.okx.com`、`x-simulated-trading: 1`、`OKX_DEMO`；
- account fingerprint 与 pin 匹配；当前 egress IP 在 allowlist；
- permissions 精确为 `read=true, trade=true, withdraw=false`；
- Futures/long-short/isolated 模式及目标 SWAP live；
- credential generation、instrument metadata、价格、余额/仓位/挂单/reconciliation 均新鲜；
- instrument metadata 来自 public instruments，exchange maximum leverage 必须来自 authenticated
  `GET /api/v5/account/adjust-leverage-info`，不得由当前 leverage 或客户端 JSON 推断；
- authenticated positions 必须是 exact target、isolated 且 long/short 合约总数均为 0；pending orders
  必须为空，同时 `adjust-leverage-info.existOrd=false`；
- server 将 fresh mark 按 tick 向下对齐为 positive frozen limit price，并以 authenticated current long
  leverage 调用 `GET /api/v5/account/max-size`；exact singular `maxBuy` 必须不小于 `minSz`；
- 最小交易所 size 对应 notional 不超过由 sealed evidence 生成且未耗尽的 one-shot budget。

server-side probe 必须先生成 typed `RedactedOkxDemoProbe`，由独立
`canonical_deployment_writer` transaction 创建并提交同一 deployment 的 redacted attestation，再由
独立 `canonical_approval_writer` transaction 调用非公开 `persist_canary_probe_receipt(...)` 写入 immutable
`execution_canary_probe_receipts`。HTTP 不接受 raw facts；policy command 只接受 `probe_receipt_id`，并
重算八类资源 digest、各 observed/expires、combined digest、attestation/deployment lineage。八类为
instrument、mark、account config、leverage、exchange maximum leverage、positions、pending orders 与
maximum order quantity。随后才可
创建 30 分钟 policy、一次 budget authorization，并为同一 exact acceptance signal 上独立于 B 的新
`intent_mode=EXECUTION` intent 创建唯一
`decision_mode=EXECUTION` 的 `RISK_ACCEPTED` reservation。
policy 必须从 exact accepted strategy artifact 的 `leverage()` AST 提取不可伪造的上限；当前 acceptance
artifact 为 12x，因此 strategy cap=12，effective leverage 必须小于等于 `min(12, exchange max)`。浏览器、
CLI 或调用者不得自报/回退到历史 14x。

仓库已声明下列 release-only composition 命令；它在进程内创建 sealed session，且不提供 raw facts 参数：

```bash
python scripts/canonical_v13_phase9_service.py probe-canary \
  --service order_writer --deployment-id <exact-active-demo-deployment-id>
```

该命令按上述双 capability saga 串接两个 committed transaction。两步之间的 sealed safe probe 只保存
脱敏、可校验且有期限的 facts；若 attestation 已提交而 receipt 尚未提交即 crash，重放必须先确认尚无
linked receipt，再以同一 sealed probe/attestation exact idempotent completion，不能再次访问 authenticated
exchange，也不能生成第二条 attestation/receipt。只把脱敏输出的 exact `probe_receipt_id` 交给 canary
policy API。禁止用 SQL、fixture 或 raw JSON 手工补行。若缺
receipt/policy/budget、预算已耗尽、市场/allowlist/credential 未知或 attestation 过期，立即 `BLOCKED`，
不得创建 order。禁止为了通过门禁创建或重置金额。

由于 mark/max-size evidence 保持交易所原始短 TTL，生产首次创建 policy 必须使用 release-only
组合命令，避免把已提交 receipt 暴露给另一个人工/API 往返：

```bash
python scripts/canonical_v13_phase9_service.py probe-authorize-canary-policy \
  --service order_writer \
  --deployment-id <exact-active-demo-deployment-id> \
  --qualification-decision-id <exact-qualified-decision-id> \
  --deployment-approval-id <exact-human-approval-id> \
  --actor-identity operator:<owner> \
  --idempotency-key <one-shot-policy-key> \
  --reason <acceptance-only-reason>
```

该命令仍先以 `canonical_deployment_writer` 独立提交 immutable attestation；随后在同一个
`canonical_approval_writer` transaction 内写入新的 append-only probe receipt 与 policy。policy
qualification advisory lock 覆盖 probe generation，避免并发双写；同一 request 的 safe saga 文件只含
脱敏 facts。未提交 policy 前若 generation 过期，只能追加新的 attestation/receipt generation，不能更新或
删除历史行；policy 已提交后的 exact replay 直接返回同一 policy/receipt，不能再次访问 exchange。旧的
`probe-canary` 只用于 evidence-only probe 和 crash recovery，不得再将其短 TTL receipt 人工转交给首次
policy POST。组合命令仍不加载 writer、不调用 order POST，任何 lineage/freshness/flat/maxBuy/minSz
失败均整体回滚 approval transaction 并保持无订单。

writer 使用独立 `canonical_order_writer` LOGIN 和独立 LaunchAgent：

```bash
python scripts/canonical_v13_phase9_service.py prepare \
  --service order_writer --stage OKX_DEMO_CANARY --enable-order-writer \
  --release-digest <sha256-of-canonical-v13-release-colon-main-sha> \
  --deployment-id <exact-deployment-id> \
  --deployment-capability-digest <exact-deployment-capability-digest> \
  --execution-canary-risk-policy-id <exact-policy-id> \
  --execution-canary-risk-policy-digest <exact-policy-digest> \
  --attestation-id <exact-attestation-id> \
  --attestation-digest <exact-attestation-digest> \
  --attestation-expires-at <timezone-aware-ISO8601> \
  --instrument-metadata-digest <exact-instrument-resource-digest> \
  --mark-price-snapshot-digest <exact-mark-resource-digest> \
  --strategy-max-leverage <exact-artifact-derived-cap> \
  --effective-leverage <exact-authenticated-current-long-within-cap> \
  --position-policy LONG_ONLY
python scripts/canonical_v13_phase9_service.py confirm \
  --service order_writer --plan-digest <exact>
```

先在 DB durable prepare exact `ordType=limit`、`px=frozen limit_price`、`sz=minSz` request 与
idempotency key。唯一 POST 紧前，同一 sealed private session 必须再次 authenticated 读取 positions、
pending orders、current leverage 与 `max-size`；只有 fresh flat/no-pending、current long leverage 仍等于
policy effective leverage、`maxBuy>=minSz` 才可将 guard-bound exact-one claim 写入 immutable
`order_dispatch_receipts`。claim digest 必须绑定 order/risk/policy/probe/attestation、credential generation、
lease generation/digest/acquired/expires、四类 resource digests/windows 与 exact request，并证明
`lease_acquired_at <= claimed_at < lease_expires_at`；随后且仅随后执行一次 allowlisted
`POST /api/v5/trade/order`。POST success 或 GET recovery 只能首次写入一条
`order_dispatch_outcome_receipts`，绑定 claim digest、`clOrdId`、exchange order identity、redacted safe
response digest 与 `POST|GET_RECOVERY` mode；order receipt 必须由该 immutable outcome receipt 重算。
timeout/unknown outcome 禁止再次 guard 或 POST，只能用 GET order identity 恢复；重放必须返回原 receipt。
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
python scripts/canonical_v13_phase9_service.py confirm-runtime-observation \
  --service long_lived_runtime --plan-digest <exact-runtime-plan-digest>
python scripts/canonical_v13_phase9_service.py stop --service long_lived_runtime
python scripts/canonical_v13_phase9_service.py accept-recovery-soak \
  --service recovery_control \
  --qualification-decision-id <exact-qualified-decision-id>
backend/.venv/bin/python backend/scripts/canonical_v13_bootstrap.py phase9-readiness \
  --stage RECOVERY_SOAK "${PHASE9_IDS[@]}"
```

`accept-recovery-soak` 不接受 lifecycle、order、process 或 observability 的 caller raw
facts。它使用独立 `canonical_control_writer` 登录，只读重算 exact qualification order/runtime
lineage，并严格读取 append-only supervisor receipts 与当前 filesystem/launchd 状态。只有以下条件
同时成立才写入单一 append-only acceptance receipt：GET-only order replay 已是 exact no-op、writer
STOP 先于 runtime RESTART/RECOVER、最新 HEALTHY runtime observation 晚于 recovery、两个
LaunchAgent 均 unloaded、两个 file lease 均不存在、DB active order-writer lease 与 zombie process
均为零。任何 receipt 损坏、orphan lease、live holder 或 lineage drift 均 fail closed。

只有 D `READY`、post-execution backup/restore verifier 通过、所有 receipts 可重算、服务唯一且没有锁或
未决 recovery，才可关闭 #724。然后汇总 Phase 0–9 current evidence，最后关闭 #714。

## Fail-closed recovery order

任何阶段失败：停止后置 writer/runtime → 只读盘点 exact lineage/计数/leases → 撤销 credential session →
GET-only 恢复未知 order → reconciliation → 验证 backup restore → 决定 forward retry 或 release rollback。
不得删除 canonical receipt、改写 qualification/approval、重发 unknown order 或切换到 live funds。
