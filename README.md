# Freqtrade AI

Freqtrade AI 是建立在 Freqtrade 外层的 AI 策略研发管理系统，不重新开发量化交易框架。

Freqtrade 继续负责行情数据下载、策略加载、回测执行、Hyperopt、Dry-run / Live、FreqUI / REST API / Telegram 和交易所接入。本项目负责 AI 策略生成、策略 JSON 蓝图管理、策略代码生成、策略版本管理、批量回测任务管理、回测结果归档、策略评分排行榜、项目进度管理和 FreqUI 快捷入口。

## Phase 0 范围

Phase 0 只初始化项目治理与工程骨架：

- FastAPI 后端骨架和 `/health` 接口
- React + Vite + TypeScript 前端骨架和基础页面路由
- Freqtrade Adapter 占位层
- PostgreSQL 第一阶段 8 张核心表迁移草案
- YAML / ENV / 本地文件系统的存储边界
- GitHub Project、Issue、PR、Milestone 管理规则

Phase 0 不做策略生成、不执行回测、不调用交易所 API、不做实盘或模拟盘交易、不引入 Redis / Celery / Kafka / RabbitMQ。

## Phase 1 离线 MVP 状态

Phase 1 离线 MVP 已完成验收。当前闭环可在本地离线模式完成：

- fake provider 生成策略蓝图。
- 受控目录写入生成策略文件。
- fixture runner 生成 Freqtrade 形状的回测结果。
- 后端解析回测摘要、保存结果、计算评分并输出排行榜。
- 前端 MVP 页面可构建并展示核心数据。

验收命令：

```bash
python3 scripts/smoke_mvp.py --offline --tmp-dir /tmp/freqtrade-ai-smoke
```

完整验收状态见 [phase1_acceptance.md](docs/phase1_acceptance.md)。

当前限制：

- 策略生成使用 fake provider，不调用真实 LLM。
- 回测执行使用 fixture runner，不执行真实 Freqtrade CLI。
- 验收数据库使用临时 SQLite，不连接生产数据库。
- 不支持 dry-run / live trading。
- 不连接真实交易所，不下载真实行情，不执行真实下单。
- 不引入 Redis / Celery / Kafka / RabbitMQ。
- 不修改 Freqtrade 源码。

## Phase 2 策略研发增强状态

Phase 2 策略研发增强已完成验收。当前已具备：

- StrategyBlueprint schema v2 与严格校验。
- 策略代码静态检查与安全审查。
- 策略失败原因归档与查询。
- 策略版本 Diff 与 lineage 记录。
- ENV-only 的真实 LLM StrategyBlueprintProvider 边界，默认测试路径仍使用 fake/mock。
- Phase 2 策略质量评分拆解与淘汰原因。
- 前端展示失败原因、校验错误、版本 Diff 和 Ranking 评分拆解。
- 离线 Phase 2 smoke 验收脚本。

Phase 2 smoke 命令：

```bash
python3 scripts/smoke_phase2.py --offline --tmp-dir /tmp/freqtrade-ai-phase2-smoke
```

完整验收状态见 [phase2_acceptance.md](docs/phase2_acceptance.md)。

当前限制：

- 不支持 dry-run / live trading。
- 不做 Hyperopt。
- 不连接真实交易所，不下载真实 K 线，不执行真实下单。
- 默认 smoke 不调用真实 LLM，不读取真实 LLM API key。
- 真实 Freqtrade CLI spike 依赖本地已有 `user_data/data` 行情数据；缺少数据时应明确
  输出 `BLOCKED`，不能伪造成功。
- 不引入 Redis / Celery / Kafka / RabbitMQ。
- 不修改 Freqtrade 源码。

## Phase 3 回测体系增强状态

Phase 3 回测体系增强已完成验收。当前已具备：

