import { combineDataSources } from "../api/sourceState";
import type { DataSourceTraceSummary } from "../api/types";
import { useMvpData } from "../api/useMvpData";
import {
  buildBacktestMatrixSummary,
  formatNumber,
  metricRows,
  reasonText,
  statusClassName,
} from "./backtestDisplay";
import {
  emptyBacktestMetrics,
  findBacktestResultForRun,
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

export function BacktestRuns() {
  const { data, sources, isLoading, error } = useMvpData();
  const source = combineDataSources(sources, ["backtestRuns", "backtestTasks"]);
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
          <colgroup>
            <col className="backtest-col-id" />
            <col className="backtest-col-strategy" />
            <col className="backtest-col-status" />
            <col className="backtest-col-profile" />
            <col className="backtest-col-count" />
            <col className="backtest-col-artifact" />
            <col className="backtest-col-metrics" />
            <col className="backtest-col-path" />
            <col className="backtest-col-source" />
            <col className="backtest-col-reason" />
          </colgroup>
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
              const result = findBacktestResultForRun(data.backtestResults, run.id);
              const artifact = run.artifactManifest ?? linkedTask?.artifactManifest ?? null;
              const resultPath = result?.resultPath ?? artifact?.resultPath ?? linkedTask?.resultPath ?? EMPTY_TEXT;
              const manifestPath = artifact?.manifestPath ?? EMPTY_TEXT;
              const recordedReason = reasonText(
                run.blockedReason ?? linkedTask?.blockedReason ?? null,
                run.failedReason ?? linkedTask?.failedReason ?? null,
              );
              const reason = recordedReason === EMPTY_TEXT && !result ? missingBacktestResultReason("批次") : recordedReason;

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
                    <CompactPath label="manifest" value={manifestPath} />
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
                  <td className="path-cell" title={resultPath}>
                    <CompactPath value={resultPath} />
                  </td>
                  <td className="source-cell">
                    <BacktestSourceSummary source={result?.dataSource ?? run.dataSource ?? linkedTask?.dataSource} />
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
