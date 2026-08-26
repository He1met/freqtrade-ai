# 测试分级

目标是让个人项目的开发循环保持快速，同时把交易安全和研究可信度放在正确的门槛上。
测试数量不是完成标准；应选择能覆盖本次改动风险的最低充分级别。

## T0：开发循环

每次小改动运行直接相关的测试文件或测试节点，并执行 `git diff --check`。

```bash
make test-dev TEST=tests/test_health.py
make test-dev TEST=tests/test_okx_demo_read_adapter.py::test_name
```

默认使用 `backend/.venv/bin/python`；隔离 worktree 可通过
`BACKEND_PYTHON=/absolute/path/to/compatible/venv/bin/python` 复用兼容环境。

- 只覆盖正在修改的模块和一小组相邻回归。
- 纯文档改动不要求运行 pytest；仍需检查链接、命令和 diff。
- 不连接交易所，不读取私有凭证，不写共享数据库。

## T1：受影响子系统

提交或 PR 前运行受影响子系统的测试文件。多个文件用空格分隔：

```bash
make test-subsystem TESTS="tests/test_canonical_v13_continuous_demo_execution.py tests/test_canonical_v13_continuous_demo_order_writer.py"
make test-frontend
```

- runtime/order writer 改动必须覆盖：Demo-only、`allow_real_funds=false`、幂等、单仓/单挂单、
  reduce-only 平仓、notional/minSz/leverage、rate-limit/backoff、对账和 fail-closed。
- 研究改动必须覆盖 TRAIN/VALIDATION/HOLDOUT 隔离、闭合 K 线因果、数据完整性和失败状态。
- API/UI 改动只运行相关后端文件、相关前端 model/client 测试和必要的单条 Playwright 场景。

## T2：隔离 PostgreSQL

只要改动 SQL、migration、ACL、repository、writer lease、订单/成交/账本/持仓对账，就在临时数据库
运行至少一个直接相关的 PostgreSQL integration test。

```bash
POSTGRES_TEST_URL=postgresql+psycopg://postgres@127.0.0.1:5432/<temporary_db> \
  make test-pg TEST=tests/test_canonical_v13_phase9_postgresql.py::test_name
```

- 必须使用专用临时数据库；禁止指向 `freqtrade_ai_v13`、生产、共享或来源不明的数据库。
- 测试前后由调用方创建和删除临时数据库；测试本身不得重置共享 schema。

## T3：里程碑 / 高风险发布

只有下列情况运行全量 backend、frontend、PostgreSQL、E2E 和 backup/restore：

- 发布里程碑或准备切换 clean release；
- 修改交易风险边界、订单写入、migration/ACL 主干或恢复路径；
- 夜间定期回归；
- T0–T2 暴露跨子系统问题。

```bash
make test-milestone
```

该入口运行本地全量 backend、frontend unit/build、Python compile、diff 和 secret scan。
隔离 PostgreSQL 全套、Playwright E2E 与 backup/restore 按现有 CI/runbook 在本里程碑再运行一次，
不塞进日常开发入口。

`make test` 保留为兼容入口，等价于 `make test-milestone`。普通开发和普通 PR 不应重复运行全量
2000+ 测试；先记录本次选择的级别、命令和结果。

## 永久安全边界

任何测试级别都不得把 fixture/fallback 当真实结果，不得打开真实资金、扩大交易权限、读取或输出
私有凭证，也不得破坏 HOLDOUT 隔离。缺失、过期或无法证明的状态继续返回 `BLOCKED` / `UNKNOWN` /
`NO_OP`，不能为了让测试通过而放宽。