- 本地行情数据 catalog 与数据质量状态。
- BacktestProfile schema v2 与实验变量锁定。
- 真实 Freqtrade backtesting artifact manifest。
- 回测结果指标扩展与版本兼容解析。
- 批量回测矩阵执行与 fail-closed 聚合。
- baseline 对比与重复性检查。
- 前端展示 BacktestRun artifact、增强指标和 Backtest Matrix 摘要。
- 离线 Phase 3 smoke 验收脚本。

Phase 3 smoke 命令：

```bash
python3 scripts/smoke_phase3.py --offline --tmp-dir /tmp/freqtrade-ai-phase3-smoke
```

完整验收状态见 [phase3_acceptance.md](docs/phase3_acceptance.md)。

当前限制：

- 只使用本地已有 `user_data/data` 行情数据，缺少数据时必须明确 `BLOCKED`。
- 不连接真实交易所，不下载真实 K 线，不执行真实下单。
- 不支持 dry-run / live trading。
- 不做 Hyperopt。
- 不提交真实 API key、secret、passphrase。
- 不引入 Redis / Celery / Kafka / RabbitMQ。
- 不修改 Freqtrade 源码。

## Phase 4 Hyperopt 参数优化状态

Phase 4 Hyperopt 参数优化已完成验收。当前已具备：

- HyperoptProfile schema 与优化变量锁定。
- 受控 Freqtrade `hyperopt` CLI runner 边界。
- Hyperopt artifact manifest 与 fail-closed 结果归档。
- Freqtrade Hyperopt result JSON 解析。
- 基于 best params 生成优化后 StrategyVersion。
- 优化前后策略表现对比。
- 前端展示 Hyperopt runs、best params 和 before/after 对比。
- 离线 Phase 4 smoke 验收脚本。

Phase 4 smoke 命令：

```bash
python3 scripts/smoke_phase4.py --offline --tmp-dir /tmp/freqtrade-ai-phase4-smoke
```

完整验收状态见 [phase4_acceptance.md](docs/phase4_acceptance.md)。

当前限制：

- 不支持 dry-run / live trading。
- 不连接真实交易所，不下载真实 K 线，不执行真实下单。
- 不提交真实 API key、secret、passphrase。
- 不引入 Redis / Celery / Kafka / RabbitMQ。
- 不修改 Freqtrade 源码。

## Phase 5 Dry-run / FreqUI 运行管理状态

Phase 5 Dry-run / FreqUI 运行管理已完成最终验收。当前已具备：

- Dry-run / FreqUI 安全边界与执行计划。
- Freqtrade dry-run 本地前置条件和风险预检。
- DryRunProfile schema 与运行变量锁定。
- 受控 Freqtrade dry-run CLI command 构造。
- Dry-run 临时配置生成和 ENV-only 密钥预检。
- Dry-run artifact manifest 与状态归档。
- Dry-run 只读状态快照与事件解析。
- FreqUI 入口配置与只读链接边界。
- Dry-run / FreqUI 运行管理前端展示。
- PR #127 runtime API 与 fallback 契约拆分决策。
- 离线 Phase 5 smoke 验收脚本。

Phase 5 smoke 命令：

```bash
python3 scripts/smoke_phase5.py --offline --tmp-dir /tmp/freqtrade-ai-phase5-smoke
```

完整验收状态见 [phase5_acceptance.md](docs/phase5_acceptance.md)。

当前限制：

- 不做 live trading，不执行真实下单。
- 不连接真实交易所，不下载真实 K 线。
- 真实密钥仅允许来自 ENV，且不得写入代码、YAML、数据库、日志、报告、Issue 或 PR。
- 本项目只提供 FreqUI 只读链接和状态展示边界，不嵌入、不代理、不重写 FreqUI 控制面。
- 不引入 Redis / Celery / Kafka / RabbitMQ 或部署基础设施。
- Phase 6 实盘候选和部署管理必须另行规划，不能由 Phase 5 自动进入。

## Phase 6 实盘候选与部署治理状态

Phase 6 实盘候选与部署治理已完成验收。当前已具备：

