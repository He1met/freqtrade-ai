# 独立 OOS / Walk-forward 验证矩阵

`StrategyValidationMatrixService` 是研究结果进入候选晋级前的 fail-closed
边界。它不把一个 `BacktestResult` 的 trades 切片解释成 OOS 或
walk-forward。

固定流程如下：

1. `declare` 在执行前保存一个 immutable plan：同一
   `StrategyVersion`、一个 OOS 窗口、至少三个互不重叠且覆盖
   `bull` / `bear` / `range` 的 walk-forward 窗口，以及每窗
   market-data digest。
2. `prepare_runs` 复用 `LocalBacktestTriggerService`，为每窗创建独立
   `BacktestRun` 和 `BacktestTask`。确定性的 profile identity 允许进程在
   trigger 已提交但关联尚未写回时恢复，而不会重复创建 run。
3. 现有 Freqtrade runner 和 artifact ingest 执行每窗任务。SUCCESS
   manifest 保存 `execution_id`、manifest checksum、config/result/strategy/
   market-data checksums 和 provider lineage。
4. `evaluate` 只接受四组独立且成功的 Run/Task/Result。任一窗口缺失、
   重叠、复用、fixture/offline/source unknown、收益或交易数不达标、
   artifact tamper 或 lineage 漂移，整个 plan 变为 `BLOCKED`。
5. 全部窗口通过后，聚合证据及 digest 持久化到
   `strategy_validation_plans`，并将受 plan ID/digest 约束的
   `promotion_evidence.validation_matrix` 绑定到主回测结果。

## 与 #570 的集成边界

#570 的 research/full-chain 编排应在 BACKTEST/SCORING 后调用本服务：
先准备并执行所有验证任务，再等待 `StrategyValidationPlan.status=PASSED`，
最后才进入 `promotion_candidate_digest`。本改动不修改
`research_full_chain_orchestrator.py`、`research_jobs` 恢复逻辑或
full-chain 原子审批；旧的单结果切片证据会被晋级门直接拒绝。

单元测试中的 fixture 只验证机械合同，不能作为真实 provider 或执行门的
验收证据。
