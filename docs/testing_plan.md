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

## Phase 4 Hyperopt Smoke

Phase 4 Hyperopt 参数优化使用离线 smoke 覆盖 HyperoptProfile、
Freqtrade hyperopt adapter 边界、artifact manifest、结果解析、优化后
StrategyVersion 派生、before/after 对比和前端展示构建。

验收命令：

```bash
python3 scripts/smoke_phase4.py --offline --tmp-dir /tmp/freqtrade-ai-phase4-smoke
```

该命令默认使用临时 SQLite、fixture HyperoptProfile 和 fake Freqtrade hyperopt
executor，不执行真实 Hyperopt，不连接真实交易所，不下载 K 线，不执行 dry-run /
live trading，不连接生产数据库，也不读取或写入真实 API key。

真实 Hyperopt 执行依赖本地已有 `user_data/data`。如果本地缺少目标 pair /
timeframe / timerange 的数据，真实路径必须输出 `BLOCKED`，不得自动下载数据或
伪造成功。

覆盖范围：

- 校验 `HyperoptProfile` schema 和实验变量锁定。
- 通过 fake runner 生成 SUCCESS / FAILED / BLOCKED artifact manifest。
- 解析 fixture Hyperopt result，提取 best params、loss、epoch 和 metrics snapshot。
- 基于 best params 生成优化后 StrategyVersion fixture，并验证 parent lineage、
  `change_summary`、`diff_snapshot` 和 optimized parameter metadata。
- 使用 fixture 原始回测结果、Hyperopt best result、优化后回测结果生成 before/after
  对比。
- 覆盖本地数据缺失 `BLOCKED`、命令返回非零 `FAILED`、manifest 日志脱敏和成功解析路径。
- 可选运行 `npm run build` 验证前端 Phase 4 展示仍可构建。

限制和禁止项：

- 不调用真实 Hyperopt。
- 不连接真实交易所，不下载 K 线。
- 不执行 dry-run、live trading 或真实下单。
- 不读取或写入真实 API key、secret、passphrase。
- 不引入 Redis、Celery、Kafka、RabbitMQ。
- 不修改 Freqtrade 源码。
- 不部署，不进入 Phase 5、Phase 6 或 Phase 7。

## Phase 5 Dry-run / FreqUI Management

Phase 5 Dry-run / FreqUI 运行管理使用离线 smoke 覆盖 DryRunProfile、ENV-only
配置预检、受控 dry-run CLI command、fake runner、artifact manifest、只读状态快照、
FreqUI 只读链接 metadata 和前端构建。

验收命令：

```bash
python3 scripts/smoke_phase5.py --offline --tmp-dir /tmp/freqtrade-ai-phase5-smoke
```

该命令默认使用临时目录、fixture profile、fixture strategy、fake Freqtrade trade
executor 和本地前端 build。不启动真实 dry-run，不连接真实交易所，不下载 K 线，不执行
live trading 或真实下单，也不读取或写入真实 API key、secret、passphrase。

覆盖范围：

- 校验 `DryRunProfile` schema、locked variables 和 `dry_run=true` 边界。
- 生成临时 dry-run config，并验证真实密钥值不会写入配置。
- 通过 fake runner 生成 SUCCESS / FAILED / BLOCKED artifact manifest。
- 解析 `DryRunStatusSnapshot` / `DryRunEvent` 只读状态和事件。
- 验证缺失文件、损坏 JSON、`dry_run=false`、secret-shaped 内容的 fail-closed /
  redaction 行为。
- 验证 FreqUI metadata 只提供 `read-only-link`，无配置时返回 disabled / blocked。
- 写入 smoke summary，明确 safety flags 均保持为 false。
- 默认运行 `npm run build` 验证前端 Phase 5 展示仍可构建。

限制和禁止项：

- 不启动真实 dry-run。
- 不连接真实交易所，不下载 K 线。
- 不执行 live trading 或真实下单。
- 不读取或写入真实 API key、secret、passphrase。
- 不嵌入、不代理、不重写 FreqUI 控制面。
- 不引入 Redis、Celery、Kafka、RabbitMQ。
- 不修改 Freqtrade 源码。
- 不部署，不进入 Phase 6 或 Phase 7。

