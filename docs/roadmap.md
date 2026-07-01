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

## Phase 3: 规划后续增强

Phase 2 完成后，可以进入 Phase 3 规划。Phase 3 具体范围必须另行创建
Issue 和验收标准；不要把 Phase 2 验收直接扩大成 dry-run、live trading、
Hyperopt 或生产运行。

## Phase 4: 生产交易治理

Live 交易必须单独评审。默认禁用，必须通过 Issue、Milestone、PR、审计和人工确认。
