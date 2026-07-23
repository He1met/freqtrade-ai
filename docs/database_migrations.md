# PostgreSQL Schema Migration Contract

本项目只对本地开发 PostgreSQL 执行 schema migration；禁止把本流程用于生产、共享环境、
真实交易或 Freqtrade 上游数据库。

## 使用方式

```bash
brew services start postgresql@16
export DATABASE_URL='postgresql+psycopg://freqtrade:change_me@localhost:5432/freqtrade_ai'
make db-backup   # 已有本地开发库时先执行
make db-init
make db-verify
```

`db-init` 使用 `python -m app.db.migrate upgrade`。它创建或验证
`freqtrade_ai_schema_migrations`，以 `backend/app/models` 的 SQLAlchemy metadata 作为当前
唯一 schema 基线。`db-verify` 与 `/readyz` 会检查 version、表、列、外键、unique 和
check constraint；任一不匹配都会 fail closed。

## 旧数据库处理

旧 `001_init.sql` 的 `backtest_runs`、`backtest_tasks`、`backtest_results` 与当前 ORM
不一致，并且遗漏 `debug_mvp_seed_payloads` 等表。升级器只会重建**所有受管表为空**的旧
本地 schema。若发现任何受管表有数据，它会在 DDL 前退出为 BLOCKED；先用 `db-backup`
保存本地副本，再为实际数据制定显式、可审计的数据迁移，不得手工删列或伪造成功。

迁移在 PostgreSQL transaction 内运行：失败不会写入 version，也不会留下部分 schema。
命令和 readiness 输出只显示脱敏 database identity，绝不显示 URL 密码或其他密钥。

## psql / pg_dump 边界

`postgresql+psycopg://` 是 SQLAlchemy 专用 URL。`psql` 和 `pg_dump` 使用 `postgresql://`
形式，并应通过 `PGPASSWORD` 或 `.pgpass` 认证，而不是把密码打印在终端、文档或日志中。
