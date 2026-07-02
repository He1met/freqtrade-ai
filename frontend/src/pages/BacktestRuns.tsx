import { useMvpData } from "../api/useMvpData";
import {
  buildBacktestMatrixSummary,
  formatNumber,
  metricRows,
  reasonText,
  statusClassName,
} from "./backtestDisplay";

export function BacktestRuns() {
  const { data, source, isLoading } = useMvpData();
  const matrixSummary = buildBacktestMatrixSummary(data.backtestRuns, data.backtestTasks);
  const statusEntries = Object.entries(matrixSummary.statusCounts).filter(([, count]) => count > 0);

  return (
    <section className="page">
      <header className="page-header">
        <h1>Backtest Runs</h1>
        <span className="status-pill">{isLoading ? "Loading" : source}</span>
      </header>
      <section className="matrix-summary" aria-label="Backtest Matrix summary">
        <div className="matrix-overview">
          <span className={`run-status ${statusClassName(matrixSummary.status)}`}>{matrixSummary.status}</span>
          <div>
            <strong>Backtest Matrix</strong>
            <span>
              {matrixSummary.completedTasks}/{matrixSummary.totalTasks} tasks completed across{" "}
              {matrixSummary.profileCount} profiles and {matrixSummary.strategyCount} strategies
            </span>
          </div>
        </div>
        <div className="matrix-status-grid">
          {statusEntries.map(([status, count]) => (
            <div className="matrix-status-item" key={status}>
              <span className={`run-status ${statusClassName(status)}`}>{status}</span>
              <strong>{count}</strong>
            </div>
          ))}
        </div>
        <div className="matrix-range-grid">
          {matrixSummary.metricRanges.map((range) => (
            <div className="matrix-range-item" key={range.label}>
              <span>{range.label}</span>
              <strong>{formatNumber(range.avg, range.suffix)} avg</strong>
              <em>
                {formatNumber(range.min, range.suffix)} min / {formatNumber(range.max, range.suffix)} max
              </em>
            </div>
          ))}
        </div>
        <div className="matrix-reasons">
          {matrixSummary.reasons.length === 0 ? (
            <span>No blocked or failed reasons.</span>
          ) : (
            matrixSummary.reasons.map((entry) => (
              <div className="reason-line" key={`${entry.status}:${entry.reason}`}>
                <strong>{entry.status}</strong>
                <span>
                  {entry.count}x {entry.reason}
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
              <th>Run</th>
              <th>Strategy</th>
              <th>Status</th>
              <th>Profile</th>
              <th>Tasks</th>
              <th>Artifact</th>
              <th>Metrics</th>
              <th>Result JSON</th>
              <th>Reason</th>
            </tr>
          </thead>
          <tbody>
            {data.backtestRuns.map((run) => {
              const linkedTask = data.backtestTasks.find((task) => task.runId === run.id);
              const artifact = run.artifactManifest ?? linkedTask?.artifactManifest ?? null;
              const resultPath = artifact?.resultPath ?? linkedTask?.resultPath ?? "none";
              const manifestPath = artifact?.manifestPath ?? "none";
              const reason = reasonText(
                run.blockedReason ?? linkedTask?.blockedReason ?? null,
                run.failedReason ?? linkedTask?.failedReason ?? null,
              );

              return (
                <tr key={run.id}>
                  <td>{run.id}</td>
                  <td>{run.strategyName}</td>
                  <td>
                    <span className={`run-status ${statusClassName(run.status)}`}>{run.status}</span>
                  </td>
                  <td>{run.profileName}</td>
                  <td>
                    {run.completedTaskCount}/{run.requestedTaskCount}
                  </td>
                  <td className="artifact-cell">
                    <span className={`run-status ${statusClassName(artifact?.status ?? run.status)}`}>
                      {artifact?.status ?? "none"}
                    </span>
                    <span>manifest: {manifestPath}</span>
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
        <div className="empty-state">No backtest runs found.</div>
      ) : null}
    </section>
  );
}
