# Roadmap

## 后续功能入口

Phase 1 到 Phase 7 已完成验收。Phase 8 已通过单独 Epic / Issue 队列打开，
但后续任何新增功能仍必须先完成
[feature_intake.md](feature_intake.md)，再按
[acceptance_checklist.md](acceptance_checklist.md) 定义验证命令、人工检查和安全边界。
Feature Intake 不会自动启动 Phase 9，也不授权 live trading、真实下单、真实交易所连接、
真实 K 线下载、生产部署、deployment executor、live bot start / stop / deploy controls、
队列基础设施实现或 Freqtrade 源码修改。

## Phase 0: 项目治理与工程骨架

目标是建立目录、配置、文档、数据库草案、后端健康检查、前端路由空壳、Freqtrade Adapter 边界和 GitHub 项目治理规则。

不实现 AI 策略生成、不执行回测、不运行 Freqtrade CLI、不接交易所 API。

## Phase 1: AI 策略研发闭环

第一阶段只做研发闭环：

- AI 生成 JSON 策略蓝图
- 后端生成 Freqtrade Python 策略类
- 保存策略、版本和生成批次
- 创建批量回测任务
- 通过 Freqtrade 执行回测
- 解析核心回测指标
- 生成策略评分排行榜

Phase 1 不做 Redis、Celery、Kafka、RabbitMQ、实盘交易、模拟盘交易、复杂权限和复杂审计。

## Phase 2: 策略研发增强

状态：已完成验收。详见 [phase2_acceptance.md](phase2_acceptance.md)。

Phase 2 已完成：

- StrategyBlueprint schema v2 与严格校验。
- 策略代码静态检查与安全审查。
- 策略失败原因归档与查询。
- 策略版本 Diff 与 lineage 记录。
- 真实 LLM StrategyBlueprintProvider 边界。
- 策略质量评分拆解与淘汰规则。
- 前端失败原因、校验错误、版本 Diff 和 Ranking 评分拆解展示。
- Phase 2 离线 smoke 验收脚本。

Phase 2 不做 Hyperopt、dry-run、live trading、真实交易所连接或真实 K 线下载。

## Phase 3: 回测体系增强

状态：已完成验收。详见 [phase3_acceptance.md](phase3_acceptance.md)。

Phase 3 已建立可重复、可审计、可解释的本地回测研究闭环：

- 本地行情数据可用性检查和数据质量 catalog。
- BacktestProfile Schema v2 与实验变量锁定。
- 真实 Freqtrade backtesting artifact manifest。
- 回测结果指标扩展和版本兼容解析。
- 批量回测矩阵执行与 fail-closed 聚合。
- baseline 对比和重复性检查。
- 前端展示 BacktestRun artifact、增强指标和矩阵摘要。
- Phase 3 smoke 验收脚本。

Phase 3 只使用本地已有 `user_data/data` 行情数据。缺少数据时必须明确
`BLOCKED`，不得下载数据或伪造成功。

Phase 3 不做 Hyperopt、dry-run、live trading、真实交易所连接、真实 K 线下载或
生产运行。

## Phase 4: Hyperopt 参数优化

状态：已完成验收。详见 [phase4_acceptance.md](phase4_acceptance.md)。

Phase 4 已完成本地研究型 Hyperopt 参数优化闭环：

- HyperoptProfile schema 与优化变量锁定。
- 受控 Freqtrade `hyperopt` CLI runner 边界。
- Hyperopt artifact manifest 与 fail-closed 结果归档。
- Freqtrade Hyperopt result JSON 解析。
- 基于 Hyperopt best params 生成优化后 StrategyVersion。
- 优化前后策略表现对比。
- 前端展示 Hyperopt runs、best params 和 before/after 对比。
- Phase 4 offline smoke 验收脚本。

Phase 4 不做 dry-run、live trading、真实交易所连接、真实 K 线下载、真实下单、
真实 API key / secret / passphrase 提交、Freqtrade 源码修改或 Redis / Celery /
Kafka / RabbitMQ 队列基础设施。

