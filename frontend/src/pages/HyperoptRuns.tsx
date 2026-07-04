import type { HyperoptMetricComparison, HyperoptRunSummary } from "../api/types";
import { useMvpData } from "../api/useMvpData";
import { formatNumber, reasonText, statusClassName, summarizeText } from "./backtestDisplay";

function paramsPreview(params: Record<string, unknown>): Array<[string, string]> {
  return Object.entries(params)
    .slice(0, 6)
    .map(([key, value]) => [key, typeof value === "number" ? String(value) : JSON.stringify(value)]);
}

function metricValue(value: number | null, suffix: string): string {
  return value === null ? "none" : `${value.toFixed(2)}${suffix}`;
}

function metricDelta(metric: HyperoptMetricComparison): string {
  if (metric.delta === null) {
    return "none";
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
        <h1>Hyperopt Runs</h1>
        <span className="status-pill">{isLoading ? "Loading" : source}</span>
      </header>
      {error ? <div className="notice">Using fallback data: {error}</div> : null}
      {!isLoading && source === "fallback" && !error ? (
        <div className="notice">Backend API unavailable; showing controlled fallback Hyperopt data.</div>
      ) : null}
      <section className="hyperopt-summary" aria-label="Hyperopt summary">
        <article className="overview-panel">
          <h2>Run Status</h2>
          <div className="status-counts">
            {Object.entries(statusCounts).length === 0 ? (
              <span>No runs</span>
            ) : (
              Object.entries(statusCounts).map(([status, count]) => (
                <span className={`run-status ${statusClassName(status)}`} key={status}>
                  {status}: {count}
                </span>
              ))
            )}
          </div>
        </article>
        <article className="overview-panel">
          <h2>Best Result</h2>
          {bestRun ? (
            <p>
              {bestRun.strategyName} epoch {bestRun.epoch ?? "none"} loss{" "}
              {formatNumber(bestRun.bestLoss)} score {formatNumber(bestRun.score)}.
            </p>
          ) : (
            <p>No best result is available.</p>
          )}
        </article>
        <article className="overview-panel">
          <h2>Blocked Review</h2>
          <p>
            {blockedRuns.length === 0
              ? "No blocked Hyperopt runs are present."
              : `${blockedRuns.length} run(s) require local data or artifact review.`}
          </p>
        </article>
      </section>
      <div className="table-shell">
        <table>
          <thead>
            <tr>
              <th>Run</th>
              <th>Strategy</th>
              <th>Status</th>
              <th>Best Params</th>
              <th>Artifact</th>
              <th>Before / After</th>
              <th>Warnings</th>
              <th>Reason</th>
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
                      {artifact?.status ?? run.status}
                    </span>
                  </td>
                  <td className="params-cell">
                    {paramRows.length === 0 ? (
                      <span>none</span>
                    ) : (
                      paramRows.map(([key, value]) => (
                        <span key={key}>
                          <strong>{key}</strong>
                          {value}
                        </span>
                      ))
                    )}
                    <em>spaces: {run.spaces.length > 0 ? run.spaces.join(", ") : "none"}</em>
                    <em>loss: {formatNumber(run.bestLoss)}</em>
                    <em>score: {formatNumber(run.score)}</em>
                  </td>
                  <td className="artifact-cell">
                    <span>manifest: {run.manifestPath ?? artifact?.manifestPath ?? "none"}</span>
                    <span>result: {run.resultPath ?? artifact?.resultPath ?? "none"}</span>
                    <span>loss fn: {artifact?.hyperoptLoss ?? "none"}</span>
                    <span>epochs: {artifact?.epochs ?? "none"}</span>
                  </td>
                  <td className="comparison-cell">
                    {comparison?.metrics.length ? (
                      comparison.metrics.map((metric) => (
                        <span key={metric.label}>
                          <strong>{metric.label}</strong>
                          {metricValue(metric.before, metric.suffix)} to {metricValue(metric.after, metric.suffix)}
                          <em>{metricDelta(metric)}</em>
                        </span>
                      ))
                    ) : (
                      <span>none</span>
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
                      "none"
                    )}
                  </td>
                  <td className="reason-cell">{reason}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {data.hyperoptRuns.length === 0 ? <div className="empty-state">No Hyperopt runs found.</div> : null}
    </section>
  );
}
