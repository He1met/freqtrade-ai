import { useMvpData } from "../api/useMvpData";
import {
  buildBacktestMatrixSummary,
  formatNumber,
  metricRows,
  reasonText,
  statusClassName,
} from "./backtestDisplay";
import { NONE_TEXT, sourceLabel, statusLabel } from "./display";

export function BacktestRuns() {
  const { data, source, isLoading } = useMvpData();
  const matrixSummary = buildBacktestMatrixSummary(data.backtestRuns, data.backtestTasks);
  const statusEntries = Object.entries(matrixSummary.statusCounts).filter(([, count]) => count > 0);

  return (
    <section className="page">
      <header className="page-header">
        <h1>回测批次</h1>
        <span className="status-pill">{sourceLabel(source, isLoading)}</span>
      </header>
      <section className="matrix-summary" aria-label="回测矩阵摘要">
        <div className="matrix-overview">
          <span className={`run-status ${statusClassName(matrixSummary.status)}`}>
            {statusLabel(matrixSummary.status)}
          </span>
          <div>
            <strong>回测矩阵</strong>
            <span>
              {matrixSummary.completedTasks}/{matrixSummary.totalTasks} 个任务已完成，覆盖{" "}
              {matrixSummary.profileCount} 个配置和 {matrixSummary.strategyCount} 个策略
            </span>
          </div>
        </div>
        <div className="matrix-status-grid">
          {statusEntries.map(([status, count]) => (
            <div className="matrix-status-item" key={status}>
              <span className={`run-status ${statusClassName(status)}`}>{statusLabel(status)}</span>
              <strong>{count}</strong>
            </div>
          ))}
        </div>
        <div className="matrix-range-grid">
          {matrixSummary.metricRanges.map((range) => (
            <div className="matrix-range-item" key={range.label}>
              <span>{range.label}</span>
              <strong>平均 {formatNumber(range.avg, range.suffix)}</strong>
              <em>
                最小 {formatNumber(range.min, range.suffix)} / 最大 {formatNumber(range.max, range.suffix)}
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
                <strong>{statusLabel(entry.status)}</strong>
                <span>
                  {entry.count} 次：{entry.reason}
                </span>
              </div>
            ))
          )}
        </div>
      </section>
      <div className="table-shell">
        <table>
          <thead>
            <tr>
              <th>批次</th>
              <th>策略</th>
              <th>状态</th>
              <th>配置</th>
              <th>任务</th>
              <th>产物</th>
              <th>指标</th>
              <th>结果 JSON</th>
              <th>原因</th>
            </tr>
          </thead>
          <tbody>
            {data.backtestRuns.map((run) => {
              const linkedTask = data.backtestTasks.find((task) => task.runId === run.id);
              const artifact = run.artifactManifest ?? linkedTask?.artifactManifest ?? null;
              const resultPath = artifact?.resultPath ?? linkedTask?.resultPath ?? NONE_TEXT;
              const manifestPath = artifact?.manifestPath ?? NONE_TEXT;
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
                      {statusLabel(run.status)}
                    </span>
                  </td>
                  <td>{run.profileName}</td>
                  <td>
                    {run.completedTaskCount}/{run.requestedTaskCount}
                  </td>
                  <td className="artifact-cell">
                    <span className={`run-status ${statusClassName(artifact?.status ?? run.status)}`}>
                      {statusLabel(artifact?.status ?? null)}
                    </span>
                    <span>manifest：{manifestPath}</span>
                  </td>
                  <td className="metric-summary">
                    {metricRows(run.metrics).map(([label, value]) => (
                      <span key={label}>
                        <strong>{label}</strong>
                        {value}
                      </span>
                    ))}
                  </td>
                  <td className="path-cell">{resultPath}</td>
                  <td className="reason-cell">{reason}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {data.backtestRuns.length === 0 ? (
        <div className="empty-state">暂无回测批次。</div>
      ) : null}
    </section>
  );
}