## Phase 5: Dry-run / FreqUI 运行管理

状态：已完成最终验收。详见 [phase5_acceptance.md](phase5_acceptance.md) 和
[phase5_plan.md](phase5_plan.md)。

Phase 5 已在明确安全门禁下完成 dry-run readiness、DryRunProfile、受控
Freqtrade dry-run CLI 边界、ENV-only 密钥预检、artifact manifest、只读状态快照、
FreqUI 入口、前端展示、PR #127 拆分决策和 offline smoke。

Phase 5 仍不是 live trading 授权。当前能力只覆盖本项目外层的 dry-run / FreqUI
运行管理边界、离线验收和只读展示，不执行真实下单，不下载真实 K 线，不提交真实
密钥，不重写 FreqUI，也不修改 Freqtrade 源码。

## Phase 6: 实盘候选与部署管理

状态：已完成验收。详见 [phase6_acceptance.md](phase6_acceptance.md) 和
[phase6_live_candidate_plan.md](phase6_live_candidate_plan.md)。

Phase 6 已完成实盘候选与部署治理链路：

- 实盘候选与部署治理设计方案。
- `LiveCandidateProfile` schema 与准入条件锁定。
- 实盘候选风险检查清单与 fail-closed 预检。
- 人工审批记录与状态机。
- `DeploymentRecord` schema 与 rollback plan。
- 只读运行监控与告警摘要 DTO。
- 实盘候选审批与部署记录只读页面。
- Phase 6 offline governance smoke。

Phase 6 已完成的 Issue 队列：

