# Feature Intake

本项目后续任何新增功能、阶段扩展、运行能力、治理能力或前端展示变更，都必须先完成
Feature Intake。Intake 的目的不是批准开发，而是先明确范围、风险、验收标准和安全边界，
避免 Codex 或人工开发绕过阶段治理，把 fixture / fallback 误当成真实运行能力，或把
Phase 7 验收扩展成 Phase 8、live trading、生产部署或交易控制能力。

Phase 7 已完成 runtime read-only API contract、operator status API、audit events、
CI、secret scanning、Operator Dashboard 和 Phase 7 smoke。Phase 7 验收不授权 Phase 8、
live trading、真实下单、交易所连接、真实 K 线下载、生产部署、deployment executor、
live bot start / stop / deploy controls、队列基础设施实现或 Freqtrade 源码修改。

## Intake 流程

1. 先创建 Feature Request Issue，并完整填写本文件的 Intake 字段。
2. 明确功能所属阶段；如果要进入新阶段，必须先有单独规划 Issue 和人工审批。
3. 按风险等级确认是否需要单独审批。高风险功能不得和普通功能合并在同一个 PR 中推进。
4. 在 Issue 中写清楚验收标准，并引用 [acceptance_checklist.md](acceptance_checklist.md)。
5. PR 只能实现已批准 Issue 的范围，不得顺手扩展、重构或新增交易控制能力。

## Intake 字段

每个 Feature Request 必须包含并回答以下字段：

| 字段 | 填写要求 |
| --- | --- |
| 功能背景 | 说明为什么需要该功能、来自哪个阶段或哪个已验收缺口。 |
| 功能目标 | 用可验收语言描述最终产出。 |
| 所属阶段 | 例如 Phase 7 maintenance、Future Phase planning；不得默认启动 Phase 8。 |
| 风险等级 | `safe` / `medium` / `high`，并说明理由。 |
| 是否只读 | `yes` / `no`；如果不是只读，必须说明写入对象和 fail-closed 行为。 |
| 是否涉及真实交易所 | `yes` / `no`；`yes` 必须单独审批。 |
| 是否涉及真实密钥 | `yes` / `no`；`yes` 必须说明 ENV-only、脱敏和禁止落盘边界。 |
| 是否涉及 dry-run | `yes` / `no`；真实 dry-run execution 属于高风险。 |
| 是否涉及 live trading | `yes` / `no`；`yes` 默认禁止，除非后续单独阶段明确批准。 |
| 是否涉及部署 | `yes` / `no`；deployment executor 或生产操作属于高风险。 |
| 是否涉及 worker / queue | `yes` / `no`；实现 queue infrastructure 属于高风险。 |
| 是否需要人工审批 | `yes` / `no`；高风险必须为 `yes`。 |
| 是否需要更新 smoke | `yes` / `no`；新增行为通常需要更新对应 offline smoke。 |
| 是否需要更新前端 | `yes` / `no`；涉及 UI 时必须说明 fallback / fixture 可见性。 |
| 是否需要更新文档 | `yes` / `no`；阶段边界、运行能力和验收标准变更必须更新文档。 |

## 风险等级

- `safe`：文档、只读展示、测试说明、静态校验或不改变运行能力的治理变更。
- `medium`：新增 API、schema、artifact、manifest、前端状态展示、offline smoke 或本地只读诊断，
  但不连接真实交易所、不读取真实密钥、不启动真实 dry-run / live trading、不部署。
- `high`：任何可能触达真实运行、真实交易所、真实密钥、生产部署、live candidate execution、
  queue infrastructure 或交易控制能力的变更。

## 高风险功能必须单独审批

以下功能必须单独创建 Issue、单独审批、单独验收，不能被普通 Feature Request 或阶段维护 PR 顺带实现：

- real dry-run execution；
- exchange connectivity；
- live candidate execution；
- deployment executor；
- queue infrastructure；
- production operation。

高风险审批至少必须说明：

- 人工审批人和审批记录保存位置；
- 是否允许真实密钥进入运行环境，以及如何保证不落盘、不进日志、不进 Issue / PR；
- fail-closed 条件和阻断输出；
- 需要运行的 smoke、pytest、compileall、frontend build、secret scan 和人工检查；
- 如何证明没有新增 live bot start / stop / deploy controls，或者该控制能力已由新阶段明确授权。

## 明确禁止项

除非后续单独阶段、单独 Issue 和人工审批明确授权，任何 Feature Request 和 PR 都不得：

- 执行真实下单；
- 启动 live trading；
- 连接真实交易所；
- 下载真实 K 线；
- 提交真实 API key、secret、passphrase；
- 修改 Freqtrade 源码；
- 实现 Redis、Celery、Kafka、RabbitMQ 或 worker pool；
- 实现 deployment executor；
- 实现 start / stop / deploy live bot 控制；
- 把 fixture / fallback 数据展示成真实运行数据。

## Fixture / Fallback 可见性

任何使用 fixture、fake runner、fallback、mock data 或本地临时 artifact 的功能，都必须在 API payload、
前端展示、文档或 smoke 输出中让来源可见。不能把 fallback 状态写成 production ready，不能把
fixture artifact 当成真实交易、真实 dry-run、真实部署或真实交易所连接证据。

## Intake 结果

Intake 完成后，Issue 应明确给出以下结论之一：

- `APPROVED`：范围、风险和验收标准清晰，可以进入实现。
- `NEEDS_APPROVAL`：触及高风险能力，等待人工审批。
- `BLOCKED`：缺少阶段授权、验收标准、安全边界或本地前置条件。
- `REJECTED`：违反当前阶段禁止项或试图绕过治理边界。
