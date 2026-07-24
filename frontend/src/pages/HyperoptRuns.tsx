import type {
  HyperoptMetricComparison,
  HyperoptRunSummary,
  RankingSignalSummary,
} from "../api/types";
import { combineDataSources } from "../api/sourceState";
import { useMvpData } from "../api/useMvpData";
import {
  CompactText,
  CopyableValue,
  EmptyState,
  ExpandableText,
  PageHeader,
  StatusBadge,
} from "../components/DisplayPrimitives";
import "../styles/hyperopt-runs.css";
import { FallbackNotice } from "./FallbackNotice";
import {
  countHyperoptStatuses,
  effectiveHyperoptStatus,
  firstUsableHyperoptBestRun,
  formatHyperoptParamsJson,
  formatHyperoptValue,
  hasUsableHyperoptBestResult,
  hyperoptParamsPreview,
  hyperoptRunReason,
} from "./hyperoptDisplay";
import { EMPTY_TEXT, displayLoadState, displayNumber, displayValue } from "./uiCopy";

function metricValue(value: number | null, suffix: string): string {
  return value === null ? EMPTY_TEXT : `${displayNumber(value, { maximumFractionDigits: 2 })}${suffix}`;
}

function metricDelta(metric: HyperoptMetricComparison): string {
  if (metric.delta === null) {
    return EMPTY_TEXT;
  }
  const prefix = metric.delta > 0 ? "+" : "";
  return `${prefix}${displayNumber(metric.delta, { maximumFractionDigits: 2 })}${metric.suffix}`;
}

function WarningDetails({ runId, warnings }: { runId: string; warnings: RankingSignalSummary[] }) {
  if (warnings.length === 0) {
    return <span className="hyperopt-muted">暂无警告</span>;
  }
  return (
    <div className="hyperopt-warning-list">
      {warnings.map((warning, index) => (
        <div key={`${runId}:${warning.code ?? index}`}>
          <StatusBadge showRaw status={warning.severity} />
          <CompactText label="警告摘要" value={warning.message} />
          <ExpandableText summary="查看完整警告" value={warning.message} />
        </div>
      ))}
    </div>
  );
}

function HyperoptTechnicalDetails({ run }: { run: HyperoptRunSummary }) {
  const artifact = run.artifactManifest;
  const paramsJson = formatHyperoptParamsJson(run.bestParams);
  const manifestPath = run.manifestPath ?? artifact?.manifestPath;
  const resultPath = run.resultPath ?? artifact?.resultPath;

  return (
    <details className="hyperopt-technical-details">
      <summary>参数、spaces 与 Artifact</summary>
      <div className="hyperopt-technical-panel">
        <div className="hyperopt-technical-grid">
          <section>
            <h3>完整参数集</h3>
            <CopyableValue label="最佳参数 JSON" value={paramsJson} />
            <ExpandableText mono summary="展开参数 JSON" value={paramsJson} />
          </section>
          <section>
            <h3>运行配置</h3>
            <dl>
              <div>
                <dt>spaces</dt>
                <dd><CopyableValue label="Hyperopt spaces" value={run.spaces.join(", ") || EMPTY_TEXT} /></dd>
              </div>
              <div>
                <dt>Loss function</dt>
                <dd><CopyableValue label="Hyperopt loss function" value={artifact?.hyperoptLoss} /></dd>
              </div>
              <div>
                <dt>计划 epochs</dt>
                <dd>{displayValue(artifact?.epochs)}</dd>
              </div>
              <div>
                <dt>结果 epoch</dt>
                <dd>{displayValue(run.epoch)}</dd>
              </div>
            </dl>
          </section>
          <section>
            <h3>Artifact 路径</h3>
            <dl>
              <div>
                <dt>Manifest</dt>
                <dd><CopyableValue label="Manifest 路径" value={manifestPath} /></dd>
              </div>
              <div>
                <dt>结果文件</dt>
                <dd><CopyableValue label="结果文件路径" value={resultPath} /></dd>
              </div>
              <div>
                <dt>配置文件</dt>
                <dd><CopyableValue label="配置文件路径" value={artifact?.configPath} /></dd>
              </div>
            </dl>
          </section>
        </div>
        <section className="hyperopt-warning-section">
          <h3>警告</h3>
          <WarningDetails runId={run.id} warnings={run.comparison?.warnings ?? []} />
        </section>
      </div>
    </details>
  );
}

