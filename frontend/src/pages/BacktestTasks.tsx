import { useMvpData } from "../api/useMvpData";
import { metricRows, reasonText, statusClassName, summarizeText } from "./backtestDisplay";

export function BacktestTasks() {
  const { data, source, isLoading } = useMvpData();

  return (
    <section className="page">
      <header className="page-header">
        <h1>Backtest Tasks</h1>
        <span className="status-pill">{isLoading ? "Loading" : source}</span>
      </header>
      <div className="table-shell">
        <table>
          <thead>
            <tr>
              <th>Task</th>
              <th>Run</th>
              <th>Strategy</th>
              <th>Pair</th>
              <th>Timeframe</th>
              <th>Status</th>
              <th>Artifact</th>
              <th>Metrics</th>
              <th>Config</th>
              <th>Result</th>
              <th>Reason</th>
              <th>Stdout/Stderr</th>
            </tr>
          </thead>
          <tbody>
            {data.backtestTasks.map((task) => {
              const artifact = task.artifactManifest;
              const artifactStatus = artifact?.status ?? task.status;
              return (
                <tr key={task.id}>
                  <td>{task.id}</td>
                  <td>{task.runId}</td>
                  <td>{task.strategyName}</td>
                  <td>{task.pair}</td>
                  <td>{task.timeframe}</td>
                  <td>
                    <span className={`run-status ${statusClassName(task.status)}`}>{task.status}</span>
                  </td>
                  <td className="artifact-cell">
                    <span className={`run-status ${statusClassName(artifactStatus)}`}>
                      {artifactStatus}
                    </span>
                    <span>return: {artifact?.returnCode ?? "none"}</span>
                    <span>manifest: {artifact?.manifestPath ?? "none"}</span>
                  </td>
                  <td className="metric-summary">
                    {metricRows(task.metrics).map(([label, value]) => (
                      <span key={label}>
                        <strong>{label}</strong>
                        {value}
                      </span>
                    ))}
                  </td>
                  <td className="path-cell">{task.configPath ?? "none"}</td>
                  <td className="path-cell">{task.resultPath ?? artifact?.resultPath ?? "none"}</td>
                  <td className="reason-cell">
                    {reasonText(task.blockedReason, task.failedReason, task.errorMessage)}
                  </td>
                  <td className="log-cell">
                    <span>stdout: {summarizeText(artifact?.stdout)}</span>
                    <span>stderr: {summarizeText(artifact?.stderr)}</span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {data.backtestTasks.length === 0 ? (
        <div className="empty-state">No backtest tasks found.</div>
      ) : null}
    </section>
  );
}
