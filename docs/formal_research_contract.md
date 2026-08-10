# 60-candidate 正式研究契约

本文是正式研究候选集合、生命周期计数和 deployment handoff 的唯一 current 文档定义。
代码、schema 和测试仍是可执行契约；如与本文不一致，保持 fail closed 并通过 GitHub issue
收敛，不使用旧 roadmap、PRD、报告或历史批次补足结论。

## 候选矩阵

一轮完整正式研究固定为 `3 pairs × 2 timeframes × 10 slots = 60 candidates`：

- pairs：`BTC/USDT:USDT`、`ETH/USDT:USDT`、`SOL/USDT:USDT`；
- timeframes：`5m`、`15m`；
- 每个 timeframe 必须有 10 个 timeframe-bound 蓝图/源码 slot；每个 slot 分别投影到三个 pair。

因此源码前置集合是 `5m=10`、`15m=10`，持久化研究单元是 60 条。缺少任一 pair/timeframe
行情 artifact、蓝图、源码、slot、等价性或新鲜所有权证据时，整轮在生成前 fail closed；
不得复制候选、跨 timeframe 覆盖或降低质量门凑满 60 条。

## 生命周期和计数

- `NOT_GENERATED`：前置门禁未通过，`generated/validated/persisted/qualified/rejected = 0`。
  这是未生成，不是 60 条被拒绝。
- `FAILED`：生成或验证流水线失败。若已产生候选证据，必须保留为
  `VALIDATION_FAILED`；不得并入质量拒绝或伪装为完整验证。
- `VALIDATED`：完整报告和持久化完成。60 条候选逐条为 `QUALIFIED` 或 `REJECTED`，
  且批次计数、候选行和 receipt 必须能够对账。
- `VALIDATED` 且 `qualified_count=0`：研究成功完成但无候选进入后续队列；这是合法终态。

报告、数据库和只读 API 必须保留 requested、generated、validated、persisted、qualified、
rejected 六类计数及结构化失败/拒绝原因。未知、超时或缺 receipt 不得显示为 0 或成功。

## 质量与交接

每条研究单元必须保留 load/static、lookahead、费用与滑点、独立 OOS、bull/range/bear、
交易数、评分、数据 lineage 以及 15% drawdown 门的证据。不得为增加活跃度而放宽门槛。

只有持久化的 `status=QUALIFIED` 候选可以被 canonical bridge/deployment review 读取。
`REJECTED`、`VALIDATION_FAILED`、`FAILED`、`NOT_GENERATED` 或证据未知均不得进入该交接。
即使 `QUALIFIED`，也不等于已批准、已部署、已产生自然信号或已下单；后续每一段仍需独立、
新鲜、可审计的 lineage/receipt 和唯一 owner/writer 门禁。

## 不变安全边界

- execution target 固定为 `OKX_DEMO`，`allow_real_funds=false`、`real_orders=false`。
- 研究不授权 credentials、DB/ACL 变更、runtime 控制、grant、手工信号、订单重放或真实资金。
- `NO_ACTION` 是自然信号评估的有效终态，不得强迫产生订单。
- 报告和历史批次是证据，不是 current runtime truth。

实现入口：`backend/app/services/formal_strategy_research.py`、
`scripts/run_strategy_candidate_research.py`、`backend/app/services/strategy_research.py`；
工作计划以[开放 GitHub issues](https://github.com/He1met/freqtrade-ai/issues)为准。
