import type { HyperoptMetricComparison, HyperoptRunSummary } from "../api/types";
import { useMvpData } from "../api/useMvpData";
import { formatNumber, reasonText, statusClassName, summarizeText } from "./backtestDisplay";
import { FallbackNotice } from "./FallbackNotice";
import { EMPTY_TEXT, displayLoadState, displayStatus } from "./uiCopy";

function paramsPreview(params: Record<string, unknown>): Array<[string, string]> {
  return Object.entries(params)
    .slice(0, 6)
    .map(([key, value]) => [key, typeof value === "number" ? String(value) : JSON.stringify(value)]);
}

function metricValue(value: number | null, suffix: string): string {
  return value === null ? EMPTY_TEXT : `${value.toFixed(2)}${suffix}`;
}

function metricDelta(metric: HyperoptMetricComparison): string {
  if (metric.delta === null) {
    return EMPTY_TEXT;
  }

  const prefix = metric.delta > 0 ? "+" : "";
  return `${prefix}${metric.delta.toFixed(2)}${metric.suffix}`;
}

function runReason(run: HyperoptRunSummary): string {
  return reasonText(run.blockedReason, run.failedReason);
}

export function HyperoptRuns() {
  const { data, source, isLoading, error } = useMvpData();
  const statusCounts = data.hyperoptRuns.reduce<Record<string, number>>((counts, run) => {
    const status = run.artifactManifest?.status ?? run.status;
    counts[status] = (counts[status] ?? 0) + 1;
    return counts;
  }, {});
  const bestRun = data.hyperoptRuns.find((run) => run.bestLoss !== null || run.score !== null);
  const blockedRuns = data.hyperoptRuns.filter((run) => run.blockedReason || run.artifactManifest?.blockedReason);

  return (
    <section className="page">
      <header className="page-header">
        <h1>Hyperopt 参数优化</h1>
        <span className="status-pill">{displayLoadState(isLoading, source)}</span>
      </header>
      <FallbackNotice
        context="Hyperopt 参数优化批次、best params、artifact 和优化前后指标。"
        error={error}
        isLoading={isLoading}
        source={source}
      />
      <section className="hyperopt-summary" aria-label="Hyperopt 参数优化摘要">
        <article className="overview-panel">
          <h2>批次状态</h2>
          <div className="status-counts" role="list">
            {Object.entries(statusCounts).length === 0 ? (
              <span>暂无批次</span>
            ) : (
              Object.entries(statusCounts).map(([status, count], index, entries) => (
                <span
                  aria-label={`${displayStatus(status)}：${count} 个`}
                  className={`run-status ${statusClassName(status)}`}
                  key={status}
                  role="listitem"
                >
                  {displayStatus(status)}：{count}
                  {index < entries.length - 1 ? <span className="status-text-gap"> </span> : null}
                </span>
              ))
            )}
          </div>
        </article>
        <article className="overview-panel">
          <h2>最佳结果</h2>
          {bestRun ? (
            <p>
              {bestRun.strategyName} epoch {bestRun.epoch ?? EMPTY_TEXT}，loss{" "}
              {formatNumber(bestRun.bestLoss)}，score {formatNumber(bestRun.score)}。
            </p>
          ) : (
            <p>暂无最佳结果。</p>
          )}
        </article>
        <article className="overview-panel">
          <h2>阻塞复核</h2>
          <p>
            {blockedRuns.length === 0
              ? "暂无已阻塞的 Hyperopt 批次。"
              : `${blockedRuns.length} 个批次需要本地数据或 artifact 复核。`}
          </p>
        </article>
      </section>
      <div className="table-shell">
        <table>
          <thead>
            <tr>
              <th>批次</th>
              <th>策略</th>
              <th>状态</th>
              <th>Best Params</th>
              <th>Artifact</th>
              <th>优化前 / 后</th>
              <th>警告</th>
              <th>原因</th>
            </tr>
          </thead>
          <tbody>
            {data.hyperoptRuns.map((run) => {
              const artifact = run.artifactManifest;
              const comparison = run.comparison;
              const reason = runReason(run);
              const paramRows = paramsPreview(run.bestParams);

              return (
                <tr key={run.id}>
                  <td>
                    <strong>{run.id}</strong>
                    <span className="secondary-cell">{run.profileName}</span>
                  </td>
                  <td>{run.strategyName}</td>
                  <td>
                    <span className={`run-status ${statusClassName(artifact?.status ?? run.status)}`}>
                      {displayStatus(artifact?.status ?? run.status)}
                    </span>
                  </td>
                  <td className="params-cell">
                    {paramRows.length === 0 ? (
                      <span>{EMPTY_TEXT}</span>
                    ) : (
                      paramRows.map(([key, value]) => (
                        <span key={key}>
                          <strong>{key}</strong>
                          {value}
                        </span>
                      ))
                    )}
                    <em>spaces：{run.spaces.length > 0 ? run.spaces.join(", ") : EMPTY_TEXT}</em>
                    <em>loss: {formatNumber(run.bestLoss)}</em>
                    <em>score: {formatNumber(run.score)}</em>
                  </td>
                  <td className="artifact-cell">
                    <span>manifest：{run.manifestPath ?? artifact?.manifestPath ?? EMPTY_TEXT}</span>
                    <span>result：{run.resultPath ?? artifact?.resultPath ?? EMPTY_TEXT}</span>
                    <span>loss fn：{artifact?.hyperoptLoss ?? EMPTY_TEXT}</span>
                    <span>epochs：{artifact?.epochs ?? EMPTY_TEXT}</span>
                  </td>
                  <td className="comparison-cell">
                    {comparison?.metrics.length ? (
                      comparison.metrics.map((metric) => (
                        <span key={metric.label}>
                          <strong>{metric.label}</strong>
                          {metricValue(metric.before, metric.suffix)} 至 {metricValue(metric.after, metric.suffix)}
                          <em>{metricDelta(metric)}</em>
                        </span>
                      ))
                    ) : (
                      <span>{EMPTY_TEXT}</span>
                    )}
                  </td>
                  <td className="reason-cell">
                    {comparison?.warnings.length ? (
                      comparison.warnings.map((warning) => (
                        <div className={`reason-line ${warning.severity}`} key={`${run.id}:${warning.code}`}>
                          {summarizeText(warning.message)}
                        </div>
                      ))
                    ) : (
                      EMPTY_TEXT
                    )}
                  </td>
                  <td className="reason-cell">{reason}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {data.hyperoptRuns.length === 0 ? <div className="empty-state">暂无 Hyperopt 参数优化批次。</div> : null}
    </section>
  );
}
