# Roadmap

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

Phase 5 可以进入 planning。Phase 5 规划必须单独创建 Epic、执行包 Issue、
验收标准和安全评审。

Phase 5 planning 不等于授权 dry-run 实现。任何 dry-run / FreqUI 运行管理能力
都必须通过单独 Issue、PR、安全边界、审计和人工确认。

## Phase 6: 实盘候选与部署管理

Live 交易必须单独评审，默认禁用，必须通过 Issue、Milestone、PR、审计和人工确认。

Phase 6 才允许规划实盘候选、人工审批、风险检查、部署记录和回滚机制。
