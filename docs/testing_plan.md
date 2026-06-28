# Testing Plan

## Phase 0

- 后端 `/health` 使用 pytest 验证。
- YAML 配置必须能被解析。
- 前端必须能通过 TypeScript 构建。
- 禁止项扫描：不出现 Redis、Celery、Kafka、RabbitMQ、真实 API Key、实盘交易模块、模拟盘交易模块。

## Phase 1

- 策略蓝图 JSON schema 测试。
- 策略代码生成单元测试。
- Freqtrade Adapter 命令构造测试。
- 回测结果解析测试。
- PostgreSQL repository 测试。
- 评分算法测试。

## 回归策略

每个 PR 至少运行受影响模块测试。阶段门 PR 必须运行完整验证清单。