- 实盘候选与部署治理设计方案。
- `LiveCandidateProfile` schema 与准入条件锁定。
- 实盘候选风险检查清单与 fail-closed 预检。
- 人工审批记录与状态机。
- `DeploymentRecord` schema 与 rollback plan。
- 只读运行监控与告警摘要 DTO。
- 实盘候选审批与部署记录只读页面。
- 离线 Phase 6 governance smoke 验收脚本。

Phase 6 smoke 命令：

```bash
python3 scripts/smoke_phase6.py --offline --tmp-dir /tmp/freqtrade-ai-phase6-smoke
```

完整验收状态见 [phase6_acceptance.md](docs/phase6_acceptance.md)，设计边界见
[phase6_live_candidate_plan.md](docs/phase6_live_candidate_plan.md)。

当前限制：

- 不执行真实下单，不启动 live trading。
- 不连接真实交易所，不下载真实 K 线。
- 不提交真实 API key、secret、passphrase。
- 不把密钥写入代码、配置、数据库、日志或文档。
- 不修改 Freqtrade 源码。
- 不引入 Redis / Celery / Kafka / RabbitMQ。
- 不做生产部署，不绕过人工审批，不实现自动启动 live bot。
- 不提供 start / stop / deploy live 控制，不实现 deployment executor。
- Phase 7 必须另行规划，不能由 Phase 6 自动进入。

## Phase 7 工程化升级与规模化运行状态

Phase 7 工程化升级与规模化运行已完成最终验收。当前已具备：

- Runtime read-only API contract。
- Operator status API 与本地诊断入口。
- Audit log schema 与 governance event 归档。
- GitHub Actions CI 基线。
- Secret scanning 与配置安全检查。
- Worker / Queue 架构设计方案，仅设计，不实现队列基础设施。
- Operator Dashboard 只读展示系统状态、fallback 状态、smoke 状态和
  artifact 链接。
- 离线 Phase 7 engineering smoke 验收脚本。

Phase 7 smoke 命令：

```bash
python3 scripts/smoke_phase7.py --offline --tmp-dir /tmp/freqtrade-ai-phase7-smoke
```

完整验收状态见 [phase7_acceptance.md](docs/phase7_acceptance.md)，工程规划见
[phase7_engineering_plan.md](docs/phase7_engineering_plan.md)，Worker / Queue 设计边界见
[phase7_worker_queue_design.md](docs/phase7_worker_queue_design.md)。

当前限制：

- 不执行真实下单，不启动 live trading。
- 不连接真实交易所，不下载真实 K 线。
- 不提交真实 API key、secret、passphrase。
- 不把密钥写入代码、配置、数据库、日志或文档。
- 不修改 Freqtrade 源码。
- 不实现 Redis / Celery / Kafka / RabbitMQ、worker pool 或生产 queue
  infrastructure。
- 不做生产部署，不绕过人工审批，不实现自动启动 live bot。
- 不提供 start / stop / deploy live 控制，不实现 deployment executor。
- Phase 8 必须另行规划，不能由 Phase 7 自动进入。

## 启动后端

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

健康检查：

```bash
curl http://127.0.0.1:8000/health
```

## 启动前端

```bash
cd frontend
npm install
npm run dev
```

默认访问 Vite 输出的本地地址，通常是 `http://127.0.0.1:5173`。

## 初始化数据库

```bash
docker compose up -d postgres
psql "postgresql://freqtrade:change_me@localhost:5432/freqtrade_ai" -f db/migrations/001_init.sql
```

生产或共享环境请先设置真实 `.env`，不要提交真实密钥。

## 存储原则

- PostgreSQL 保存长期结构化数据。
- YAML 保存静态配置。
- ENV 保存数据库密码、交易所密钥和模型 API Key。
- 本地文件系统保存 Freqtrade 数据文件、策略文件、回测报告和日志。

详见 [storage_policy.md](docs/storage_policy.md) 和 [database_design_phase1.md](docs/database_design_phase1.md)。