Phase 5 最终验收还需要运行：

```bash
cd backend && . .venv/bin/activate && pytest
python3 -m compileall backend/app backend/tests scripts
cd frontend && npm run build
git diff --check
```

## Phase 6 Live-candidate Governance Smoke

Phase 6 实盘候选与部署治理使用离线 smoke 覆盖 `LiveCandidateProfile`、
risk preflight、人工审批记录、`DeploymentRecord`、rollback plan 和只读监控摘要。

验收命令：

```bash
python3 scripts/smoke_phase6.py --offline --tmp-dir /tmp/freqtrade-ai-phase6-smoke
```

该命令默认只使用 fixture 和 `--tmp-dir` 下生成的临时治理记录。不启动 Freqtrade，
不连接真实交易所，不下载 K 线，不读取真实密钥，不执行 live trading 或真实下单，
也不执行生产部署。

覆盖范围：

- 校验 `LiveCandidateProfile` 和 offline evidence manifest。
- 验证 passing preflight 只能进入人工审批，不代表部署许可。
- 通过两个人工审批记录解锁 deployment governance record。
- 验证 rollback plan 是创建部署记录的必需输入。
- 解析只读 monitoring summary，确认不暴露控制动作。
- 覆盖缺失风险证据、缺失人工审批、缺失 rollback plan 的 `BLOCKED` 路径。
- 覆盖风险阈值超限的 `FAILED` 路径。
- 写入 smoke summary，明确真实交易、真实连接、真实下单、密钥读取和生产部署均未发生。

限制和禁止项：

- 不启动 live trading。
- 不连接真实交易所，不下载 K 线。
- 不读取或写入真实 API key、secret、passphrase。
- 不提供 start / stop / deploy live 控制。
- 不引入 Redis、Celery、Kafka、RabbitMQ。
- 不修改 Freqtrade 源码。
- 不部署，不进入 Phase 7。

## Phase 8 Local Strategy Lab Acceptance

Phase 8 的验收核心是证明页面、API 和数据库三方一致，而不是只证明 fixture runner
或 smoke 能通过。当前本地 QA 入口是：

```bash
python3 scripts/smoke_phase8.py --offline --tmp-dir /tmp/freqtrade-ai-phase8-smoke
```

Phase 8 必须覆盖：

- 本地后端可以启动。
- 本地前端可以启动。
- 前端可以访问后端 API。
- 核心页面没有白屏。
- 检查流程没有严重 console error。
- 检查流程没有关键 API 404 / 500。
- 页面可以提交策略想法并触发后端生成。
- 策略、策略版本和生成批次写入数据库。
- 生成策略文件写入 approved local runnable directory，并记录文件状态。
- 页面刷新后仍能展示数据库-backed 的策略和版本。
- 页面可以触发本地回测任务。
- 回测任务、回测结果、artifact 引用、失败 / 阻塞原因写入数据库。
- 策略评分和排行榜基于数据库 `BacktestResult` 与 `StrategyVersion`。
- 页面、API response 和数据库查询可以对账。
- fixture、fallback、mock 和 unknown-source 都明确标记，不能冒充核心成功。
- dry-run readiness 基于真实本地检查返回 `READY` 或 `BLOCKED`。
- 受控 dry-run 只能在 readiness、安全和人工确认满足后推进，并且必须保持
  dry-run-only、local-only、secret-redacted。

Phase 8 推荐验证顺序：

1. `#233` 先定义 Local Strategy Lab 流程、状态机、source marker 和阶段验收口径。
2. `#234` 到 `#235` 建立 API/DB 来源契约和 local/dev/test 造数能力。
3. `#236` 到 `#241` 跑通策略生成、策略文件、回测、结果入库和评分主链路。
4. `#242` 到 `#243` 验证页面真实数据展示和 fallback/fixture 防冒充。
5. `#244` 建立页面/API/数据库三方对账 E2E。
6. `#245` 到 `#246` 处理 dry-run readiness 和本地受控 dry-run。
7. `#247` 汇总阶段验收、安全边界和剩余 blocker。

