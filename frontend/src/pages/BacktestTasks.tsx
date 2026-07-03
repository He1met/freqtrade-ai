import { useMvpData } from "../api/useMvpData";
import { metricRows, reasonText, statusClassName, summarizeText } from "./backtestDisplay";
import { NONE_TEXT, sourceLabel, statusLabel } from "./display";

export function BacktestTasks() {
  const { data, source, isLoading } = useMvpData();

  return (
    <section className="page">
      <header className="page-header">
        <h1>回测任务</h1>
        <span className="status-pill">{sourceLabel(source, isLoading)}</span>
      </header>
      <div className="table-shell">
        <table>
          <thead>
            <tr>
              <th>任务</th>
              <th>批次</th>
              <th>策略</th>
              <th>交易对</th>
              <th>周期</th>
              <th>状态</th>
              <th>产物</th>
              <th>指标</th>
              <th>配置</th>
              <th>结果</th>
              <th>原因</th>
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
                    <span className={`run-status ${statusClassName(task.status)}`}>
                      {statusLabel(task.status)}
                    </span>
                  </td>
                  <td className="artifact-cell">
                    <span className={`run-status ${statusClassName(artifactStatus)}`}>
                      {statusLabel(artifactStatus)}
                    </span>
                    <span>返回码：{artifact?.returnCode ?? NONE_TEXT}</span>
                    <span>manifest：{artifact?.manifestPath ?? NONE_TEXT}</span>
                  </td>
                  <td className="metric-summary">
                    {metricRows(task.metrics).map(([label, value]) => (
                      <span key={label}>
                        <strong>{label}</strong>
                        {value}
                      </span>
                    ))}
                  </td>
                  <td className="path-cell">{task.configPath ?? NONE_TEXT}</td>
                  <td className="path-cell">{task.resultPath ?? artifact?.resultPath ?? NONE_TEXT}</td>
                  <td className="reason-cell">
                    {reasonText(task.blockedReason, task.failedReason, task.errorMessage)}
                  </td>
                  <td className="log-cell">
                    <span>stdout：{summarizeText(artifact?.stdout)}</span>
                    <span>stderr：{summarizeText(artifact?.stderr)}</span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {data.backtestTasks.length === 0 ? (
        <div className="empty-state">暂无回测任务。</div>
      ) : null}
    </section>
  );
}
