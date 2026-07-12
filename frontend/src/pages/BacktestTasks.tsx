import { combineDataSources } from "../api/sourceState";
import type { DataSourceTraceSummary } from "../api/types";
import { useMvpData } from "../api/useMvpData";
import {
  metricRows,
  reasonText,
  statusClassName,
  summarizeText,
} from "./backtestDisplay";
import {
  emptyBacktestMetrics,
  findBacktestResultForTask,
  missingBacktestResultReason,
} from "./backtestResultLookup";
import { FallbackNotice } from "./FallbackNotice";
import { EMPTY_TEXT, displayLoadState, displayStatus } from "./uiCopy";

function formatRecord(record: Record<string, number | string> | undefined): string {
  const entries = Object.entries(record ?? {});
  return entries.length > 0 ? entries.map(([key, value]) => `${key}: ${value}`).join(", ") : EMPTY_TEXT;
}

function compactSourceTitle(source: DataSourceTraceSummary | undefined): string {
  if (!source) {
    return "Source metadata was not provided.";
  }
  return [
    `source_type: ${source.sourceType}`,
    `core_data: ${source.coreData}`,
    `database_ids: ${formatRecord(source.databaseIds)}`,
    `artifact_refs: ${formatRecord(source.artifactRefs)}`,
    `detail: ${source.sourceDetail}`,
    source.blockedReason ? `blocked: ${source.blockedReason}` : null,
  ]
    .filter(Boolean)
    .join(" | ");
}

function BacktestSourceSummary({ source }: { source: DataSourceTraceSummary | undefined }) {
  const databaseCount = Object.keys(source?.databaseIds ?? {}).length;
  const artifactCount = Object.keys(source?.artifactRefs ?? {}).length;
  return (
    <div
      className="backtest-source-summary"
      data-core-source={source?.coreData === true ? "true" : "false"}
      title={compactSourceTitle(source)}
    >
      <div className="backtest-source-heading">
        <strong>{source?.sourceType ?? "unknown"}</strong>
        <span>{source?.coreData ? "core" : "non-core"}</span>
      </div>
      <span>{source?.blockedReason ?? source?.sourceDetail ?? "Source metadata was not provided."}</span>
      <em>
        db {databaseCount} / artifacts {artifactCount}
      </em>
    </div>
  );
}

function CompactPath({ label, value }: { label?: string; value: string }) {
  return (
    <span className="compact-path" title={value}>
      {label ? `${label}: ` : ""}
      {value}
    </span>
  );
}

export function BacktestTasks() {
  const { data, sources, isLoading, error } = useMvpData();
  const source = combineDataSources(sources, ["backtestTasks", "backtestResults"]);

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
          <colgroup>
            <col className="backtest-col-id" />
            <col className="backtest-col-id" />
            <col className="backtest-col-strategy" />
            <col className="backtest-col-pair" />
            <col className="backtest-col-timeframe" />
            <col className="backtest-col-status" />
            <col className="backtest-col-artifact" />
            <col className="backtest-col-metrics" />
            <col className="backtest-col-path" />
            <col className="backtest-col-path" />
            <col className="backtest-col-source" />
            <col className="backtest-col-reason" />
            <col className="backtest-col-log" />
          </colgroup>
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
              const result = findBacktestResultForTask(data.backtestResults, task.id);
              const recordedReason = reasonText(task.blockedReason, task.failedReason, task.errorMessage);
              const reason = recordedReason === EMPTY_TEXT && !result ? missingBacktestResultReason("任务") : recordedReason;
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
                    <CompactPath label="manifest" value={artifact?.manifestPath ?? EMPTY_TEXT} />
                  </td>
                  <td className="metric-summary">
                    <span>
                      <strong>结果</strong>
                      {result?.id ?? EMPTY_TEXT}
                    </span>
                    {metricRows(result?.metrics ?? emptyBacktestMetrics()).map(([label, value]) => (
                      <span key={label}>
                        <strong>{label}</strong>
                        {value}
                      </span>
                    ))}
                  </td>
                  <td className="path-cell" title={task.configPath ?? EMPTY_TEXT}>
                    <CompactPath value={task.configPath ?? EMPTY_TEXT} />
                  </td>
                  <td className="path-cell" title={task.resultPath ?? artifact?.resultPath ?? EMPTY_TEXT}>
                    <CompactPath value={result?.resultPath ?? task.resultPath ?? artifact?.resultPath ?? EMPTY_TEXT} />
                  </td>
                  <td className="source-cell">
                    <BacktestSourceSummary source={result?.dataSource ?? task.dataSource} />
                  </td>
                  <td className="reason-cell" title={reason}>
                    {reason}
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