Phase 8 限制和禁止项：

- 不执行真实下单。
- 不启动 live trading。
- 不连接真实交易所。
- 不下载真实 K 线。
- 不操作 production、shared、remote 或 unknown 数据库。
- 不读取、写入或持久化真实 API key、secret、passphrase 或 token。
- 不把密钥写入代码、配置、数据库、日志、文档、Issue、PR、截图或测试数据。
- 不修改 Freqtrade 源码。
- 不实现 Redis、Celery、Kafka、RabbitMQ、worker pool 或生产 queue infrastructure。
- 不实现 deployment executor。
- 不提供 live bot start / stop / deploy 控制。
- 不把 fixture、fallback、mock 或 unknown-source 数据展示成真实数据库成功。

## Phase 9 Operational Readiness

Phase 9 验收核心是证明本地 real-run 证据链不会被 fake、fixture、fallback 或 mock
掩盖。安全默认入口是：

```bash
python3 scripts/phase9_deepseek_single_e2e.py --json
```

该命令默认不调用真实 DeepSeek。它用于检查 ENV-only provider 边界、缺 key fail-closed、
数据库/API/UI evidence shape、source marker 和下一步提示。

真实 DeepSeek 单次调用只能在本地 operator 明确授权并提供本地 ENV key 时运行：

```bash
python3 scripts/phase9_deepseek_single_e2e.py --allow-real-call --json
```

HTTP 写入口还要求本地 ENV 中存在 `FREQTRADE_AI_OPERATOR_TOKEN`，并由本次请求通过
`X-Operator-Token` 提交；每次请求必须使用新的 `Idempotency-Key`。真实 Provider 尝试还必须
显式把 `X-Provider-Authorization` header 设置为单次许可值 `once`，且单次最多生成一个策略。token/key 值不得进入
数据库、日志、API response、报告或浏览器存储。

Phase 9 和当前 refactor/runtime PR 必须覆盖：

- `generation_run`、`strategy`、`strategy_version`、策略文件、回测、评分和页面展示链路。
- 页面、API response 和数据库查询可以对账。
- `database` / `api_aggregate` 只有在包含 `database_ids` 时才能作为核心验收数据。
- `fixture`、`fallback`、`mock`、`unknown` 必须显示为非核心。
- 缺少 key、缺少本地行情、缺少 artifact 或解析失败时返回 `FAILED` / `BLOCKED`，不得伪造成功。
- 核心按钮必须留下 success / failed / BLOCKED 证据，而不是只依赖 console 或短暂 toast。

Phase 9 限制和禁止项：

- 不启动 live trading。
- 不执行真实下单。
- 不连接真实交易所或生产环境。
- 不把真实 key/token/passphrase 写入代码、配置、数据库、日志、页面、报告、Issue 或 PR。
- 不修改 Freqtrade 源码。
- 不实现生产队列、worker pool、deployment executor 或自动实盘调度。

### Phase 9 CI Gate

GitHub Actions 的 `Backend, frontend, and offline smoke` 是当前 hosted merge gate。它使用两个隔离的
PostgreSQL 16 数据库：一个只验证 fresh migration/schema 不漂移，另一个运行 Phase 8 API/DB reconciliation
与 Local Strategy Lab 浏览器契约，避免测试 reset 破坏 migration version。

后端完整 pytest 覆盖 artifact existence、checksum 与 DB ID reconciliation；前端 Node tests 覆盖
source-state、non-core Provider 与稳定 `NOT_RUN` 空态；Playwright 在桌面和移动视口验证确定性 Provider seed
只能进入 non-core diagnostics，同时 PostgreSQL backtest/result/ranking 的核心 database/API 证据仍可见。
完全空 API 必须显示“没有可证明的核心成功结果”，不得宣称核心成功。

CI 同时运行 Phase 7/8 offline smoke。所有 smoke 只使用 CI service、临时目录或受控 fixture；不调用真实
DeepSeek、交易所、Freqtrade live/dry-run、真实订单或生产部署。