function BestResultSummary({ run }: { run: HyperoptRunSummary }) {
  const preview = hyperoptParamsPreview(run.bestParams);
  return (
    <>
      <div className="hyperopt-best-heading">
        <strong>{run.strategyName}</strong>
        <StatusBadge label="可用最佳结果" status="SUCCESS" />
      </div>
      <dl className="hyperopt-best-metrics">
        <div>
          <dt>Loss</dt>
          <dd>{displayNumber(run.bestLoss, { maximumFractionDigits: 4 })}</dd>
        </div>
        <div>
          <dt>Score</dt>
          <dd>{displayNumber(run.score, { maximumFractionDigits: 2 })}</dd>
        </div>
        <div>
          <dt>Epoch</dt>
          <dd>{displayValue(run.epoch)}</dd>
        </div>
      </dl>
      <div className="hyperopt-param-preview">
        {preview.map(([key, value]) => (
          <span key={key}>
            <strong>{key}</strong>
            <CompactText label={`参数 ${key}`} value={value} />
          </span>
        ))}
      </div>
    </>
  );
}

export function HyperoptRuns() {
  const { data, sources, isLoading, error } = useMvpData();
  const source = combineDataSources(sources, ["hyperoptRuns"]);
  const statusCounts = countHyperoptStatuses(data.hyperoptRuns);
  const bestRun = firstUsableHyperoptBestRun(data.hyperoptRuns);
  const blockedOrFailedRuns = data.hyperoptRuns.filter((run) =>
    ["BLOCKED", "FAILED"].includes(effectiveHyperoptStatus(run)),
  );
  const firstProblem = blockedOrFailedRuns[0];

  return (
    <section className="page hyperopt-page">
      <PageHeader
        title="Hyperopt 参数优化"
        description="先确认是否存在可用最佳结果；完整参数和 Artifact 证据按需展开。"
        status={<StatusBadge label={displayLoadState(isLoading, source)} status={isLoading ? "RUNNING" : source} />}
      />
      <FallbackNotice
        context="Hyperopt 参数优化批次、最佳参数、Artifact 和优化前后指标。"
        error={error}
        isLoading={isLoading}
        source={source}
      />

      <section className="hyperopt-overview" aria-label="Hyperopt 参数优化摘要">
        <article className="hyperopt-overview-card">
          <span>批次状态</span>
          <div className="hyperopt-status-counts" role="list">
            {Object.entries(statusCounts).length === 0 ? (
              <StatusBadge label="暂无批次" status="NOT_RUN" />
            ) : (
              Object.entries(statusCounts).map(([status, count]) => (
                <span key={status} role="listitem">
                  <StatusBadge showRaw status={status} />
                  <strong>{count}</strong>
                </span>
              ))
            )}
          </div>
        </article>
        <article className={bestRun ? "hyperopt-overview-card hyperopt-best-card" : "hyperopt-overview-card"}>
          <span>最佳结果</span>
          {bestRun ? (
            <BestResultSummary run={bestRun} />
          ) : (
            <div className="hyperopt-no-result">
              <StatusBadge label="无可用最佳结果" status="NOT_ACCEPTABLE" />
              <p>尚无同时满足成功状态、最佳参数、Loss 和结果 Artifact 的记录。</p>
            </div>
          )}
        </article>
        <article className={firstProblem ? "hyperopt-overview-card hyperopt-problem-card" : "hyperopt-overview-card"}>
          <span>阻塞与失败</span>
          <strong>{blockedOrFailedRuns.length}</strong>
          {firstProblem ? (
            <>
              <StatusBadge showRaw status={effectiveHyperoptStatus(firstProblem)} />
              <CompactText label="首要阻塞或失败原因" value={hyperoptRunReason(firstProblem)} />
            </>
          ) : (
            <p>当前没有已记录的阻塞或失败批次。</p>
          )}
        </article>
      </section>

      <div className="table-shell hyperopt-table-shell">
        <table className="hyperopt-table">
          <colgroup>
            <col className="hyperopt-col-run" />
            <col className="hyperopt-col-status" />
            <col className="hyperopt-col-result" />
            <col className="hyperopt-col-comparison" />
            <col className="hyperopt-col-details" />
          </colgroup>
          <thead>
            <tr>
              <th>策略与批次</th>
              <th>状态</th>
              <th>最佳结果</th>
              <th>改进结论</th>
              <th>技术详情</th>
            </tr>
          </thead>
          <tbody>
            {data.hyperoptRuns.map((run) => {
              const effectiveStatus = effectiveHyperoptStatus(run);
              const usableBest = hasUsableHyperoptBestResult(run);
              const comparison = run.comparison;
              const reason = hyperoptRunReason(run);
              const preview = hyperoptParamsPreview(run.bestParams);

              return (
                <tr data-problem={["BLOCKED", "FAILED"].includes(effectiveStatus) ? "true" : "false"} key={run.id}>
                  <td>
                    <strong className="hyperopt-strategy-name">{run.strategyName}</strong>
                    <span className="hyperopt-profile-name">{run.profileName}</span>
                    <CopyableValue label="Hyperopt 批次 ID" value={run.id} />
                  </td>
                  <td>
                    <div className="hyperopt-status-cell">
                      <StatusBadge showRaw status={effectiveStatus} />
                      {reason !== EMPTY_TEXT ? (
                        <>
                          <CompactText className="hyperopt-problem-text" label="阻塞或失败原因" value={reason} />
                          <ExpandableText summary="查看完整原因" value={reason} />
                        </>
                      ) : null}
                    </div>
                  </td>
                  <td>
                    {usableBest ? (
                      <div className="hyperopt-result-cell">
                        <StatusBadge label="可用" status="SUCCESS" />
                        <strong>Loss {displayNumber(run.bestLoss, { maximumFractionDigits: 4 })}</strong>
                        <span>Score {displayNumber(run.score, { maximumFractionDigits: 2 })}</span>
                        <span>Epoch {displayValue(run.epoch)}</span>
                        <div className="hyperopt-inline-params">
                          {preview.map(([key, value]) => (
                            <span key={key}>
                              <strong>{key}</strong>
                              {value}
                            </span>
                          ))}
                        </div>
                      </div>
                    ) : (
                      <div className="hyperopt-result-cell">
                        <StatusBadge label="不可作为最佳结果" status="NOT_ACCEPTABLE" />
                        {run.bestLoss !== null || run.score !== null ? (
                          <ExpandableText
                            summary="查看记录值（不可验收）"
                            value={`Loss ${displayValue(run.bestLoss)}；Score ${displayValue(run.score)}；Epoch ${displayValue(run.epoch)}`}
                          />
                        ) : (
                          <span className="hyperopt-muted">本批次没有最佳结果。</span>
                        )}
                      </div>
                    )}
                  </td>
                  <td>
                    {comparison ? (
                      <div className="hyperopt-comparison-cell">
                        <StatusBadge showRaw status={comparison.status} />
                        {comparison.status.toLowerCase() === "success" && comparison.metrics.length > 0 ? (
                          comparison.metrics.slice(0, 3).map((metric) => (
                            <span key={metric.label}>
                              <strong>{metric.label}</strong>
                              {metricValue(metric.before, metric.suffix)} → {metricValue(metric.after, metric.suffix)}
                              <em>{metricDelta(metric)}</em>
                            </span>
                          ))
                        ) : (
                          <CompactText
                            label="改进结论"
                            value={comparison.blockedReason ?? comparison.failedReason ?? "暂无可验收的改进结论。"}
                          />
                        )}
                      </div>
                    ) : (
                      <span className="hyperopt-muted">暂无优化前后对比。</span>
                    )}
                  </td>
                  <td><HyperoptTechnicalDetails run={run} /></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {!isLoading && data.hyperoptRuns.length === 0 ? (
        <EmptyState
          description="当前没有 Hyperopt 参数优化批次；空结果不代表优化成功。"
          title="暂无真实 Hyperopt 批次"
        />
      ) : null}
    </section>
  );
}
