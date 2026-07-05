import { useMvpData } from "../api/useMvpData";
import {
  buildBacktestMatrixSummary,
  formatNumber,
  metricRows,
  reasonText,
  statusClassName,
} from "./backtestDisplay";
import { FallbackNotice } from "./FallbackNotice";
import { SourceMarker } from "./SourceMarker";
import { EMPTY_TEXT, displayLoadState, displayStatus } from "./uiCopy";

export function BacktestRuns() {
  const { data, source, isLoading, error } = useMvpData();
  const matrixSummary = buildBacktestMatrixSummary(data.backtestRuns, data.backtestTasks);
  const statusEntries = Object.entries(matrixSummary.statusCounts).filter(([, count]) => count > 0);

  return (
    <section className="page">
      <header className="page-header">
        <h1>回测批次</h1>
        <span className="status-pill">{displayLoadState(isLoading, source)}</span>
      </header>
      <FallbackNotice
        context="回测批次、artifact manifest、指标和失败原因摘要。"
        error={error}
        isLoading={isLoading}
        source={source}
      />
      <section className="matrix-summary" aria-label="回测矩阵摘要">
        <div className="matrix-overview">
          <span className={`run-status ${statusClassName(matrixSummary.status)}`}>
            {displayStatus(matrixSummary.status)}
          </span>
          <div>
            <strong>回测矩阵</strong>
            <span>
              已完成 {matrixSummary.completedTasks}/{matrixSummary.totalTasks} 个任务，覆盖{" "}
              {matrixSummary.profileCount} 个 profile 和 {matrixSummary.strategyCount} 个策略
            </span>
          </div>
        </div>
        <div className="matrix-status-grid" role="list">
          {statusEntries.map(([status, count], index) => (
            <div
              aria-label={`${displayStatus(status)}：${count} 个`}
              className="matrix-status-item"
              key={status}
              role="listitem"
            >
              <span className={`run-status ${statusClassName(status)}`}>{displayStatus(status)}</span>
              <span className="status-count-separator" aria-hidden="true">
                ：
              </span>
              <strong>{count}</strong>
              {index < statusEntries.length - 1 ? <span className="status-text-gap"> </span> : null}
            </div>
          ))}
        </div>
        <div className="matrix-range-grid">
          {matrixSummary.metricRanges.map((range) => (
            <div className="matrix-range-item" key={range.label}>
              <span>{range.label}</span>
              <strong>{formatNumber(range.avg, range.suffix)} 平均</strong>
              <em>
                {formatNumber(range.min, range.suffix)} 最小 / {formatNumber(range.max, range.suffix)} 最大
              </em>
            </div>
          ))}
        </div>
        <div className="matrix-reasons">
          {matrixSummary.reasons.length === 0 ? (
            <span>暂无阻塞或失败原因。</span>
          ) : (
            matrixSummary.reasons.map((entry) => (
              <div className="reason-line" key={`${entry.status}:${entry.reason}`}>
                <strong>{displayStatus(entry.status)}</strong>
                <span>
                  {entry.count} 次：{entry.reason}
                </span>
              </div>
            ))
          )}
        </div>
      </section>
      <div className="table-shell backtest-table-shell">
        <table>
          <thead>
            <tr>
              <th>批次</th>
              <th>策略</th>
              <th>状态</th>
              <th>Profile</th>
              <th>任务</th>
              <th>Artifact</th>
              <th>指标</th>
              <th>Result JSON</th>
              <th>数据来源</th>
              <th>原因</th>
            </tr>
          </thead>
          <tbody>
            {data.backtestRuns.map((run) => {
              const linkedTask = data.backtestTasks.find((task) => task.runId === run.id);
              const artifact = run.artifactManifest ?? linkedTask?.artifactManifest ?? null;
              const resultPath = artifact?.resultPath ?? linkedTask?.resultPath ?? EMPTY_TEXT;
              const manifestPath = artifact?.manifestPath ?? EMPTY_TEXT;
              const reason = reasonText(
                run.blockedReason ?? linkedTask?.blockedReason ?? null,
                run.failedReason ?? linkedTask?.failedReason ?? null,
              );

              return (
                <tr key={run.id}>
                  <td>{run.id}</td>
                  <td>{run.strategyName}</td>
                  <td>
                    <span className={`run-status ${statusClassName(run.status)}`}>
                      {displayStatus(run.status)}
                    </span>
                  </td>
                  <td>{run.profileName}</td>
                  <td>
                    {run.completedTaskCount}/{run.requestedTaskCount}
                  </td>
                  <td className="artifact-cell">
                    <span className={`run-status ${statusClassName(artifact?.status ?? run.status)}`}>
                      {displayStatus(artifact?.status ?? run.status)}
                    </span>
                    <span title={manifestPath}>manifest：{manifestPath}</span>
                  </td>
                  <td className="metric-summary">
                    {metricRows(run.metrics).map(([label, value]) => (
                      <span key={label}>
                        <strong>{label}</strong>
                        {value}
                      </span>
                    ))}
                  </td>
                  <td className="path-cell" title={resultPath}>
                    {resultPath}
                  </td>
                  <td className="source-cell">
                    <SourceMarker source={run.dataSource ?? linkedTask?.dataSource} />
                  </td>
                  <td className="reason-cell" title={reason}>
                    {reason}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {data.backtestRuns.length === 0 ? (
        <div className="empty-state">暂无 database-backed 回测批次；fixture/fallback 不能作为真实验收。</div>
      ) : null}
    </section>
  );
}
