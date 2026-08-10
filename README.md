# Freqtrade AI

Freqtrade AI 是建立在 Freqtrade 外层的策略研发、验证、治理与证据管理系统；
它不重新实现 Freqtrade，也不把研究结果自动解释为可交易策略。

项目负责策略蓝图与代码生成、正式研究批次、独立验证、版本与评分、受控的
OKX 模拟盘链路，以及可审计的运行证据。Freqtrade 继续负责行情、回测和交易所适配。

## 安全边界

- 当前唯一允许的执行目标是 `OKX_DEMO`；`allow_real_funds=false`、
  `real_orders=false` 必须保持成立。
- 默认 fail closed。缺少所有权、数据、receipt、lineage、风控、唯一 writer 或对账证据时，
  结论是 `BLOCKED`、`FAILED`、`NOT_GENERATED` 或未知，不得补写成功。
- 研究、`QUALIFIED`、批准、部署、自然信号、订单是不同生命周期；前一阶段成功不自动授权后一阶段。
- 不制造或重放信号/订单，不从历史报告、HTTP 200、PID、页面快照或已合并 PR 推断当前可运行。
- `OKX_LIVE`、真实资金、真实订单、凭据、生产部署和扩大数据库写权限均不在本入口授权范围内。

## 权威入口

- [文档导航](docs/index.md)：唯一 current 文档入口。
- [当前工作计划](https://github.com/He1met/freqtrade-ai/issues)：以开放 GitHub issues
  及其 `roadmap/current`、`roadmap/next`、`roadmap/long-term` 标签为准。
- [60-candidate 正式研究契约](docs/formal_research_contract.md)：正式候选集合、状态与
  `QUALIFIED`-only 交接的单一文档定义。
- [本地 runtime / worker 操作说明](docs/phase9_db_backed_worker.md) 与
  [OKX Demo E2E 验收](docs/okx_demo_e2e.md)：运行核对和 fail-closed 验收入口。
- [历史 roadmap](docs/roadmap.md)：仅用于追溯阶段背景，不代表当前计划。

阶段验收、ADR 和 `reports/` 下带 receipt 的报告均保留为历史证据。它们证明特定时间、
提交和环境中的事实，不替代当前 GitHub issue、runtime verification 或新鲜 receipt。
