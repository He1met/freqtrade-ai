import { combineDataSources } from "../api/sourceState";
import { useMvpData } from "../api/useMvpData";
import {
  EmptyState,
  PageHeader,
  StatusBadge,
} from "../components/DisplayPrimitives";
import "../styles/backtests.css";
import { reasonText } from "./backtestDisplay";
import {
  findBacktestResultForTask,
  missingBacktestResultReason,
} from "./backtestResultLookup";
import {
  BacktestResultMetrics,
  BacktestTechnicalDetails,
} from "./BacktestViewParts";
import { FallbackNotice } from "./FallbackNotice";
import { EMPTY_TEXT, displayLoadState } from "./uiCopy";

export function BacktestTasks() {
  const { data, sources, isLoading, error } = useMvpData();
  const source = combineDataSources(sources, ["backtestTasks", "backtestResults"]);

  return (
    <section className="page backtest-page">
      <PageHeader
        description="按任务核对交易参数、执行状态与真实持久化结果。"
        eyebrow="研究与验证"
        status={<StatusBadge label={displayLoadState(isLoading, source)} status={isLoading ? "running" : source} />}
        title="回测任务"
      />
      <FallbackNotice
        context="回测任务、artifact manifest、指标、Result 路径和 stdout/stderr 摘要。"
        error={error}
        isLoading={isLoading}
        source={source}
      />

      {!isLoading && data.backtestTasks.length > 0 ? (
        <div className="table-shell">
          <table className="backtest-desktop-table">
            <colgroup>
              <col className="backtest-col-status-main" />
              <col className="backtest-col-strategy-main" />
              <col className="backtest-col-market-main" />
              <col className="backtest-col-profile-main" />
              <col className="backtest-col-result-main" />
              <col className="backtest-col-details-main" />
            </colgroup>
            <thead>
              <tr>
                <th>状态</th>
                <th>策略</th>
                <th>Pair / Timeframe</th>
                <th>Profile</th>
                <th>真实 BacktestResult 指标</th>
                <th>技术详情</th>
              </tr>
            </thead>
            <tbody>
              {data.backtestTasks.map((task) => {
                const run = data.backtestRuns.find((item) => item.id === task.runId);
                const result = findBacktestResultForTask(data.backtestResults, task.id);
                const reason = reasonText(task.blockedReason, task.failedReason, task.errorMessage);
                const visibleReason =
                  reason === EMPTY_TEXT && !result
                    ? missingBacktestResultReason("任务")
                    : reason;

                return (
                  <tr key={task.id}>
                    <td><StatusBadge showRaw status={task.status} /></td>
                    <td>
                      <span className="backtest-primary-value" title={task.strategyName}>
                        {task.strategyName}
                      </span>
                      <span className="backtest-secondary-value">任务 #{task.id}</span>
                    </td>
                    <td>
                      <span className="backtest-primary-value">{task.pair}</span>
                      <span className="backtest-secondary-value">{task.timeframe}</span>
                    </td>
                    <td>{run?.profileName ?? EMPTY_TEXT}</td>
                    <td>
                      <BacktestResultMetrics
                        result={result}
                        status={task.artifactManifest?.status ?? task.status}
                      />
                    </td>
                    <td>
                      <BacktestTechnicalDetails
                        artifact={task.artifactManifest}
                        configPath={task.configPath}
                        id={task.id}
                        reason={visibleReason}
                        resultPath={result?.resultPath ?? task.resultPath ?? task.artifactManifest?.resultPath ?? null}
                        source={result?.dataSource ?? task.dataSource}
                        status={task.status}
                      />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : null}

      {!isLoading && data.backtestTasks.length === 0 ? (
        <EmptyState
          description="当前没有来自数据库的核心回测任务。缺少前置条件时应先处理 BLOCKED 原因。"
          title="暂无真实回测任务"
        />
      ) : null}
    </section>
  );
}
