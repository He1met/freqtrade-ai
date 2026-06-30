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