- [#176](https://github.com/He1met/freqtrade-ai/issues/176)
  `[EPIC][Phase 6] 实盘候选与部署治理`
- [#177](https://github.com/He1met/freqtrade-ai/issues/177)
  `[Design][Phase 6] 实盘候选与部署治理设计方案`
- [#178](https://github.com/He1met/freqtrade-ai/issues/178)
  `[Backend][Phase 6] LiveCandidateProfile schema 与准入条件锁定`
- [#179](https://github.com/He1met/freqtrade-ai/issues/179)
  `[Backend][Phase 6] 实盘候选风险检查清单与 fail-closed 预检`
- [#180](https://github.com/He1met/freqtrade-ai/issues/180)
  `[Backend][Phase 6] 人工审批记录与状态机`
- [#181](https://github.com/He1met/freqtrade-ai/issues/181)
  `[Backend][Phase 6] DeploymentRecord schema 与 rollback plan`
- [#182](https://github.com/He1met/freqtrade-ai/issues/182)
  `[Backend][Phase 6] 只读运行监控与告警摘要 DTO`
- [#183](https://github.com/He1met/freqtrade-ai/issues/183)
  `[Frontend][Phase 6] 实盘候选审批与部署记录只读页面`
- [#184](https://github.com/He1met/freqtrade-ai/issues/184)
  `[Test][Phase 6] Phase 6 offline governance smoke`
- [#185](https://github.com/He1met/freqtrade-ai/issues/185)
  `[Review][Phase 6] Phase 6 实盘候选与部署治理验收`

Phase 6 安全边界：

- 不执行真实下单。
- 不启动 live trading。
- 不连接真实交易所。
- 不下载真实 K 线。
- 不提交真实 API key、secret、passphrase。
- 不把密钥写入代码、配置、数据库、日志或文档。
- 不修改 Freqtrade 源码。
- 不引入 Redis、Celery、Kafka、RabbitMQ，除非后续 Phase 7 明确允许。
- 不做生产部署。
- 不绕过人工审批。
- 不实现自动启动 live bot。
- 不提供 start / stop / deploy live 控制。

Phase 6 验收不授权 Phase 7。Phase 7 必须通过新的规划 Issue、验收标准和安全审查
单独启动。

## Phase 7: 工程化升级与规模化运行

状态：已完成最终验收。详见 [phase7_acceptance.md](phase7_acceptance.md)、
[phase7_engineering_plan.md](phase7_engineering_plan.md)、
[phase7_ci.md](phase7_ci.md)、[phase7_secret_scanning.md](phase7_secret_scanning.md)
和 [phase7_worker_queue_design.md](phase7_worker_queue_design.md)。

Phase 7 已完成工程化升级与规模化运行基础：

- Runtime read-only API contract。
- Operator status API 与本地诊断入口。
- Audit log schema 与 governance event 归档。
- GitHub Actions CI 基线。
- Secret scanning 与配置安全检查。
- Worker / Queue 架构设计方案，仅设计，不实现队列基础设施。
- Operator Dashboard 只读展示系统状态、fallback 状态、smoke 状态和
  artifact 链接。
- Phase 7 engineering smoke。

Phase 7 已完成的 Issue 队列：

- [#195](https://github.com/He1met/freqtrade-ai/issues/195)
  `[EPIC][Phase 7] 工程化升级与规模化运行`
- [#196](https://github.com/He1met/freqtrade-ai/issues/196)
  `[Docs][Phase 7] Phase 1-6 收口清理与 Phase 7 工程化规划`
- [#197](https://github.com/He1met/freqtrade-ai/issues/197)
  `[Backend][Phase 7] Runtime Read-only API Contract`
- [#198](https://github.com/He1met/freqtrade-ai/issues/198)
  `[Backend][Phase 7] Operator Status API 与本地诊断入口`
- [#199](https://github.com/He1met/freqtrade-ai/issues/199)
  `[Backend][Phase 7] Audit Log schema 与 governance event 归档`
- [#200](https://github.com/He1met/freqtrade-ai/issues/200)
  `[DevOps][Phase 7] GitHub Actions CI：backend pytest / frontend build / smoke`
- [#201](https://github.com/He1met/freqtrade-ai/issues/201)
  `[Security][Phase 7] Secret scanning 与配置安全检查增强`
- [#202](https://github.com/He1met/freqtrade-ai/issues/202)
  `[Design][Phase 7] Worker / Queue 架构设计方案`
- [#203](https://github.com/He1met/freqtrade-ai/issues/203)
  `[Frontend][Phase 7] Operator Dashboard：系统状态、fallback 状态、smoke 状态、artifact 链接`
- [#204](https://github.com/He1met/freqtrade-ai/issues/204)
  `[Test][Phase 7] Phase 7 engineering smoke`
- [#205](https://github.com/He1met/freqtrade-ai/issues/205)
  `[Review][Phase 7] Phase 7 工程化验收`

Phase 7 安全边界：

- 不执行真实下单。
- 不启动 live trading。
- 不连接真实交易所。
- 不下载真实 K 线。
- 不提交真实 API key、secret、passphrase。
- 不把密钥写入代码、配置、数据库、日志或文档。
- 不修改 Freqtrade 源码。
- 不实现 Redis、Celery、Kafka、RabbitMQ、worker pool 或生产 queue
  infrastructure。
- 不做生产部署。
- 不绕过人工审批。
- 不实现自动启动 live bot。
- 不提供 start / stop / deploy live 控制。
- 不实现 deployment executor。

Phase 7 验收不自动授权 Phase 8。Phase 8 已通过新的 Epic / Issue 队列单独启动，
详见 [phase8_local_strategy_lab_plan.md](phase8_local_strategy_lab_plan.md)。

## Phase 8: Local Strategy Lab 本地真实运行验证

状态：已创建 Epic 和 Issue 队列，尚未完成验收。详见
[phase8_local_strategy_lab_plan.md](phase8_local_strategy_lab_plan.md)。

Phase 8 的目标是把项目从 offline fixture / mock 展示升级为页面可操作、API 可调用、
数据库可对账、文件和 artifact 可追踪的本地策略研发闭环。

Phase 8 包括：

- 页面输入策略想法并触发后端策略生成。
- 策略、策略版本和生成记录真实落库。
- 生成后的策略文件写入 approved local runnable directory，并记录文件状态。
- 页面展示数据库-backed 的策略、版本、生成状态和文件状态。
- 页面触发本地回测任务。
- 回测任务、回测结果、artifact 引用和失败 / 阻塞原因真实落库。
- 策略评分和排行榜基于数据库 `BacktestResult` / `StrategyVersion`。
- 页面刷新后仍能从 API / 数据库读取核心记录。
- 页面明确展示 database / API / fixture / fallback / unknown 来源。
- QA 可以对账页面、API response 和数据库查询。
- 在主链路跑通后进行本地 dry-run readiness 检查。
- 仅在 readiness、安全和人工确认满足时，推进本地受控 dry-run 状态快照。

Phase 8 已创建的 Issue 队列：

- [#232](https://github.com/He1met/freqtrade-ai/issues/232)
  `[EPIC][Phase 8] Local Strategy Lab 本地真实运行验证`
- [#233](https://github.com/He1met/freqtrade-ai/issues/233)
  `[Design][Phase 8] Local Strategy Lab 总体设计与验收口径`
- [#234](https://github.com/He1met/freqtrade-ai/issues/234)
  `[Backend][Phase 8] API/DB 数据真实性契约与来源标识`
- [#235](https://github.com/He1met/freqtrade-ai/issues/235)
  `[Test][Phase 8] 本地测试数据库 reset/seed/dirty-data 能力`
- [#236](https://github.com/He1met/freqtrade-ai/issues/236)
  `[Backend][Phase 8] 策略想法提交到生成记录与策略版本落库`
- [#237](https://github.com/He1met/freqtrade-ai/issues/237)
  `[Backend][Phase 8] 策略文件写入可运行目录与可运行性验证`
- [#238](https://github.com/He1met/freqtrade-ai/issues/238)
  `[Frontend][Phase 8] 页面提交策略想法并展示生成状态`
- [#239](https://github.com/He1met/freqtrade-ai/issues/239)
  `[Backend][Phase 8] 页面触发本地回测任务与 fail-closed 前置检查`
- [#240](https://github.com/He1met/freqtrade-ai/issues/240)
  `[Backend][Phase 8] 回测 artifact 解析入库与任务/策略版本追踪`
- [#241](https://github.com/He1met/freqtrade-ai/issues/241)
  `[Backend][Phase 8] 策略评分与排行榜真实数据库链路`
- [#242](https://github.com/He1met/freqtrade-ai/issues/242)
  `[Frontend][Phase 8] 策略/版本/回测/评分真实数据展示`
- [#243](https://github.com/He1met/freqtrade-ai/issues/243)
  `[Frontend][Phase 8] 数据来源标识与 fallback/fixture 防冒充`
- [#244](https://github.com/He1met/freqtrade-ai/issues/244)
  `[Test][Phase 8] 页面/API/数据库三方对账 E2E 验收`
- [#245](https://github.com/He1met/freqtrade-ai/issues/245)
  `[Security][Phase 8] 本地 dry-run readiness 预检与 BLOCKED 展示`
- [#246](https://github.com/He1met/freqtrade-ai/issues/246)
  `[DevOps][Phase 8] 本地受控 dry-run 启停边界与状态快照`
- [#247](https://github.com/He1met/freqtrade-ai/issues/247)
  `[Review][Phase 8] Local Strategy Lab 阶段验收与安全边界审查`

Phase 8 安全边界：

- 不执行真实下单。
- 不启动 live trading。
- 不连接真实交易所。
- 不下载真实 K 线。
- 不操作 production / shared / remote / unknown 数据库。
- 不提交真实 API key、secret、passphrase 或 token。
- 不把密钥写入代码、配置、数据库、日志、文档、Issue、PR、截图或测试数据。
- 不修改 Freqtrade 源码。
- 不实现 Redis、Celery、Kafka、RabbitMQ、worker pool 或生产 queue infrastructure。
- 不实现 production deployment executor。
- 不绕过人工确认。
- 不提供 live bot start / stop / deploy 控制。
- 不把 fixture、fallback 或 unknown-source 数据伪装成真实数据库成功。

Phase 8 验收不授权 Phase 9、live trading、生产部署或真实下单。后续阶段必须通过新的
规划 Issue、验收标准和安全审查单独启动。
