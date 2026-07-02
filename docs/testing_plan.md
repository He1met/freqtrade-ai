# Testing Plan

## Phase 0

- 后端 `/health` 使用 pytest 验证。
- YAML 配置必须能被解析。
- 前端必须能通过 TypeScript 构建。
- 禁止项扫描：不出现 Redis、Celery、Kafka、RabbitMQ、真实 API Key、实盘交易模块、
  模拟盘交易模块。

## Phase 1

- 策略蓝图 JSON schema 测试。
- 策略代码生成单元测试。
- Freqtrade Adapter 命令构造测试。
- 回测结果解析测试。
- PostgreSQL repository 测试。
- 评分算法测试。
- MVP smoke 验收命令：

```bash
python3 scripts/smoke_mvp.py --offline --tmp-dir /tmp/freqtrade-ai-smoke
```

该命令使用临时 SQLite、fake strategy provider、fixture backtest result 和现有解析/评分/
排行榜实现，验证 AI 策略生成到前端构建的 Phase 1 MVP 闭环。命令会输出每个关键步骤的
`RUN` / `PASS` / `FAIL` 状态，并在失败时打印具体失败步骤。

真实实现覆盖：

- 策略蓝图持久化、策略版本写入和生成策略文件。
- 回测任务生命周期、结果 JSON 解析和 `backtest_results` 入库。
- 策略评分服务和排行榜排序。
- 前端 TypeScript build。

fixture / fake 覆盖：

- 策略生成使用 `FakeStrategyBlueprintProvider`，不调用真实 LLM。
- 回测使用 fixture runner 写入 Freqtrade 形状 JSON，不调用真实 Freqtrade CLI。
- 数据库使用 `--tmp-dir` 下的临时 SQLite，不连接生产数据库。

限制和禁止项：

- 不连接真实交易所，不下载 K 线。
- 不执行 dry-run、live trading 或真实下单。
- 不读取或写入真实 API key。
- 不引入 Redis、Celery、Kafka、RabbitMQ。
- 不修改 Freqtrade 源码。

## 回归策略

每个 PR 至少运行受影响模块测试。阶段门 PR 必须运行完整验证清单。

## Phase 2 Real Freqtrade Spike

Phase 2 的第一个 Spike 验证真实 Freqtrade CLI 是否能在本地已有数据上运行单策略
backtesting：

```bash
python3 scripts/spike_real_freqtrade_backtest.py
```

该命令只使用本地已有 `user_data/data` 文件，不下载 K 线、不连接真实交易所、不执行
dry-run 或 live trading。若本地缺少 `freqtrade` 命令或行情数据，命令会输出
`BLOCKED` 并在 `reports/spikes/phase2_real_freqtrade_backtest_latest.md` 写入原因。

## Phase 2 Strategy Research Smoke

Phase 2 策略研发增强使用新的离线 smoke 覆盖 schema v2、静态检查、失败原因、
版本 Diff / lineage、评分拆解和前端 build：

```bash
python3 scripts/smoke_phase2.py --offline --tmp-dir /tmp/freqtrade-ai-phase2-smoke
```

该命令默认只使用临时 SQLite、`FakeStrategyBlueprintProvider`、fixture backtest
metrics 和本地前端 build。命令会输出每个关键步骤的 `RUN` / `PASS` / `FAIL`
状态；任一步失败时会打印具体失败步骤和异常。

覆盖范围：

- 校验生成的 `StrategyBlueprint` 使用 schema v2。
- 对生成策略代码执行静态审查，并验证违规 fixture 能被识别。
- 创建带 parent 的策略子版本，并校验 Diff / lineage 查询结果。
- 写入并查询 warning 级失败原因。
- 使用 fixture backtest metrics 生成 Phase 2 评分拆解和排行榜记录。
- 默认运行 `npm run build` 验证前端评分拆解展示仍可构建。

限制和禁止项：

- 不调用真实 LLM，不读取真实 LLM API key。
- 不连接真实交易所，不下载 K 线。
- 不调用真实 Freqtrade CLI，不执行 dry-run、live trading 或真实下单。
- 不连接生产数据库，不写入运行产物到仓库。
- 不引入 Redis、Celery、Kafka、RabbitMQ。
- 不修改 Freqtrade 源码。

Phase 2 开发 PR 仍需单独运行完整回归命令：

```bash
cd backend && . .venv/bin/activate && pytest
python3 -m compileall backend/app backend/tests scripts
cd frontend && npm run build
git diff --check
```

## Phase 3 Backtesting Smoke

Phase 3 回测体系增强使用离线 smoke 覆盖 MarketDataCatalog、BacktestProfile v2、
artifact manifest、结果指标解析、批量矩阵 fail-closed 聚合、baseline/reproducibility
检查和前端 build：

```bash
python3 scripts/smoke_phase3.py --offline --tmp-dir /tmp/freqtrade-ai-phase3-smoke
```

该命令默认生成临时 fixture 行情数据并使用 fake backtest runner，不调用真实
Freqtrade CLI、不连接真实交易所、不下载 K 线、不执行 dry-run/live trading/Hyperopt，
也不连接生产数据库。命令会额外检查仓库 `user_data/data` 的真实回测 readiness；如果
本地没有真实数据，只输出 `BLOCKED` readiness 信息，smoke 仍以 fixture/local-only
路径验证 Phase 3 闭环。

覆盖范围：

- 扫描临时 `user_data/data` fixture 并校验 catalog 元数据。
- 校验 BacktestProfile v2 的 local-only 安全边界。
- 生成一个成功任务和一个缺数据 `BLOCKED` 任务，写入 artifact manifest 与矩阵摘要。
- 解析 fixture Freqtrade result JSON 的核心指标和风险指标。
- 生成 reproducibility fingerprint，并验证 stable 与 missing-baseline 状态。
- 默认运行 `npm run build` 验证前端 Phase 3 展示仍可构建。

限制和禁止项：

- 不调用真实 Freqtrade CLI；真实路径只作为 readiness 检查，不伪造成功。
- 不连接真实交易所，不下载 K 线。
- 不执行 dry-run、live trading、Hyperopt 或真实下单。
- 不读取或写入真实 API key。
- 不引入 Redis、Celery、Kafka、RabbitMQ。
- 不修改 Freqtrade 源码。
