# Canonical V1.3 Phase 9 no-order readiness

本 runbook 只覆盖 #724 的代码、schema、权限边界与三段验收准备。它不授权读取交易凭据、连接
private OKX、创建 production deployment、启动 long-lived runtime，或制造 signal、intent、order、
fill、ledger、reconciliation。`TEST_SIMULATED`、fixture、SQLite 与隔离 PostgreSQL 只能证明代码
合同，不能替代 current canonical handoff 或 Demo 验收。

## 不可变边界

严格链路为：

`qualification → human approval → deployment → long-lived runtime → signal → intent → central risk → canonical order writer → Demo order → fill → ledger → reconciliation`

- 只接受一个 current canonical `QUALIFIED` decision，且必须重算通过 exact qualification receipt、
  frozen bundle digest、market snapshot digest 与 validation plan digest。
- `control_activation`、`ephemeral_research`、`long_lived_runtime`、`order_writer` 使用不同 process
  identity、LaunchAgent label 与生命周期。research 保持 network-none、credentialless、writerless；
  runtime 只能 signal-write；只有 `canonical_order_writer` 具有 order capability。
- 全链固定 `demo_only=true`、`allow_real_funds=false`。`OKX_LIVE`、真实资金和 withdraw 永久禁止。
- 缺失、歧义、digest drift、非零越界行或未知状态均为 `BLOCKED`；不得以 fixture 补齐。

机器合同位于：

- `app.canonical_v13.phase9_topology`
- `app.canonical_v13.phase9_readiness`
- `app.canonical_v13.phase9_schema_upgrade`

## Current-state 只读门

以下命令只接受显式 canonical PostgreSQL URL，并在数据库事务中执行 `SET TRANSACTION READ ONLY`：

```bash
export FREQTRADE_AI_CANONICAL_V13_PROVISIONER_DATABASE_URL='postgresql+psycopg:///freqtrade_ai_v13'
backend/.venv/bin/python backend/scripts/canonical_v13_bootstrap.py phase9-readiness \
  --stage QUALIFICATION_HANDOFF
backend/.venv/bin/python backend/scripts/canonical_v13_bootstrap.py phase9-schema-verify
```

`QUALIFICATION_HANDOFF` 只有在以下条件同时成立时才会返回 `READY`：exact `QUALIFIED=1`、资格
receipt 可重算、且十二张 Phase 9 execution-domain 表全部为 0。receipt 不含时间戳，因此相同数据库
状态会 byte-identical replay。

2026-08-20 的只读快照是：`QUALIFIED=0`、`REJECTED=29`，原因均为
`REQUIRED_WINDOW_GATE_FAILED`；十二张 execution-domain 表全部为 0；runtime API 返回
`TRADING_DISABLED / ACTIVE_DEPLOYMENT_UNSET`。研究线新增 strategy version
`cad3cd7a-73b1-4c74-9fec-4f342b9d428d`、bundle
`55ac84e1-2aa9-5c8b-a120-708e4a5ac8a8` 与 snapshot
`8c4365f6-7410-4edb-b4f9-bc67da8e6078`，但 gate attempt
`b22fce50-c611-4b8c-8320-3c2d500c3217` 仍为 `RUNNING` orphan，lease 到
`2026-08-20T23:40:28.226535+08:00`；它没有 static/lookahead/backtest/score/qualification
receipt，不能作为 Phase 9 handoff。该快照会过期，后续操作前必须重新运行上面命令，不能引用本文
作为 current evidence。

## Additive uniqueness upgrade

upgrade 只增加以下唯一性合同：

- 一个 qualification decision 最多一个 approval；
- 一个 approval 最多一个 deployment；
- 一个 deployment 最多一个 runtime identity；
- runtime receipt digest 不可重复；
- 同一 runtime、target、signal digest 的 exact replay 不可重复。

apply/rollback 都会在 DDL 前要求 `deployment_approvals`、`deployments`、`runtime_instances`、
`runtime_receipts`、`signals` 为 0；任何非零值或 partial constraint 状态均 fail closed。rollback 只移除
这五个约束，不删除或改写业务行。

在 apply 前，必须按
[`strategy_platform_v13_production_bootstrap.md`](strategy_platform_v13_production_bootstrap.md)
创建 custom-format backup、记录 SHA-256，并恢复到全新
`freqtrade_ai_v13_restore_<identity>` 做 owner/ACL/48-table 验证。备份或 restore evidence 缺失时停止。

```bash
export FREQTRADE_AI_CANONICAL_V13_UPGRADE_ACTOR='operator:<explicit-identity>'
backend/.venv/bin/python backend/scripts/canonical_v13_bootstrap.py phase9-schema-apply
backend/.venv/bin/python backend/scripts/canonical_v13_bootstrap.py phase9-schema-verify
```

rollback 仅用于尚未创建任何 Phase 9 行的 release rollback：

```bash
backend/.venv/bin/python backend/scripts/canonical_v13_bootstrap.py phase9-schema-rollback
```

## 三段执行门

1. `NO_ORDER_SOAK`：等待协调任务交付 exact handoff 和单独 runtime authority；要求 approval、
   deployment、runtime/heartbeat evidence 存在，signal 及其后所有表仍为 0。
2. `SIGNAL_RISK_SHADOW`：另行授权后只允许自然 signal→intent→risk shadow；orders 及其后所有表
   必须为 0。不得手工制造 signal 或把 `TEST_SIMULATED` 当验收。
3. `OKX_DEMO_CANARY`：需要再次明确授权、private credential boundary、唯一 writer lease、fresh
   reconciliation 与风险上限。当前 preflight 故意不提供此 stage；在合同、API/UI 与 supervisor
   接线合并前传入 `DEMO_CANARY` 必须 fail closed。

任一段失败都不得改写 qualification、approval 或历史 receipt。恢复顺序固定为：停止后置服务、
只读盘点 exact lineage 和非零计数、恢复数据库副本验证、再由新的明确授权决定 rollback 或重试。

## 当前未接线项

Phase 9 API/UI composition 暂不在本批改动中；`backend/app/canonical_v13/api.py`、DTO 与 canonical
frontend 正由 UX #769 独占。收到其 merge SHA 后必须基于新的 `main` 重新审计并另开后续唯一 Draft
PR。当前也没有 production LaunchAgent/supervisor、private OKX adapter 或真实 order path；这些缺口
不能被本地绿色测试外推为可运行。
