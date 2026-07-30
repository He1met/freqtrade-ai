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

每次 `promotion_candidate_digest` 都会清空 ORM identity cache 后从数据库
重新读取 plan/window/run/task/result，并重新读取 manifest、config、
result、strategy 与 market-data 文件计算 checksum。`status=PASSED` 不是
快照缓存，也不能跳过重验；PASSED 后任何文件或数据库 lineage 漂移都会
阻断晋级。

主回测的 Run/Task/Result 以及 timerange 均不得被 OOS/WF 窗口复用或
重叠。artifact ingest 会保存由服务端基于 manifest checksum 和数据库
identity 生成的 receipt；仅在 metadata 中手造 `ingest_source=freqtrade`
不能通过。

`bull` / `bear` / `range` 不是调用者提供的事实。调用者只能预声明希望
覆盖的 regime slot；执行后系统使用 `window-close-return-v1` 从持久化
market-data 文件计算首末 close return，同时保存 source artifacts、
algorithm、parameters、market-data checksum 与 evidence digest。

PostgreSQL 对 plan/window identity 提供不可变 trigger。运行角色只能更新
状态和执行证据列，不能更新 plan digest、窗口 timerange、预声明 regime
或 market-data digest；`execution_id` 具有全表唯一约束。

## 与 #570 的集成边界

#570 的 research/full-chain 编排应在 BACKTEST/SCORING 后调用本服务：
先准备并执行所有验证任务，再等待 `StrategyValidationPlan.status=PASSED`，
最后才进入 `promotion_candidate_digest`。本改动不修改
`research_full_chain_orchestrator.py`、`research_jobs` 恢复逻辑或
full-chain 原子审批；旧的单结果切片证据会被晋级门直接拒绝。

单元测试中的 fixture 只验证机械合同，不能作为真实 provider 或执行门的
验收证据。
