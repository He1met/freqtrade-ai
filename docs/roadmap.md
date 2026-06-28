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

## Phase 2: 策略稳健性验证

增加数据质量检查、随机窗口验证、Walk-forward、Monte Carlo、市场状态分组、过拟合检测和更完整的评分体系。

## Phase 3: 运行态准备

在治理规则允许后，才考虑 Dry-run 管理、运行态监控和更严格的人工审批。

## Phase 4: 生产交易治理

Live 交易必须单独评审。默认禁用，必须通过 Issue、Milestone、PR、审计和人工确认。
