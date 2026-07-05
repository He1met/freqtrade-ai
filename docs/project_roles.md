# Project Roles

本项目后续开发按 AI Agent / 团队角色分工运行。角色分工用于约束 Issue
拆分、PR 范围、验收责任和安全边界；它不授权任何角色绕过阶段治理、启动真实交易、
连接真实交易所、提交真实密钥、修改 Freqtrade 源码或引入 Redis / Celery / Kafka /
RabbitMQ。

## 全局边界

- 一个 PR 只解决一个已批准 Issue 或一个明确的治理文档任务。
- 任何新功能必须先完成 [feature_intake.md](feature_intake.md)。
- 每个 PR 必须按 [acceptance_checklist.md](acceptance_checklist.md) 给出验收证据。
- 默认禁止操作 production、shared、remote 数据库。
- 默认禁止真实 dry-run、live trading、真实下单、真实交易所连接和真实 K 线下载。
- 默认禁止把 API key、secret、passphrase、token 写入代码、配置、数据库、日志、报告、
  Issue、PR 或文档。
- 默认禁止修改 Freqtrade 源码。
- 默认禁止实现 Redis、Celery、Kafka、RabbitMQ、worker pool、deployment executor 或
  live bot start / stop / deploy controls。

## 角色权限矩阵

| 角色 | 职责 | 是否允许修改代码 | 是否允许修改数据库 | 是否允许创建 Issue | 是否允许合并 PR | 主要输出物 |
| --- | --- | --- | --- | --- | --- | --- |
| Product Manager | 定义阶段目标、Feature Intake、优先级、范围边界、验收口径和不做范围。 | 否，仅修改产品/治理文档。 | 否。 | 是，限需求、范围、验收和 blocker。 | 否。 | Feature Request、范围说明、验收标准、阶段计划、scope creep 判定。 |
| Architect Engineer | 维护架构边界、模块契约、接口设计、数据流、风险拆分和 PR 拆分建议。 | 仅允许修改架构文档、接口草案和设计文档；业务实现代码需单独 Dev Issue。 | 否；仅能提出 migration / schema 设计建议。 | 是，限架构设计、拆分和技术 blocker。 | 否。 | 架构设计、接口契约、schema 设计、依赖边界、拆分计划。 |
| Development Engineer | 按已批准 Issue 实现后端、前端、脚本、测试和 smoke，保持最小 diff。 | 是，限已批准 Issue 范围内的业务代码、测试和脚本。 | 不允许直接改 production/shared/remote；仅允许提交 migration、测试 fixture 或本地开发数据脚本。 | 是，限实现中发现的 blocker 或拆分项。 | 否。 | 实现 PR、单元测试、smoke、变更说明、fail-closed 处理。 |
| QA Test Engineer | 设计验收矩阵、本地 DB seed、UI E2E、API 检查、脏数据测试和 DB/UI 对账。 | 是，限测试、fixture、seed、E2E、smoke 和测试文档。 | 是，仅限 local/dev/test 数据库；允许 reset、migration、seed、插入脏数据和对账；严禁 production/shared/remote。 | 是，限缺陷、验收缺口和回归 blocker。 | 否。 | 测试计划、E2E 记录、DB/UI 对账报告、缺陷 Issue、回归结果。 |
| SRE / DevOps | 维护本地运行健康检查、CI、只读诊断、artifact 留存、日志边界和操作手册输入。 | 是，限 CI、脚本、配置校验、只读诊断和运维文档。 | 否；只允许读取 local/dev/test 诊断状态，不做远端写入。 | 是，限运行健康、CI、诊断和操作 blocker。 | 否。 | 健康检查报告、CI 结果、诊断脚本、运行手册输入、artifact 索引。 |
| Security / Risk | 审查密钥、交易控制、真实交易所、真实 dry-run/live、部署和安全边界。 | 是，限安全校验、secret scan、风险策略、文档和测试。 | 否。 | 是，限安全缺陷、风险审批和边界 blocker。 | 否。 | 风险评估、secret scan 结果、安全门禁、禁止项确认、审批建议。 |
| Quant / Data | 定义策略研究指标、数据质量要求、回测解释、baseline 对比和实验口径。 | 是，限研究脚本、指标解析、测试 fixture、文档和 smoke；不得扩大交易能力。 | 仅限 local/dev/test 研究数据和 fixture；严禁 production/shared/remote。 | 是，限数据质量、指标解释和研究 blocker。 | 否。 | 数据质量报告、指标定义、baseline 对比、实验说明、研究验收证据。 |
| Release Manager | 汇总 PR 风险、验证证据、scope creep、发布边界和合并前检查。 | 否，除 release notes、PR 模板、治理文档外不改代码。 | 否。 | 是，限 release blocker、回归缺口和拆分要求。 | 是，仅在所有必需验收通过、范围明确且无高风险未审批项时合并。 | PR 合并判定、release notes、验证清单、回滚说明、Done 判定。 |
| Technical Writer / Operator Docs | 维护 README、roadmap、operator docs、运行手册、FAQ、验收说明和链接一致性。 | 否，除文档、示例和模板外不改代码。 | 否。 | 是，限文档缺口、操作风险和链接问题。 | 否。 | 用户文档、operator runbook、变更说明、文档链接检查、术语表。 |

## 冲突处理

- Product Manager 对范围和优先级负责；Security / Risk 对安全边界有否决权。
- Architect Engineer 对跨模块契约和 PR 拆分有否决权。
- QA Test Engineer 对验收证据不完整有否决权。
- Release Manager 在合并前必须确认范围、测试、安全和文档证据齐备。
- 任一角色发现需求蔓延、生产数据库风险、真实交易风险、密钥风险或测试缺口时，必须创建
  blocker Issue 或在 PR 中标记 `BLOCKED`，不能用 best effort 合并。
