# Database Design Phase 1

## 设计原则

第一阶段只使用 PostgreSQL、YAML、ENV 和本地文件系统。PostgreSQL 只保存长期结构化数据，不保存真实密钥、不保存 K 线明细、不保存每笔交易明细。

## 8 张核心表

- `strategies`: 策略主信息、生命周期、标签和当前版本指针。
- `strategy_generation_runs`: AI 策略生成批次、模型信息、提示摘要、生成数量和状态。
- `strategy_versions`: 策略版本、JSON 蓝图、生成代码快照、文件路径和校验状态。
- `market_data_files`: Freqtrade 行情数据文件索引，只记录文件元数据。
- `backtest_runs`: 回测批次、配置快照、任务计数和批次状态。
- `backtest_tasks`: 具体回测任务、策略版本、交易对、周期、窗口、配置路径和结果路径。
- `backtest_results`: 回测核心指标和 Freqtrade 原始结果 JSON 摘要。
- `strategy_scores`: 策略综合评分、分项评分和排行榜快照。

## 表结构说明

### `strategies`

保存策略身份和生命周期状态。

关键字段：

- `name`、`slug`: 策略名称和唯一标识。
- `status`: `draft`、`active`、`archived`。
- `source`: `ai_generated`、`imported`、`manual`。
- `tags`: JSONB 标签数组。
- `current_version_id`: 当前策略版本指针。

### `strategy_generation_runs`

保存一次 AI 策略生成批次，不保存密钥或完整敏感提示。

关键字段：

- `provider`、`model`: 生成服务和模型名称。
- `prompt_hash`、`prompt_summary`: 提示词追踪信息。
- `params_snapshot`: 非敏感参数快照。
- `status`: `pending`、`running`、`succeeded`、`failed`、`cancelled`。
- `requested_count`、`generated_count`、`accepted_count`、`failed_count`: 批次数量。

### `strategy_versions`

保存策略版本的最小可用信息，支持后续代码渲染、文件管理和回测。

关键字段：

- `strategy_id`: 所属策略。
- `generation_run_id`: 来源生成批次，可为空。
- `version_number`: 策略内递增版本号。
- `blueprint`: 策略 JSON 蓝图。
- `generated_code`: 生成代码快照。
- `file_path`: 生成策略文件路径。
- `validation_status`、`validation_errors`: 校验结果。

约束：

- `(strategy_id, version_number)` 唯一。
- `file_path` 唯一。

### `market_data_files`

只索引 Freqtrade 已下载的数据文件，不把 K 线明细写入 PostgreSQL。

关键字段：

- `exchange`、`pair`、`timeframe`: 数据归属。
- `data_format`: `feather`、`parquet`、`json`、`csv`。
- `relative_path`: 本地相对路径。
- `timerange_start`、`timerange_end`: 文件覆盖窗口。
- `row_count`、`file_size_bytes`、`checksum`: 文件元数据。

### `backtest_runs`

保存一批回测任务的批次信息和配置快照。

关键字段：

- `name`: 批次名称。
- `profile_name`: 使用的回测配置 profile。
- `config_snapshot`: 当次任务的 YAML / ENV 派生配置快照，不含真实密钥。
- `status`: `pending`、`running`、`succeeded`、`failed`、`cancelled`。
- `total_tasks`、`pending_tasks`、`running_tasks`、`succeeded_tasks`、`failed_tasks`: 批次计数。

### `backtest_tasks`

保存单个 Freqtrade 回测任务，Worker 后续可以用状态字段领取任务。

关键字段：

- `backtest_run_id`: 所属批次。
- `strategy_version_id`: 要回测的策略版本。
- `market_data_file_id`: 关联行情数据文件索引，可为空。
- `pair`、`timeframe`、`timerange_start`、`timerange_end`: 回测范围。
- `status`: `pending`、`running`、`succeeded`、`failed`、`cancelled`。
- `freqtrade_config_path`: 本次生成的 Freqtrade 配置文件路径。
- `result_file_path`: Freqtrade 回测结果 JSON 文件路径。

约束：

- 同一批次内同一策略版本、交易对、周期和窗口只能创建一个任务。

### `backtest_results`

保存回测摘要指标和 Freqtrade 原始结果 JSON，不保存 trade-level 明细表。

关键字段：

- `backtest_task_id`: 对应回测任务，唯一。
- `strategy_version_id`: 便于按策略版本查询。
- `total_trades`、`win_rate`、`profit_total_abs`、`profit_total_pct`。
- `max_drawdown_pct`、`sharpe`、`sortino`、`calmar`、`expectancy`。
- `summary`: 归一化摘要。
- `raw_result`: Freqtrade 原始结果 JSON。

### `strategy_scores`

保存排行榜评分快照。

关键字段：

- `strategy_id`、`strategy_version_id`: 策略和版本。
- `backtest_result_id`: 来源回测结果，可为空。
- `scoring_version`: 评分算法版本。
- `total_score`: 综合评分。
- `profit_score`、`risk_score`、`stability_score`、`quality_score`: 分项评分。
- `metrics_snapshot`: 评分输入指标快照。

约束：

- `(strategy_version_id, scoring_version)` 唯一。

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

## 迁移与 schema 版本

当前基线由 `backend/app/db/migrations.py` 的 `SCHEMA_VERSION` 管理，执行
`make db-init` 迁移，执行 `make db-verify` 验证。迁移只接受 PostgreSQL SQLAlchemy
URL，例如 `postgresql+psycopg://user:password@host:5432/database`；不要把该 URL 直接
传给 `psql`，因为 `psql` 不理解 SQLAlchemy driver 名。需要人工检查时，可在 backend
虚拟环境中调用 `app.db.migrations.psql_database_url()` 生成不含密码的 libpq URL，并通过
`PGPASSWORD` 或 `.pgpass` 提供认证。

新库会从当前 SQLAlchemy ORM 创建全部表、外键、unique/check constraint，并写入
`freqtrade_ai_schema_migrations`。旧的非版本化 schema 仅在所有受管表为空时可原子重建；
只要检测到数据就会在改动前 BLOCKED。迁移 DDL 运行在 PostgreSQL transaction 中，失败
会回滚；本地开发库仍应先执行 `make db-backup`。

## 索引策略

Phase 1 只建立 MVP 查询需要的基础索引：

- 状态队列索引：`strategy_generation_runs.status`、`backtest_runs.status`、`backtest_tasks.status`。
- 外键查询索引：策略版本、生成批次、回测批次、回测结果和评分相关外键。
- 行情文件索引：`exchange`、`pair`、`timeframe`。
- 排行榜索引：`strategy_scores.total_score DESC`。

## 不保存内容

Phase 1 迁移不包含以下内容：

- 真实 API Key、交易所 Secret、Passphrase。
- K 线明细表。
- 每笔交易明细表。
- Dry-run / live trading 运行表。
- 部署、审批、回滚表。
- Redis / Celery / Kafka / RabbitMQ 相关结构。
