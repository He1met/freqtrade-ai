# Database Design Phase 1

## 设计原则

第一阶段只使用 PostgreSQL、YAML、ENV 和本地文件系统。PostgreSQL 只保存长期结构化数据，不保存真实密钥、不保存 K 线明细、不保存每笔交易明细。

## 8 张核心表

- `strategies`: 策略主信息。
- `strategy_versions`: 策略版本、JSON 蓝图和生成代码。
- `strategy_generation_runs`: AI 策略生成批次。
- `market_data_files`: 行情数据文件索引。
- `backtest_runs`: 回测批次。
- `backtest_tasks`: 具体回测任务和任务状态。
- `backtest_results`: 回测核心指标和 Freqtrade 原始结果 JSON。
- `strategy_scores`: 策略综合评分和排行榜。

## 配置边界

交易所、币种、周期、回测窗口、模型和路径配置放在 YAML。任务创建时把当时的配置快照写入 PostgreSQL，方便复现。

## 任务队列

Phase 1 不引入 Redis。回测任务由 `backtest_tasks.status` 管理，Worker 使用 PostgreSQL 行锁领取任务：

```sql
SELECT id
FROM backtest_tasks
WHERE status = 'pending'
ORDER BY id
LIMIT 1
FOR UPDATE SKIP LOCKED;
```

## 迁移文件

初始化 SQL 位于 `db/migrations/001_init.sql`。
