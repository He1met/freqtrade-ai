# Phase 1 验收说明

## MVP smoke 命令

从仓库根目录运行离线 smoke：

```bash
python3 scripts/smoke_mvp.py --offline --tmp-dir /tmp/freqtrade-ai-smoke
```

该命令使用临时 SQLite、fake strategy blueprint provider、fixture Freqtrade result JSON，以及现有 repository/service 层。除非传入 `--skip-frontend-build` 做后端局部诊断，否则命令也会运行前端 build。

## 通过标准

- 能初始化隔离的临时数据库 schema。
- 能通过 `FakeStrategyBlueprintProvider` 生成策略。
- 能把生成策略文件写入 smoke 临时目录。
- 能创建 backtest run 和 backtest task。
- 能使用 fixture runner 代替真实 Freqtrade CLI。
- 能把 fixture Freqtrade 指标解析入 `backtest_results`。
- 能计算策略评分并验证排行榜输出。
- 能通过 `npm run build` 构建前端。
- 任一关键步骤失败时，命令输出对应 `FAIL` 步骤和失败原因。

## fake / fixture 边界

- 策略生成使用 fake provider，不调用真实 LLM。
- 回测使用 fixture runner 写入 Freqtrade 形状 JSON，不调用真实 Freqtrade CLI。
- 数据库使用 `--tmp-dir` 下的临时 SQLite，不连接生产数据库。

## 限制和禁止项

- 不连接真实交易所 API，不下载真实 K 线。
- 不执行 dry-run、live trading 或真实下单。
- 不读取或写入真实 API key、secret 或交易所凭据。
- 不引入 Redis、Celery、Kafka、RabbitMQ。
- 不修改 Freqtrade 源码。
- smoke 通过只代表 Phase 1 MVP 离线演示路径可用，不代表可进行 dry-run、live trading、生产部署、真实交易所接入或真实下单。

## Review 证据

Phase 1 MVP review 至少运行：

```bash
python3 scripts/smoke_mvp.py --offline --tmp-dir /tmp/freqtrade-ai-smoke
cd backend && . .venv/bin/activate && pytest
cd frontend && npm run build
git diff --check
```
