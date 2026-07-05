import { useMvpData } from "../api/useMvpData";
import { metricRows, reasonText, statusClassName, summarizeText } from "./backtestDisplay";
import { FallbackNotice } from "./FallbackNotice";
import { SourceMarker } from "./SourceMarker";
import { EMPTY_TEXT, displayLoadState, displayStatus } from "./uiCopy";

export function BacktestTasks() {
  const { data, source, isLoading, error } = useMvpData();

  return (
    <section className="page">
      <header className="page-header">
        <h1>回测任务</h1>
        <span className="status-pill">{displayLoadState(isLoading, source)}</span>
      </header>
      <FallbackNotice
        context="回测任务、artifact manifest、指标、Result 路径和 stdout/stderr 摘要。"
        error={error}
        isLoading={isLoading}
        source={source}
      />
      <div className="table-shell backtest-table-shell">
        <table>
          <thead>
            <tr>
              <th>任务</th>
              <th>批次</th>
              <th>策略</th>
              <th>Pair</th>
              <th>Timeframe</th>
              <th>状态</th>
              <th>Artifact</th>
              <th>指标</th>
              <th>Config</th>
              <th>Result</th>
              <th>数据来源</th>
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
                      {displayStatus(task.status)}
                    </span>
                  </td>
                  <td className="artifact-cell">
                    <span className={`run-status ${statusClassName(artifactStatus)}`}>
                      {displayStatus(artifactStatus)}
                    </span>
                    <span>return：{artifact?.returnCode ?? EMPTY_TEXT}</span>
                    <span title={artifact?.manifestPath ?? EMPTY_TEXT}>
                      manifest：{artifact?.manifestPath ?? EMPTY_TEXT}
                    </span>
                  </td>
                  <td className="metric-summary">
                    {metricRows(task.metrics).map(([label, value]) => (
                      <span key={label}>
                        <strong>{label}</strong>
                        {value}
                      </span>
                    ))}
                  </td>
                  <td className="path-cell" title={task.configPath ?? EMPTY_TEXT}>
                    {task.configPath ?? EMPTY_TEXT}
                  </td>
                  <td className="path-cell" title={task.resultPath ?? artifact?.resultPath ?? EMPTY_TEXT}>
                    {task.resultPath ?? artifact?.resultPath ?? EMPTY_TEXT}
                  </td>
                  <td className="source-cell">
                    <SourceMarker source={task.dataSource} />
                  </td>
                  <td
                    className="reason-cell"
                    title={reasonText(task.blockedReason, task.failedReason, task.errorMessage)}
                  >
                    {reasonText(task.blockedReason, task.failedReason, task.errorMessage)}
                  </td>
                  <td className="log-cell">
                    <span title={artifact?.stdout ?? EMPTY_TEXT}>stdout: {summarizeText(artifact?.stdout)}</span>
                    <span title={artifact?.stderr ?? EMPTY_TEXT}>stderr: {summarizeText(artifact?.stderr)}</span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {data.backtestTasks.length === 0 ? (
        <div className="empty-state">暂无 database-backed 回测任务；缺少前置条件时应显示 BLOCKED 原因。</div>
      ) : null}
    </section>
  );
}
