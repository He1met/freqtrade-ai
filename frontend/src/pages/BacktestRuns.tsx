import { combineDataSources } from "../api/sourceState";
import { useMvpData } from "../api/useMvpData";
import {
  EmptyState,
  PageHeader,
  StatusBadge,
} from "../components/DisplayPrimitives";
import "../styles/backtests.css";
import {
  buildBacktestMatrixSummary,
  formatMatrixRangeValue,
  matrixStatusLabel,
  reasonText,
  type MatrixDisplayStatus,
} from "./backtestDisplay";
import {
  findBacktestResultForRun,
  missingBacktestResultReason,
} from "./backtestResultLookup";
import {
  BacktestResultMetrics,
  BacktestTechnicalDetails,
} from "./BacktestViewParts";
import { FallbackNotice } from "./FallbackNotice";
import { EMPTY_TEXT, displayLoadState } from "./uiCopy";

export function BacktestRuns() {
  const { data, sources, isLoading, error } = useMvpData();
  const source = combineDataSources(sources, ["backtestRuns", "backtestTasks", "backtestResults"]);
  const matrixSummary = buildBacktestMatrixSummary(
    data.backtestRuns,
    data.backtestTasks,
    data.backtestResults,
  );
  const statusEntries = Object.entries(matrixSummary.statusCounts).filter(([, count]) => count > 0);

  return (
    <section className="page backtest-page">
      <PageHeader
        description="先判断回测是否有真实结果，再查看收益、风险和技术证据。"
        eyebrow="研究与验证"
        status={<StatusBadge label={displayLoadState(isLoading, source)} status={isLoading ? "running" : source} />}
        title="回测批次"
      />
      <FallbackNotice
        context="回测批次、artifact manifest、指标和失败原因摘要。"
        error={error}
        isLoading={isLoading}
        source={source}
      />

      {!isLoading && data.backtestRuns.length > 0 ? (
        <>
          <section className="backtest-matrix-summary" aria-labelledby="backtest-matrix-title">
            <div className="backtest-matrix-heading">
              <div>
                <h2 id="backtest-matrix-title">回测矩阵</h2>
                <p>
                  已完成 {matrixSummary.completedTasks}/{matrixSummary.totalTasks} 个任务，覆盖{" "}
                  {matrixSummary.profileCount} 个 Profile 和 {matrixSummary.strategyCount} 个策略。
                </p>
              </div>
              <StatusBadge
                label={matrixStatusLabel(matrixSummary.status)}
                status={
                  matrixSummary.status === "RESULT_MISSING"
                    ? "not_acceptable"
                    : matrixSummary.status.toLowerCase()
                }
              />
            </div>
            <div className="backtest-status-counts" aria-label="状态分布">
              {statusEntries.map(([status, count]) => (
                <StatusBadge
                  key={status}
                  label={`${matrixStatusLabel(status as MatrixDisplayStatus)}：${count}`}
                  status={status === "RESULT_MISSING" ? "not_acceptable" : status.toLowerCase()}
                />
              ))}
            </div>
            <div className="backtest-summary-grid">
              {matrixSummary.metricRanges.map((range) => (
                <article className="backtest-summary-card" key={range.label}>
                  <span>{range.label}</span>
                  <strong>{formatMatrixRangeValue(range.label, range.avg, range.suffix)}</strong>
                  <em>
                    {formatMatrixRangeValue(range.label, range.min, range.suffix)} 最低 /{" "}
                    {formatMatrixRangeValue(range.label, range.max, range.suffix)} 最高
                  </em>
                </article>
              ))}
            </div>
            {matrixSummary.reasons.length > 0 ? (
              <div className="backtest-reason-summary" aria-label="阻塞与失败摘要">
                {matrixSummary.reasons.map((entry) => (
                  <div key={`${entry.status}:${entry.reason}`}>
                    <StatusBadge label={matrixStatusLabel(entry.status)} status={entry.status.toLowerCase()} />
                    <span>{entry.count} 次：{entry.reason}</span>
                  </div>
                ))}
              </div>
            ) : null}
          </section>

          <div className="table-shell">
            <table className="backtest-desktop-table">
              <colgroup>
                <col className="backtest-col-status-main" />
                <col className="backtest-col-strategy-main" />
                <col className="backtest-col-market-main" />
                <col className="backtest-col-profile-main" />
                <col className="backtest-col-progress-main" />
                <col className="backtest-col-result-main" />
                <col className="backtest-col-details-main" />
              </colgroup>
              <thead>
                <tr>
                  <th>状态</th>
                  <th>策略</th>
                  <th>Pair / Timeframe</th>
                  <th>Profile</th>
                  <th>任务进度</th>
                  <th>真实 BacktestResult 指标</th>
                  <th>技术详情</th>
                </tr>
              </thead>
              <tbody>
                {data.backtestRuns.map((run) => {
                  const linkedTask = data.backtestTasks.find((task) => task.runId === run.id);
                  const result = findBacktestResultForRun(data.backtestResults, run.id);
                  const artifact = run.artifactManifest ?? linkedTask?.artifactManifest ?? null;
                  const recordedReason = reasonText(
                    run.blockedReason ?? linkedTask?.blockedReason ?? null,
                    run.failedReason ?? linkedTask?.failedReason ?? null,
                  );
                  const reason =
                    recordedReason === EMPTY_TEXT && !result
                      ? missingBacktestResultReason("批次")
                      : recordedReason;

                  return (
                    <tr key={run.id}>
                      <td>
                        <StatusBadge showRaw status={run.status} />
                      </td>
                      <td>
                        <span className="backtest-primary-value" title={run.strategyName}>
                          {run.strategyName}
                        </span>
                        <span className="backtest-secondary-value">批次 #{run.id}</span>
                      </td>
                      <td>
                        <span className="backtest-primary-value">{linkedTask?.pair ?? EMPTY_TEXT}</span>
                        <span className="backtest-secondary-value">{linkedTask?.timeframe ?? EMPTY_TEXT}</span>
                      </td>
                      <td>{run.profileName}</td>
                      <td>{run.completedTaskCount}/{run.requestedTaskCount}</td>
                      <td>
                        <BacktestResultMetrics
                          result={result}
                          status={artifact?.status ?? run.status}
                        />
                      </td>
                      <td>
                        <BacktestTechnicalDetails
                          artifact={artifact}
                          configPath={artifact?.configPath ?? linkedTask?.configPath ?? null}
                          id={run.id}
                          reason={reason}
                          resultPath={result?.resultPath ?? artifact?.resultPath ?? linkedTask?.resultPath ?? null}
                          source={result?.dataSource ?? run.dataSource ?? linkedTask?.dataSource}
                          status={run.status}
                        />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </>
      ) : null}

      {!isLoading && data.backtestRuns.length === 0 ? (
        <EmptyState
          description="当前没有来自数据库的核心回测批次。Fixture、fallback 和缺失 ID 的记录不能作为验收结果。"
          title="暂无真实回测批次"
        />
      ) : null}
    </section>
  );
}
