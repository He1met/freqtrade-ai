import { useEffect, useState } from "react";

import {
  CompactText as DisplayCompactText,
  CopyableValue,
  EmptyState,
  ExpandableText,
  StatusBadge,
} from "../../components/DisplayPrimitives";
import {
  StrategyGenerationApiError,
  checkDryRunReadiness,
  ingestBacktestArtifact,
  runDeepSeekSingle,
  startControlledDryRun,
  stopControlledDryRun,
  triggerLocalBacktest,
} from "../../api/client";
import type {
  BacktestResultSummary,
  BacktestRunSummary,
  BacktestTaskSummary,
  DataSourceTraceSummary,
  DryRunControlReport,
  DryRunReadinessReport,
  LocalStrategyLabEvidenceSummary,
  MvpData,
  RankingEntry,
  StrategyGenerationApiResult,
  StrategyGenerationStrategy,
  StrategyGenerationVersion,
  RuntimeStatusSummary,
} from "../../api/types";
import {
  metricRows,
  reasonText,
} from "../backtestDisplay";
import {
  emptyBacktestMetrics,
  findBacktestResultForTask,
  missingBacktestResultReason,
} from "../backtestResultLookup";
import { FallbackNotice } from "../FallbackNotice";
import { isCoreDataSource } from "../SourceMarker";
import { isCoreDataSourceTrace } from "../../api/sourceState";
import { EMPTY_TEXT, displayBoolean, displayLoadState, displayStatus, displayValue } from "../uiCopy";
import {
  actionStatusClassName,
  actionStatusMessage,
  createActionEvidence,
  type ActionEvidence,
} from "./actionEvidence";
import {
  evidenceStateDisplay,
  formatTraceEntries,
  partitionEvidenceRecords,
} from "./evidenceDisplay";
import {
  controlNextAction,
  dryRunBlockers,
  dryRunSafetyConclusion,
  readinessNextAction,
  readinessReason,
  resolvedControlStatus,
} from "./readinessControlDisplay";
import "../../styles/local-strategy-lab-evidence.css";
import "../../styles/local-strategy-lab-readiness.css";

export type SubmissionState =
  | { kind: "idle" }
  | { kind: "submitting"; promptSummary: string; requestedCount: number }
  | { kind: "success"; result: StrategyGenerationApiResult }
  | { kind: "unauthorized"; message: string; statusCode: number | null; statusText: string | null }
  | {
      kind: "blocked";
      message: string;
      result?: StrategyGenerationApiResult;
      runId?: string | null;
      statusCode?: number | null;
      statusText?: string | null;
    }
  | {
      kind: "failed";
      message: string;
      runId: string | null;
      statusCode: number | null;
      statusText: string | null;
    };

type ReadinessState =
  | { kind: "idle" }
  | { kind: "checking"; strategyVersionId: string }
  | { kind: "ready"; report: DryRunReadinessReport }
  | { kind: "blocked"; report: DryRunReadinessReport | null; message: string | null }
  | { kind: "failed"; message: string };

type ControlState =
  | { kind: "idle" }
  | { kind: "starting"; strategyVersionId: string }
  | { kind: "stopping" }
  | { kind: "complete"; report: DryRunControlReport }
  | { kind: "failed"; message: string };

type SourceRow = {
  label: string;
  source: DataSourceTraceSummary;
};

type RecordActionEvidence = (entry: ActionEvidence) => void;

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function responseId(value: unknown): string | null {
  const id = asRecord(value).id;
  return id === null || id === undefined ? null : String(id);
}

function responseText(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function apiErrorMessage(error: unknown, fallback: string): string {
  return error instanceof StrategyGenerationApiError
    ? error.message
    : error instanceof Error
      ? error.message
      : fallback;
}

function apiErrorStatus(error: unknown): "UNAUTHORIZED" | "BLOCKED" | "FAILED" {
  if (error instanceof StrategyGenerationApiError) {
    return error.operationStatus ?? (error.status === 401 || error.status === 403 ? "UNAUTHORIZED" : "FAILED");
  }
  return "FAILED";
}

function formatRecord(record: Record<string, number | string>): string {
  const entries = Object.entries(record);
  return entries.length > 0 ? entries.map(([key, value]) => `${key}: ${value}`).join(", ") : EMPTY_TEXT;
}

function formatScore(value: number | null): string {
  return value === null ? EMPTY_TEXT : value.toFixed(1);
}

function formatEvidence(value: Record<string, unknown>): string {
  const entries = Object.entries(value);
  return entries.length > 0 ? entries.map(([key, item]) => `${key}: ${String(item)}`).join(", ") : EMPTY_TEXT;
}

function CompactText({ className = "", value }: { className?: string; value: string | null | undefined }) {
  return (
    <DisplayCompactText
      className={`lab-compact-text ${className}`}
      label="完整内容"
      value={value}
    />
  );
}

function ActionEvidenceHistory({ history }: { history: ActionEvidence[] }) {
  return (
    <section className="lab-evidence-section" aria-label="核心操作反馈记录">
      <div className="section-header detail-section">
        <h2>核心操作反馈记录</h2>
        <span>本浏览器保留，API/DB 证据为准</span>
      </div>
      {history.length === 0 ? (
        <div className="empty-state">尚未发起核心操作。每次请求的状态、ID、artifact 和下一步会保留在这里。</div>
      ) : (
        <div className="table-shell lab-table-shell">
          <table>
            <thead>
              <tr>
                <th>操作</th>
                <th>状态</th>
                <th>database IDs</th>
                <th>artifact paths</th>
                <th>原因 / 结果</th>
                <th>下一步</th>
                <th>Bug 建议</th>
              </tr>
            </thead>
            <tbody>
              {history.map((entry) => (
                <tr key={entry.action}>
                  <td className="primary-cell">{entry.action}</td>
                  <td><span className={`run-status ${actionStatusClassName(entry.status)}`}>{entry.status}</span></td>
                  <td className="path-cell"><CompactText value={formatRecord(entry.databaseIds)} /></td>
                  <td className="path-cell"><CompactText value={entry.artifactPaths.join(", ") || EMPTY_TEXT} /></td>
                  <td className="reason-cell"><CompactText value={`${entry.message}（${entry.updatedAt}）`} /></td>
                  <td className="reason-cell"><CompactText value={entry.nextAction} /></td>
                  <td>{entry.recommendBug ? "建议创建 Bug Issue" : "否"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function LabSourceSummary({ source }: { source: DataSourceTraceSummary | undefined }) {
  const sourceType = source?.sourceType ?? "unknown";
  const databaseEntries = Object.entries(source?.databaseIds ?? {});
  const artifactEntries = Object.entries(source?.artifactRefs ?? {});

  return (
    <details
      className="lab-source-summary lab-source-trace"
      data-core-source={source?.coreData === true ? "true" : "false"}
    >
      <summary>
        <span>{source?.coreData ? "核心来源" : "非核心来源"}</span>
        <strong>{sourceType}</strong>
      </summary>
      <dl>
        <div>
          <dt>source_type</dt>
          <dd><CopyableValue label="source_type" value={sourceType} /></dd>
        </div>
        <div>
          <dt>core_data</dt>
          <dd>{displayBoolean(source?.coreData)}</dd>
        </div>
        <div>
          <dt>database_ids</dt>
          <dd className="lab-trace-values">
            {databaseEntries.length > 0
              ? databaseEntries.map(([key, value]) => (
                  <CopyableValue key={key} label={key} value={`${key}: ${value}`} />
                ))
              : EMPTY_TEXT}
          </dd>
        </div>
        <div>
          <dt>artifact_refs</dt>
          <dd className="lab-trace-values">
            {artifactEntries.length > 0
              ? artifactEntries.map(([key, value]) => (
                  <CopyableValue key={key} label={key} value={`${key}: ${value}`} />
                ))
              : EMPTY_TEXT}
          </dd>
        </div>
        <div>
          <dt>来源说明</dt>
          <dd>
            <ExpandableText value={source?.sourceDetail ?? EMPTY_TEXT} />
          </dd>
        </div>
        {source?.blockedReason ? (
          <div>
            <dt>阻塞原因</dt>
            <dd><ExpandableText value={source.blockedReason} /></dd>
          </div>
        ) : null}
      </dl>
    </details>
  );
}

function latest<T>(items: T[], count = 6): T[] {
  return items.slice(0, count);
}

function isCoreSource(source: DataSourceTraceSummary, allowedTypes: string[]): boolean {
  return isCoreDataSourceTrace(source) && allowedTypes.includes(source.sourceType);
}

export function isCoreGenerationResult(result: StrategyGenerationApiResult): boolean {
  const hasCoreStrategy = result.strategies.some(
    (strategy) => strategy.id && isCoreSource(strategy.dataSource, ["database"]),
  );
  const hasCoreVersion = result.strategyVersions.some(
    (version) => version.id && version.filePath && isCoreSource(version.dataSource, ["database"]),
  );

  return (
    result.run.status === "succeeded" &&
    isCoreSource(result.dataSource, ["api_aggregate"]) &&
    isCoreSource(result.run.dataSource, ["database"]) &&
    hasCoreStrategy &&
    hasCoreVersion
  );
}

function statusClassName(status: string): string {
  const normalized = status.toLowerCase();
  if (normalized === "succeeded" || normalized === "success" || normalized === "acceptable" || normalized === "ready") {
    return "status-success";
  }
  if (normalized === "failed" || normalized === "cancelled") {
    return "status-failed";
  }
  if (normalized === "blocked" || normalized === "unknown" || normalized === "fallback") {
    return "status-blocked";
  }
  return "status-neutral";
}

export function submissionStatus(submission: SubmissionState): {
  className: string;
  label: string;
  title: string;
} {
  if (submission.kind === "submitting") {
    return {
      className: "status-neutral",
      label: "提交中",
      title: "正在提交到 backend API",
    };
  }
  if (submission.kind === "success") {
    return {
      className: "status-success",
      label: "核心成功",
      title: "backend API/DB 已返回可追踪生成记录",
    };
  }
  if (submission.kind === "unauthorized") {
    return {
      className: "status-blocked",
      label: "UNAUTHORIZED",
      title: "本地 operator 授权被拒绝",
    };
  }
  if (submission.kind === "failed") {
    return {
      className: "status-failed",
      label: "FAILED",
      title: "backend 返回失败状态",
    };
  }
  if (submission.kind === "blocked") {
    return {
      className: "status-blocked",
      label: "BLOCKED",
      title: "没有可证明的核心成功结果",
    };
  }
  return {
    className: "status-neutral",
    label: "等待输入",
    title: "尚未提交",
  };
}

function buildSourceRows(result: StrategyGenerationApiResult): SourceRow[] {
  return [
    { label: "API response", source: result.dataSource },
    { label: `Run ${displayValue(result.run.id)}`, source: result.run.dataSource },
    ...result.strategies.map((strategy) => ({
      label: `Strategy ${displayValue(strategy.id)}`,
      source: strategy.dataSource,
    })),
    ...result.strategyVersions.map((version) => ({
      label: `Version ${displayValue(version.id)}`,
      source: version.dataSource,
    })),
  ];
}

function versionRows(
  result: StrategyGenerationApiResult,
): Array<{ strategy: StrategyGenerationStrategy | null; version: StrategyGenerationVersion }> {
  const strategyById = new Map(result.strategies.map((strategy) => [strategy.id, strategy]));
  return result.strategyVersions.map((version) => ({
    strategy: strategyById.get(version.strategyId) ?? null,
    version,
  }));
}

export function submissionMessage(submission: SubmissionState): string {
  if (submission.kind === "success") {
    return "生成请求已由 backend API 写入数据库；仍需后续验证、回测和人工复核。";
  }
  if (submission.kind === "failed" || submission.kind === "unauthorized") {
    return submission.message;
  }
  if (submission.kind === "blocked") {
    return submission.message;
  }
  if (submission.kind === "submitting") {
    return `正在提交 ${submission.requestedCount} 个本地策略生成请求。`;
  }
  return "输入策略想法后提交，页面只接受 backend API/DB 可证明的核心结果。";
}

function readinessStatus(readiness: ReadinessState): {
  className: string;
  label: string;
  title: string;
} {
  if (readiness.kind === "checking") {
    return { className: "status-neutral", label: "检查中", title: "正在请求 readiness API" };
  }
  if (readiness.kind === "ready") {
    return { className: "status-success", label: "READY", title: "本地 dry-run readiness 检查通过" };
  }
  if (readiness.kind === "blocked") {
    return { className: "status-blocked", label: "BLOCKED", title: "本地 dry-run readiness 缺少条件" };
  }
  if (readiness.kind === "failed") {
    return { className: "status-failed", label: "FAILED", title: "readiness API 请求失败" };
  }
  return { className: "status-neutral", label: "未检查", title: "尚未执行 readiness 检查" };
}

function DataSourceTable({ rows }: { rows: SourceRow[] }) {
  return (
    <details className="lab-technical-matrix">
      <summary>查看完整 data_source 技术矩阵</summary>
      <div className="table-shell lab-table-shell">
        <table>
          <thead>
            <tr>
              <th>data_source</th>
              <th>source_type</th>
              <th>source_detail</th>
              <th>core_data</th>
              <th>database_ids</th>
              <th>artifact_refs</th>
              <th>blocked_reason</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.label}>
                <td className="primary-cell">{row.label}</td>
                <td><CopyableValue label="source_type" value={row.source.sourceType} /></td>
                <td className="path-cell">
                  <ExpandableText value={row.source.sourceDetail} />
                </td>
                <td>{displayBoolean(row.source.coreData)}</td>
                <td className="path-cell">
                  <CopyableValue label="database_ids" value={formatTraceEntries(row.source.databaseIds)} />
                </td>
                <td className="path-cell">
                  <CopyableValue label="artifact_refs" value={formatTraceEntries(row.source.artifactRefs)} />
                </td>
                <td className="path-cell">
                  <ExpandableText value={row.source.blockedReason ?? EMPTY_TEXT} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}

function StrategyVersionEvidence({
  strategies,
  versions,
}: {
  strategies: MvpData["strategies"];
  versions: StrategyGenerationVersion[];
}) {
  const strategyById = new Map(strategies.map((strategy) => [strategy.id, strategy]));
  const rows = latest(versions);

  return (
    <section className="lab-evidence-section" aria-label="持久策略版本">
      <div className="section-header detail-section">
        <h2>策略 / 版本 / 文件</h2>
        <span>{versions.length} 条 API 版本记录</span>
      </div>
      <div className="table-shell lab-table-shell">
        <table>
          <thead>
            <tr>
              <th className="lab-col-id">strategy id</th>
              <th className="lab-col-id">version id</th>
              <th className="lab-col-name">名称</th>
              <th className="lab-col-tight">版本</th>
              <th className="lab-col-tight">验证</th>
              <th className="lab-col-status">file state</th>
              <th className="lab-col-path">file path</th>
              <th className="lab-col-source">DB trace</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((version) => {
              const strategy = strategyById.get(version.strategyId);
              const fileState = version.fileState ?? {
                status: "BLOCKED",
                blockedReason: "Backend did not provide strategy file state.",
              };
              return (
                <tr key={version.id}>
                  <td>
                    <CopyableValue label="策略 ID" value={version.strategyId} />
                  </td>
                  <td>
                    <CopyableValue label="版本 ID" value={version.id} />
                  </td>
                  <td>
                    <CompactText value={strategy?.name ?? EMPTY_TEXT} />
                  </td>
                  <td>{version.versionNumber}</td>
                  <td>{displayStatus(version.validationStatus)}</td>
                  <td>
                    {displayStatus(fileState.status)}
                    {fileState.blockedReason ? (
                      <span className="inline-muted"> {fileState.blockedReason}</span>
                    ) : null}
                  </td>
                  <td className="path-cell">
                    <CopyableValue label="策略文件路径" value={version.filePath} />
                  </td>
                  <td className="source-cell">
                    <LabSourceSummary source={version.dataSource} />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {rows.length === 0 ? <div className="empty-state">暂无 API/DB strategy version 记录。</div> : null}
    </section>
  );
}

function GenerationRunEvidence({ runs }: { runs: MvpData["generationRuns"] }) {
  const rows = latest(runs);

  return (
    <section className="lab-evidence-section" aria-label="持久生成批次">
      <div className="section-header detail-section">
        <h2>生成批次</h2>
        <span>{runs.length} 条 API run 记录</span>
      </div>
      <div className="table-shell lab-table-shell">
        <table>
          <thead>
            <tr>
              <th className="lab-col-id">run id</th>
              <th className="lab-col-status">状态</th>
              <th className="lab-col-name">provider / model</th>
              <th className="lab-col-count">计数</th>
              <th className="lab-col-reason">错误</th>
              <th className="lab-col-source">DB trace</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((run) => (
              <tr key={run.id}>
                <td>
                  <CopyableValue label="生成记录 ID" value={run.id} />
                </td>
                <td>
                  <span className={`run-status ${statusClassName(run.status)}`}>{displayStatus(run.status)}</span>
                </td>
                <td>
                  <CompactText value={`${run.provider} / ${run.model}`} />
                </td>
                <td>
                  requested {run.requestedCount}, accepted {run.acceptedCount}, failed {run.failedCount}
                </td>
                <td className="reason-cell">
                  {run.errorMessage ? (
                    <ExpandableText summary="查看完整错误" value={run.errorMessage} />
                  ) : (
                    <span className="inline-muted">未记录错误</span>
                  )}
                </td>
                <td className="source-cell">
                  <LabSourceSummary source={run.dataSource} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {rows.length === 0 ? <div className="empty-state">暂无 API/DB generation run 记录。</div> : null}
    </section>
  );
}

function BacktestEvidence({
  runs,
  tasks,
  results,
}: {
  runs: BacktestRunSummary[];
  tasks: BacktestTaskSummary[];
  results: BacktestResultSummary[];
}) {
  const runById = new Map(runs.map((run) => [run.id, run]));
  const rows = latest(tasks);

  return (
    <section className="lab-evidence-section" aria-label="持久回测任务和结果">
      <div className="section-header detail-section">
        <h2>回测任务 / 结果</h2>
        <span>
          {runs.length} 批次 / {tasks.length} 任务 / {results.length} 结果
        </span>
      </div>
      <div className="table-shell lab-table-shell">
        <table>
          <thead>
            <tr>
              <th className="lab-col-id">task id</th>
              <th className="lab-col-id">run / version</th>
              <th className="lab-col-status">状态</th>
              <th className="lab-col-tight">pair</th>
              <th className="lab-col-id">result id</th>
              <th className="lab-col-metrics">指标</th>
              <th className="lab-col-path">artifact</th>
              <th className="lab-col-source">source</th>
              <th className="lab-col-reason">原因</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((task) => {
              const run = runById.get(task.runId);
              const result = findBacktestResultForTask(results, task.id);
              const recordedReason = reasonText(task.blockedReason, task.failedReason, task.errorMessage);
              const reason = recordedReason === EMPTY_TEXT && !result ? missingBacktestResultReason("任务") : recordedReason;
              return (
                <tr key={task.id}>
                  <td>
                    <CopyableValue label="回测任务 ID" value={task.id} />
                  </td>
                  <td>
                    <CopyableValue label="回测批次 ID" value={task.runId} />
                    <div className="secondary-cell">version {run?.strategyVersionId ?? EMPTY_TEXT}</div>
                  </td>
                  <td>
                    <span className={`run-status ${statusClassName(task.status)}`}>
                      {displayStatus(task.status)}
                    </span>
                  </td>
                  <td>
                    {task.pair} / {task.timeframe}
                  </td>
                  <td>
                    <CopyableValue label="回测结果 ID" value={result?.id ?? EMPTY_TEXT} />
                  </td>
                  <td className="metric-summary">
                    {metricRows(result?.metrics ?? emptyBacktestMetrics()).map(([label, value]) => (
                      <span key={label}>
                        <strong>{label}</strong>
                        {value}
                      </span>
                    ))}
                  </td>
                  <td className="path-cell">
                    <CopyableValue
                      label="回测 Artifact 路径"
                      value={result?.resultPath ?? task.resultPath ?? EMPTY_TEXT}
                    />
                  </td>
                  <td className="source-cell">
                    <LabSourceSummary source={result?.dataSource ?? task.dataSource} />
                  </td>
                  <td className="reason-cell">
                    <CompactText value={reason} />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {rows.length === 0 ? <div className="empty-state">暂无 API/DB backtest task 记录。</div> : null}
    </section>
  );
}

function RankingEvidence({ ranking }: { ranking: RankingEntry[] }) {
  const rows = latest(ranking);

  return (
    <section className="lab-evidence-section" aria-label="持久评分和排行榜">
      <div className="section-header detail-section">
        <h2>评分 / 排行榜</h2>
        <span>{ranking.length} 条 StrategyScore 记录</span>
      </div>
      <div className="table-shell lab-table-shell">
        <table>
          <thead>
            <tr>
              <th className="lab-col-tight">rank</th>
              <th className="lab-col-id">score id</th>
              <th className="lab-col-id">strategy / version</th>
              <th className="lab-col-id">backtest result</th>
              <th className="lab-col-tight">总分</th>
              <th className="lab-col-status">状态</th>
              <th className="lab-col-path">file path</th>
              <th className="lab-col-source">DB trace</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((entry) => (
              <tr key={`${entry.scoreId}-${entry.strategyVersionId}`}>
                <td>{entry.rank}</td>
                <td>
                  <CopyableValue label="评分 ID" value={displayValue(entry.scoreId)} />
                </td>
                <td>
                  <CopyableValue label="策略 ID" value={entry.strategyId} />
                  <div className="secondary-cell">version {entry.strategyVersionId}</div>
                </td>
                <td>
                  <CopyableValue label="回测结果 ID" value={entry.backtestResultId ?? EMPTY_TEXT} />
                </td>
                <td className="score-cell">{formatScore(entry.totalScore)}</td>
                <td>
                  <span className={`run-status ${entry.elimination.eliminated ? "status-failed" : "status-success"}`}>
                    {entry.elimination.eliminated ? "已淘汰" : "已入榜"}
                  </span>
                </td>
                <td className="path-cell">
                  <CopyableValue label="策略文件路径" value={entry.filePath} />
                </td>
                <td className="source-cell">
                  <LabSourceSummary source={entry.dataSource} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {rows.length === 0 ? <div className="empty-state">暂无 API/DB StrategyScore 记录。</div> : null}
    </section>
  );
}

function readinessCandidate(data: MvpData): { strategyVersionId: string; strategyName: string | null } | null {
  const ranked = data.ranking.find((entry) => isCoreDataSource(entry.dataSource));
  if (ranked?.strategyVersionId) {
    const rankedVersion = data.strategyVersions.find((version) => version.id === ranked.strategyVersionId);
    return {
      strategyVersionId: ranked.strategyVersionId,
      strategyName: rankedVersion?.fileState?.className ?? ranked.strategyName,
    };
  }

  const strategyById = new Map(data.strategies.map((strategy) => [strategy.id, strategy]));
  const version = data.strategyVersions.find((item) => isCoreDataSource(item.dataSource));
  if (!version) {
    return null;
  }

  return {
    strategyVersionId: version.id,
    strategyName: version.fileState?.className ?? strategyById.get(version.strategyId)?.name ?? null,
  };
}

function ReadinessControlOverview({ data }: { data: MvpData }) {
  const candidate = readinessCandidate(data);
  const runtime = data.operatorDashboard.runtimeContract.dryRunReadiness;
  const manifest = data.dryRun.manifest;
  const snapshot = data.dryRun.snapshot;
  const safety = dryRunSafetyConclusion(snapshot);
  const runtimeReason = readinessReason(runtime);
  const persistedBlockers = dryRunBlockers(manifest, snapshot);
  const blockers = Array.from(new Set([
    ...(runtime.status === "READY" ? [] : [runtimeReason]),
    ...persistedBlockers,
  ]));
  const nextAction = readinessNextAction(runtime.status, runtimeReason);
  const profileName = snapshot.profileName ?? manifest?.profileName ?? EMPTY_TEXT;

  return (
    <section
      aria-label="Dry-run readiness 与受控运行总览"
      className="lab-readiness-overview"
      data-testid="lab-readiness-overview"
    >
      <div className="lab-readiness-overview__heading">
        <div>
          <span className="lab-readiness-eyebrow">运行前决策</span>
          <h2>Readiness 与受控 Dry-run</h2>
        </div>
        <StatusBadge label={displayStatus(runtime.status)} showRaw status={runtime.status} />
      </div>

      <div className="lab-readiness-overview__grid">
        <article className="lab-readiness-overview__card">
          <span>Dry-run readiness</span>
          <StatusBadge label={displayStatus(runtime.status)} showRaw status={runtime.status} />
          <p>{runtime.summary}</p>
        </article>
        <article className="lab-readiness-overview__card">
          <span>持久运行状态</span>
          <StatusBadge label={displayStatus(snapshot.status)} showRaw status={snapshot.status} />
          <p>来自最新持久 status snapshot，不以按钮请求状态替代。</p>
        </article>
        <article className="lab-readiness-overview__card">
          <span>安全结论</span>
          <StatusBadge label={safety.status === "PASS" ? "仅 Dry-run" : "安全阻断"} status={safety.status} />
          <p>{safety.reason}</p>
        </article>
      </div>

      <div className="lab-readiness-decision">
        <div>
          <span>{blockers.length ? "当前阻断原因" : "当前结论"}</span>
          <strong>{blockers[0] ?? runtimeReason}</strong>
        </div>
        <div>
          <span>下一步</span>
          <strong>{nextAction}</strong>
        </div>
      </div>

      {blockers.length > 1 ? (
        <ul className="lab-readiness-blockers" aria-label="其余阻断原因">
          {blockers.slice(1).map((blocker) => <li key={blocker}>{blocker}</li>)}
        </ul>
      ) : null}

      <dl className="lab-readiness-audit">
        <div>
          <dt>候选 strategy version</dt>
          <dd><CopyableValue label="候选策略版本 ID" value={candidate?.strategyVersionId ?? EMPTY_TEXT} /></dd>
        </div>
        <div>
          <dt>profile</dt>
          <dd><CopyableValue label="Dry-run profile" value={profileName} /></dd>
        </div>
        <div>
          <dt>dry_run</dt>
          <dd>{displayBoolean(snapshot.dryRun)}</dd>
        </div>
      </dl>

      <details className="lab-readiness-details">
        <summary>展开 manifest、snapshot 与运行边界证据</summary>
        <dl className="lab-readiness-audit">
          <div>
            <dt>manifest</dt>
            <dd><CopyableValue label="Manifest 路径" value={manifest?.manifestPath ?? EMPTY_TEXT} /></dd>
          </div>
          <div>
            <dt>snapshot artifact manifest</dt>
            <dd><CopyableValue label="Snapshot artifact manifest" value={snapshot.artifactManifestPath ?? EMPTY_TEXT} /></dd>
          </div>
          <div>
            <dt>config</dt>
            <dd><CopyableValue label="Dry-run config 路径" value={manifest?.configPath ?? EMPTY_TEXT} /></dd>
          </div>
          <div>
            <dt>策略 / pair / timeframe</dt>
            <dd>{snapshot.strategyName ?? manifest?.strategyName ?? EMPTY_TEXT} · {snapshot.pair ?? manifest?.pair ?? EMPTY_TEXT} · {snapshot.timeframe ?? manifest?.timeframe ?? EMPTY_TEXT}</dd>
          </div>
          <div>
            <dt>snapshot 更新时间</dt>
            <dd>{snapshot.lastUpdated ?? EMPTY_TEXT}</dd>
          </div>
          <div>
            <dt>命令证据</dt>
            <dd><ExpandableText mono summary="查看完整 Dry-run 命令参数" value={manifest?.commandArgs.join(" ") ?? EMPTY_TEXT} /></dd>
          </div>
        </dl>
      </details>

      <aside className="lab-control-safety" role="note">
        本区只允许本地受控 dry-run；禁止 live trading、连接真实交易执行链路或提交真实订单。人工批准不改变这一边界。
      </aside>
    </section>
  );
}

function DryRunReadinessPanel({ data, recordAction }: { data: MvpData; recordAction: RecordActionEvidence }) {
  const [readiness, setReadiness] = useState<ReadinessState>({ kind: "idle" });
  const candidate = readinessCandidate(data);
  const status = readinessStatus(readiness);
  const isChecking = readiness.kind === "checking";
  const report = readiness.kind === "ready" || readiness.kind === "blocked" ? readiness.report : null;

  async function handleCheck() {
    if (!candidate) {
      const message = "没有可用于 readiness 检查的核心 strategy version。";
      setReadiness({ kind: "blocked", report: null, message });
      recordAction(createActionEvidence({
        action: "检查 Dry-run readiness", status: "BLOCKED", message,
        nextAction: "先生成并验证包含 database_ids 的核心 strategy version。", recommendBug: false,
        updatedAt: new Date().toISOString(),
      }));
      return;
    }

    setReadiness({ kind: "checking", strategyVersionId: candidate.strategyVersionId });
    recordAction(createActionEvidence({
      action: "检查 Dry-run readiness", status: "RUNNING", message: actionStatusMessage("RUNNING"),
      nextAction: "等待 backend readiness report。", recommendBug: false,
      databaseIds: { strategy_version_id: candidate.strategyVersionId }, updatedAt: new Date().toISOString(),
    }));
    try {
      const result = await checkDryRunReadiness({
        strategyName: candidate.strategyName,
        strategyVersionId: candidate.strategyVersionId,
      });
      setReadiness(
        result.status === "READY"
          ? { kind: "ready", report: result }
          : { kind: "blocked", report: result, message: result.blockedReasons.join("；") || null },
      );
      const blocked = result.status !== "READY";
      recordAction(createActionEvidence({
        action: "检查 Dry-run readiness", status: blocked ? "BLOCKED" : "SUCCESS",
        message: blocked ? result.blockedReasons.join("；") || "readiness 未通过。" : "readiness report 已返回。",
        nextAction: blocked ? "按 report 的 blocked_reason 补齐前置条件后重试。" : "仅在人工批准后考虑受控 dry-run；不会启动 live trading。",
        recommendBug: false, databaseIds: { strategy_version_id: result.strategyVersionId }, updatedAt: new Date().toISOString(),
      }));
    } catch (error) {
      const message = apiErrorMessage(error, "readiness API 请求失败");
      setReadiness({ kind: "failed", message });
      recordAction(createActionEvidence({
        action: "检查 Dry-run readiness", status: apiErrorStatus(error), message,
        nextAction: "检查 API、策略版本和服务日志；若可稳定复现，创建 Bug Issue。", recommendBug: true,
        databaseIds: { strategy_version_id: candidate.strategyVersionId }, updatedAt: new Date().toISOString(),
      }));
    }
  }

  return (
    <section className="lab-readiness-panel" aria-label="本地 dry-run readiness">
      <div className="lab-readiness-panel__heading">
        <div>
          <span className="lab-readiness-eyebrow">运行前检查</span>
          <h2>Dry-run readiness</h2>
        </div>
        <div className="lab-control-actions">
          <StatusBadge label={status.label} showRaw status={readiness.kind === "checking" ? "CHECKING" : status.label} />
          <button className="secondary-button" disabled={isChecking || !candidate} onClick={handleCheck} type="button">
            {isChecking ? "检查中" : "重新检查"}
          </button>
        </div>
      </div>
      <div className="lab-readiness-decision">
        <div>
          <span>{readiness.kind === "ready" ? "检查结论" : "阻断 / 检查状态"}</span>
          <strong>
            {readiness.kind === "failed" || readiness.kind === "blocked"
              ? readiness.message ?? report?.blockedReasons[0] ?? status.title
              : readiness.kind === "ready"
                ? "Readiness report 为 READY；仍需人工批准，且仅允许受控 dry-run。"
                : status.title}
          </strong>
        </div>
        <div>
          <span>下一步</span>
          <strong>
            {readiness.kind === "ready"
              ? "审计 profile、检查项和安全证据后，再决定是否人工批准受控 dry-run。"
              : readiness.kind === "checking"
                ? "等待 backend readiness report，不要重复提交。"
                : "补齐核心候选版本或按阻断原因修复后重新检查。"}
          </strong>
        </div>
      </div>
      <dl className="lab-readiness-audit">
        <div>
          <dt>候选 strategy_version</dt>
          <dd><CopyableValue label="候选策略版本 ID" value={candidate?.strategyVersionId ?? EMPTY_TEXT} /></dd>
        </div>
        <div>
          <dt>策略</dt>
          <dd><CompactText value={candidate?.strategyName ?? EMPTY_TEXT} /></dd>
        </div>
        <div>
          <dt>profile</dt>
          <dd><CopyableValue label="Readiness profile" value={report?.profileName ?? EMPTY_TEXT} /></dd>
        </div>
      </dl>
      <aside className="lab-control-safety" role="note">
        Readiness 通过不等于 live ready。本操作不会启动 live trading、连接真实交易执行链路或提交真实订单。
      </aside>
      {report?.blockedReasons.length ? (
        <ul className="lab-readiness-blockers">
          {report.blockedReasons.map((reason) => (
            <li key={reason}>{reason}</li>
          ))}
        </ul>
      ) : null}
      {report ? (
        <>
          <div className="lab-readiness-checks" aria-label="Readiness 检查项">
            {report.checks.map((check) => (
              <article className="lab-readiness-check" key={check.name}>
                <div className="lab-readiness-check__heading">
                  <strong>{check.name}</strong>
                  <StatusBadge showRaw status={check.status} />
                </div>
                <p>{check.summary}</p>
                {check.blockedReason ? <ExpandableText summary="查看阻断原因" value={check.blockedReason} /> : null}
                <ExpandableText mono summary="展开并复制前先核对完整证据" value={formatEvidence(check.evidence)} />
              </article>
            ))}
          </div>
          <details className="lab-readiness-details">
            <summary>展开完整 readiness report</summary>
            <dl className="lab-readiness-audit">
              <div>
                <dt>generated_at</dt>
                <dd><CopyableValue label="Readiness 生成时间" value={report.generatedAt} /></dd>
              </div>
              <div>
                <dt>env preflight</dt>
                <dd><ExpandableText mono value={formatEvidence(report.envPreflight)} /></dd>
              </div>
              <div>
                <dt>config preview</dt>
                <dd><ExpandableText mono value={formatEvidence(report.configPreview)} /></dd>
              </div>
              <div>
                <dt>safety</dt>
                <dd><ExpandableText mono value={formatEvidence(report.safety)} /></dd>
              </div>
            </dl>
          </details>
        </>
      ) : null}
      {!candidate ? (
        <EmptyState
          description="先生成并验证带 database_ids 的核心 strategy version；空结果不代表 readiness 成功。"
          title="Readiness BLOCKED"
        />
      ) : null}
    </section>
  );
}

function ReadinessDomainPanel({ data }: { data: MvpData }) {
  const contract = data.operatorDashboard.runtimeContract;
  const domains: Array<[string, RuntimeStatusSummary]> = [
    ["Research readiness", contract.researchReadiness],
    ["Dry-run readiness", contract.dryRunReadiness],
    ["Live readiness (disabled)", contract.liveReadiness],
  ];
  return (
    <section className="lab-evidence-section" aria-label="运行域就绪状态">
      <div className="section-header detail-section">
        <h2>运行域就绪状态</h2>
        <span>只读证据</span>
      </div>
      <div className="lab-evidence-summary">
        {domains.map(([label, readiness]) => (
          <div key={label}>
            <span>{label}</span>
            <strong className={statusClassName(readiness.status)}>{displayStatus(readiness.status)}</strong>
            <small title={readiness.blockedReason ?? readiness.unavailableReason ?? readiness.staleReason ?? readiness.summary}>
              {readiness.summary}
            </small>
          </div>
        ))}
      </div>
    </section>
  );
}

function EvidenceConclusion({ summary }: { summary: LocalStrategyLabEvidenceSummary | undefined }) {
  if (!summary) {
    return null;
  }

  const stateDisplay = evidenceStateDisplay(summary.state);
  const records = partitionEvidenceRecords(summary);

  return (
    <section
      className="lab-evidence-section lab-chain-conclusion"
      aria-label="真实运行链路结论"
      data-testid="lab-evidence-conclusion"
      data-state={summary.state}
    >
      <div className="lab-chain-conclusion__heading">
        <div>
          <span className="lab-chain-conclusion__eyebrow">核心链路结论</span>
          <h2>生成 → 策略版本 → 回测 → 评分</h2>
        </div>
        <div>
          <span aria-hidden="true" className="sr-only" data-testid="lab-evidence-status">
            {summary.state}
          </span>
          <StatusBadge
            label={stateDisplay.label}
            showRaw
            status={summary.state}
            tone={stateDisplay.tone}
          />
        </div>
      </div>

      <div className="lab-chain-conclusion__result">
        <div>
          <span>结论</span>
          <strong>{summary.reason}</strong>
        </div>
        <div>
          <span>下一步</span>
          <strong>{summary.nextAction}</strong>
        </div>
      </div>

      <ol className="lab-chain-stages" aria-label="持久证据链阶段">
        {summary.stages.map((stage, index) => {
          const stageDisplay = evidenceStateDisplay(stage.state);
          return (
            <li key={stage.key} data-acceptable={stage.canAccept ? "true" : "false"}>
              <span className="lab-chain-stage__index">{index + 1}</span>
              <div>
                <strong>{stage.label}</strong>
                <span>{stage.coreCount} 条核心 / {stage.observedCount} 条已观察</span>
              </div>
              <StatusBadge
                label={stageDisplay.label}
                showRaw
                status={stage.state}
                tone={stageDisplay.tone}
              />
              <ExpandableText summary="查看阶段原因与下一步" value={`${stage.reason}\n下一步：${stage.nextAction}`} />
            </li>
          );
        })}
      </ol>

      {!summary.canAccept ? (
        <span data-testid="lab-core-evidence-rejection">
          <EmptyState
            description={`没有可证明的核心成功结果。${summary.reason} 下一步：${summary.nextAction}`}
            title={stateDisplay.emptyTitle}
          />
        </span>
      ) : null}

      <div className="lab-core-evidence-count" aria-label="核心记录数量">
        <strong>{records.core.length}</strong>
        <span>条核心持久记录进入下方主链面板</span>
      </div>

      {records.diagnostic.length ? (
        <>
          <h2>非核心诊断记录（不可验收）</h2>
          <details className="lab-non-core-diagnostics" aria-label="非核心诊断记录">
            <summary>查看 {records.diagnostic.length} 条诊断记录</summary>
            <p>这些记录仅解释链路为何未通过，不会混入核心生成、回测或评分结论。</p>
            <div className="table-shell lab-table-shell">
            <table>
              <thead>
                <tr>
                  <th>阶段</th>
                  <th>ID / 父 ID</th>
                  <th>状态</th>
                  <th>Provider / Model</th>
                  <th>Artifact</th>
                  <th>技术来源</th>
                </tr>
              </thead>
              <tbody>
                {records.diagnostic.slice(0, 12).map((record) => (
                  <tr key={`${record.stage}-${record.id}`}>
                    <td>{record.stage}</td>
                    <td>
                      <CopyableValue
                        label="记录 ID"
                        value={record.parentId ? `${record.id} / ${record.parentId}` : record.id}
                      />
                    </td>
                    <td><StatusBadge showRaw status={record.status} /></td>
                    <td><CompactText value={record.provider ? `${record.provider} / ${record.model ?? EMPTY_TEXT}` : EMPTY_TEXT} /></td>
                    <td><CopyableValue label="Artifact 路径" value={record.artifactPath ?? EMPTY_TEXT} /></td>
                    <td><LabSourceSummary source={record.source} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
            </div>
          </details>
        </>
      ) : null}
    </section>
  );
}

function ControlStatePanel({
  data,
  operatorToken,
  recordAction,
}: {
  data: MvpData;
  operatorToken: string;
  recordAction: RecordActionEvidence;
}) {
  const [control, setControl] = useState<ControlState>({ kind: "idle" });
  const [manualApproval, setManualApproval] = useState(false);
  const candidate = readinessCandidate(data);
  const isBusy = control.kind === "starting" || control.kind === "stopping";
  const report = control.kind === "complete" ? control.report : null;
  const snapshot = report?.statusSnapshot ?? data.dryRun.snapshot;
  const manifest = report ? null : data.dryRun.manifest;
  const controlStatus = resolvedControlStatus({
    kind: control.kind,
    report,
    persistedStatus: data.dryRun.snapshot.status,
  });
  const persistedBlockers = dryRunBlockers(manifest, snapshot);
  const actionBlockers = report
    ? [...report.blockedReasons, report.failedReason, report.skippedReason]
      .filter((value): value is string => Boolean(value?.trim()))
    : [];
  const controlBlockers = Array.from(new Set([
    ...(control.kind === "failed" ? [control.message] : []),
    ...actionBlockers,
    ...persistedBlockers,
  ]));
  const safetyConclusion = dryRunSafetyConclusion(snapshot);
  const nextAction = controlNextAction(controlStatus, controlBlockers);
  const manifestPath = report?.manifestPath ?? manifest?.manifestPath ?? snapshot.artifactManifestPath;
  const snapshotPath = report?.statusSnapshotPath ?? EMPTY_TEXT;

  async function handleStart() {
    if (!candidate) {
      const message = "没有可用于受控 dry-run 的核心 strategy version。";
      setControl({ kind: "failed", message });
      recordAction(createActionEvidence({
        action: "启动 controlled dry-run", status: "BLOCKED", message,
        nextAction: "先生成并验证核心 strategy version。", recommendBug: false, updatedAt: new Date().toISOString(),
      }));
      return;
    }

    setControl({ kind: "starting", strategyVersionId: candidate.strategyVersionId });
    recordAction(createActionEvidence({
      action: "启动 controlled dry-run", status: "RUNNING", message: actionStatusMessage("RUNNING"),
      nextAction: "等待受控 dry-run 报告。", recommendBug: false,
      databaseIds: { strategy_version_id: candidate.strategyVersionId }, updatedAt: new Date().toISOString(),
    }));
    try {
      const result = await startControlledDryRun({
        manualApproval,
        strategyName: candidate.strategyName,
        strategyVersionId: candidate.strategyVersionId,
      }, operatorToken);
      setControl({ kind: "complete", report: result });
      const status = result.status === "SUCCESS" ? "SUCCESS" : result.status === "BLOCKED" ? "BLOCKED" : "FAILED";
      recordAction(createActionEvidence({
        action: "启动 controlled dry-run", status,
        message: result.failedReason ?? (result.blockedReasons.join("；") || `受控 dry-run 返回 ${result.status}。`),
        nextAction: status === "SUCCESS" ? "通过 status_snapshot 对账；停止前不允许切换到 live。" : "按报告的原因修复后重试；不要绕过安全边界。",
        recommendBug: status === "FAILED", databaseIds: { strategy_version_id: candidate.strategyVersionId },
        artifactPaths: [result.manifestPath, result.statusSnapshotPath], updatedAt: new Date().toISOString(),
      }));
    } catch (error) {
      const message = apiErrorMessage(error, "受控 dry-run 启动边界请求失败");
      setControl({ kind: "failed", message });
      recordAction(createActionEvidence({
        action: "启动 controlled dry-run", status: apiErrorStatus(error), message,
        nextAction: "检查本地授权与 dry-run report；若可稳定复现，创建 Bug Issue。", recommendBug: true,
        databaseIds: { strategy_version_id: candidate.strategyVersionId }, updatedAt: new Date().toISOString(),
      }));
    }
  }

  async function handleStop() {
    setControl({ kind: "stopping" });
    recordAction(createActionEvidence({
      action: "停止 controlled dry-run", status: "RUNNING", message: actionStatusMessage("RUNNING"),
      nextAction: "等待停止报告。", recommendBug: false, updatedAt: new Date().toISOString(),
    }));
    try {
      const result = await stopControlledDryRun(operatorToken);
      setControl({ kind: "complete", report: result });
      const status = result.status === "STOPPED" || result.status === "SUCCESS" ? "SUCCESS" : result.status === "BLOCKED" ? "BLOCKED" : "FAILED";
      recordAction(createActionEvidence({
        action: "停止 controlled dry-run", status,
        message: result.failedReason ?? (result.blockedReasons.join("；") || `停止请求返回 ${result.status}。`),
        nextAction: "通过 status_snapshot 复核已停止，且不会进入 live trading。", recommendBug: status === "FAILED",
        artifactPaths: [result.manifestPath, result.statusSnapshotPath], updatedAt: new Date().toISOString(),
      }));
    } catch (error) {
      const message = apiErrorMessage(error, "受控 dry-run 停止请求失败");
      setControl({ kind: "failed", message });
      recordAction(createActionEvidence({
        action: "停止 controlled dry-run", status: apiErrorStatus(error), message,
        nextAction: "检查本地授权和受控 runtime 状态；若可稳定复现，创建 Bug Issue。", recommendBug: true,
        updatedAt: new Date().toISOString(),
      }));
    }
  }

  return (
    <section className="lab-control-panel" aria-label="本地受控 dry-run">
      <div className="lab-control-panel__heading">
        <div>
          <span className="lab-readiness-eyebrow">受控运行</span>
          <h2>Controlled dry-run</h2>
        </div>
        <StatusBadge label={displayStatus(controlStatus)} showRaw status={controlStatus} />
      </div>

      <div className="lab-readiness-decision">
        <div>
          <span>当前状态 / 原因</span>
          <strong>{controlBlockers[0] ?? `最新可审计状态为 ${controlStatus}。`}</strong>
        </div>
        <div>
          <span>下一步</span>
          <strong>{nextAction}</strong>
        </div>
      </div>

      {controlBlockers.length > 1 ? (
        <ul className="lab-readiness-blockers" aria-label="Dry-run 其余阻断或失败原因">
          {controlBlockers.slice(1).map((reason) => <li key={reason}>{reason}</li>)}
        </ul>
      ) : null}

      <dl className="lab-readiness-audit">
        <div>
          <dt>候选 strategy_version</dt>
          <dd><CopyableValue label="候选策略版本 ID" value={candidate?.strategyVersionId ?? EMPTY_TEXT} /></dd>
        </div>
        <div>
          <dt>profile</dt>
          <dd><CopyableValue label="Dry-run profile" value={snapshot.profileName ?? manifest?.profileName ?? EMPTY_TEXT} /></dd>
        </div>
        <div>
          <dt>dry_run</dt>
          <dd>{displayBoolean(snapshot.dryRun)}</dd>
        </div>
        <div>
          <dt>manifest</dt>
          <dd><CopyableValue label="Manifest 路径" value={manifestPath ?? EMPTY_TEXT} /></dd>
        </div>
        <div>
          <dt>status_snapshot</dt>
          <dd><CopyableValue label="Status snapshot 路径" value={snapshotPath} /></dd>
        </div>
        <div>
          <dt>安全结论</dt>
          <dd><StatusBadge label={safetyConclusion.status === "PASS" ? "仅 Dry-run" : "安全阻断"} status={safetyConclusion.status} /></dd>
        </div>
      </dl>

      <aside className="lab-control-safety" role="note">
        {safetyConclusion.reason} 本页始终禁止 live trading、连接真实交易执行链路或提交真实订单。
      </aside>

      <div className="lab-control-action-cluster">
        <div className="lab-control-actions">
          <label className="inline-check">
            <input
              checked={manualApproval}
              disabled={isBusy}
              onChange={(event) => setManualApproval(event.target.checked)}
              type="checkbox"
            />
            人工批准本次受控 dry-run
          </label>
          <button className="secondary-button" disabled={isBusy || !candidate || !operatorToken} onClick={handleStart} type="button">
            {control.kind === "starting" ? "启动中" : "启动 Dry-run"}
          </button>
          <button className="secondary-button" disabled={isBusy || !operatorToken} onClick={handleStop} type="button">
            {control.kind === "stopping" ? "停止中" : "停止 Dry-run"}
          </button>
        </div>
        <small className="inline-muted">人工批准只适用于本次本地 dry-run，不会授予 live trading 或真实订单权限。</small>
      </div>

      <details className="lab-readiness-details">
        <summary>展开 snapshot、manifest 和安全证据</summary>
        <dl className="lab-readiness-audit">
          <div>
            <dt>snapshot status</dt>
            <dd><StatusBadge showRaw status={snapshot.status} /></dd>
          </div>
          <div>
            <dt>策略 / pair / timeframe</dt>
            <dd>{snapshot.strategyName ?? manifest?.strategyName ?? EMPTY_TEXT} · {snapshot.pair ?? manifest?.pair ?? EMPTY_TEXT} · {snapshot.timeframe ?? manifest?.timeframe ?? EMPTY_TEXT}</dd>
          </div>
          <div>
            <dt>snapshot 更新时间</dt>
            <dd><CopyableValue label="Snapshot 更新时间" value={snapshot.lastUpdated ?? EMPTY_TEXT} /></dd>
          </div>
          <div>
            <dt>config</dt>
            <dd><CopyableValue label="Dry-run config 路径" value={report?.configPath ?? manifest?.configPath ?? EMPTY_TEXT} /></dd>
          </div>
          <div>
            <dt>report safety</dt>
            <dd><ExpandableText mono value={report ? formatEvidence(report.safety) : safetyConclusion.reason} /></dd>
          </div>
          <div>
            <dt>命令证据</dt>
            <dd><ExpandableText mono value={manifest?.commandArgs.join(" ") ?? EMPTY_TEXT} /></dd>
          </div>
        </dl>

        {snapshot.recentEvents.length ? (
          <div className="table-shell lab-table-shell">
            <table>
              <thead>
                <tr>
                  <th>event</th>
                  <th>severity</th>
                  <th>message</th>
                  <th>source</th>
                </tr>
              </thead>
              <tbody>
                {snapshot.recentEvents.map((event) => (
                  <tr key={`${event.timestamp}:${event.eventType}`}>
                    <td className="primary-cell">{event.eventType}</td>
                    <td><StatusBadge showRaw status={event.severity} /></td>
                    <td><ExpandableText value={event.message} /></td>
                    <td><CompactText value={event.source} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState description="当前 snapshot 没有 recent_events；这不代表运行成功。" title="暂无运行事件" />
        )}
      </details>

      {!candidate ? (
        <EmptyState
          description="先生成并验证核心 strategy version；缺少候选时启动操作保持禁用。"
          title="Controlled dry-run BLOCKED"
        />
      ) : null}
    </section>
  );
}

function WorkflowActionsPanel({
  data,
  operatorToken,
  promptSummary,
  recordAction,
  onRefresh,
}: {
  data: MvpData;
  operatorToken: string;
  promptSummary: string;
  recordAction: RecordActionEvidence;
  onRefresh: () => void;
}) {
  const [activeAction, setActiveAction] = useState<string | null>(null);
  const [allowDeepSeek, setAllowDeepSeek] = useState(false);
  const candidate = readinessCandidate(data);
  const ingestTask = data.backtestTasks.find((task) => task.artifactManifest?.manifestPath || task.resultPath);
  const busy = activeAction !== null;
  const missingToken = !operatorToken;

  function start(action: string, ids: Record<string, string> = {}) {
    setActiveAction(action);
    recordAction(createActionEvidence({
      action, status: "RUNNING", message: actionStatusMessage("RUNNING"), nextAction: "等待 backend API 响应。",
      recommendBug: false, databaseIds: ids, updatedAt: new Date().toISOString(),
    }));
  }

  async function handleBacktest() {
    if (!candidate) return;
    const action = "触发本地回测";
    start(action, { strategy_version_id: candidate.strategyVersionId });
    try {
      const result = await triggerLocalBacktest(candidate.strategyVersionId, operatorToken);
      const run = asRecord(result.run);
      const blocked = result.preflight_status === "blocked";
      const runId = responseId(run);
      recordAction(createActionEvidence({
        action, status: blocked ? "BLOCKED" : runId ? "SUCCESS" : "API_GAP",
        message: blocked
          ? (Array.isArray(result.blocked_reasons) ? result.blocked_reasons.join("；") : "本地回测 preflight 被阻止。")
          : "已创建 preflight-only backtest run；未执行真实交易或下单。",
        nextAction: blocked ? "补齐 preflight 条件后重试。" : "检查 Backtest Runs/Tasks 的持久记录和 artifact 状态。",
        recommendBug: false,
        databaseIds: { strategy_version_id: candidate.strategyVersionId, backtest_run_id: runId },
        updatedAt: new Date().toISOString(),
      }));
      onRefresh();
    } catch (error) {
      recordAction(createActionEvidence({
        action, status: apiErrorStatus(error), message: apiErrorMessage(error, "本地回测请求失败。"),
        nextAction: "检查持久 run/task 和 API 错误；若可稳定复现，创建 Bug Issue。", recommendBug: true,
        databaseIds: { strategy_version_id: candidate.strategyVersionId }, updatedAt: new Date().toISOString(),
      }));
    } finally {
      setActiveAction(null);
    }
  }

  async function handleIngest() {
    if (!ingestTask) return;
    const action = "导入回测结果并计算评分";
    start(action, { backtest_task_id: ingestTask.id });
    try {
      const result = await ingestBacktestArtifact(ingestTask.id, {
        manifestPath: ingestTask.artifactManifest?.manifestPath,
        resultPath: ingestTask.resultPath,
        strategyName: ingestTask.strategyName,
      }, operatorToken);
      const task = asRecord(result.task);
      const run = asRecord(result.run);
      const parsedResult = asRecord(result.result);
      const score = asRecord(result.score);
      const ingestStatus = responseText(result.ingest_status) ?? "failed";
      const resultId = responseId(parsedResult);
      const scoreId = responseId(score);
      const blocked = ingestStatus === "blocked";
      const succeeded = ingestStatus === "succeeded" && resultId && scoreId;
      recordAction(createActionEvidence({
        action, status: succeeded ? "SUCCESS" : blocked ? "BLOCKED" : ingestStatus === "succeeded" ? "API_GAP" : "FAILED",
        message: responseText(result.reason) ?? (succeeded ? "回测结果和 StrategyScore 已写入数据库。" : `artifact ingest 返回 ${ingestStatus}。`),
        nextAction: succeeded ? "刷新并核对 BacktestResult、StrategyScore 与 artifact path。" : "检查 artifact、任务状态和失败原因；不要将不完整结果当作成功。",
        recommendBug: !blocked && !succeeded,
        databaseIds: {
          backtest_run_id: responseId(run), backtest_task_id: responseId(task) ?? ingestTask.id,
          backtest_result_id: resultId, strategy_score_id: scoreId,
        },
        artifactPaths: [ingestTask.artifactManifest?.manifestPath, ingestTask.resultPath], updatedAt: new Date().toISOString(),
      }));
      onRefresh();
    } catch (error) {
      recordAction(createActionEvidence({
        action, status: apiErrorStatus(error), message: apiErrorMessage(error, "artifact ingest 请求失败。"),
        nextAction: "检查 artifact path、持久任务和 API 错误；若可稳定复现，创建 Bug Issue。", recommendBug: true,
        databaseIds: { backtest_task_id: ingestTask.id },
        artifactPaths: [ingestTask.artifactManifest?.manifestPath, ingestTask.resultPath], updatedAt: new Date().toISOString(),
      }));
    } finally {
      setActiveAction(null);
    }
  }

  async function handleDeepSeekSingle() {
    const action = "运行 DeepSeek 单次 E2E";
    start(action);
    try {
      const result = await runDeepSeekSingle(promptSummary, operatorToken, allowDeepSeek);
      const success = isCoreGenerationResult(result);
      recordAction(createActionEvidence({
        action, status: success ? "SUCCESS" : "BLOCKED",
        message: success ? "DeepSeek 单次结果已返回可追踪的 API/DB 证据。" : "响应没有完整核心证据，未展示为成功。",
        nextAction: success ? "刷新并核对 generation run、策略文件和后续回测证据。" : "检查 provider、database_ids 和策略文件；不要将其视为核心成功。",
        recommendBug: false, databaseIds: { strategy_generation_run_id: result.run.id },
        artifactPaths: result.strategyVersions.map((version) => version.filePath), updatedAt: new Date().toISOString(),
      }));
      onRefresh();
    } catch (error) {
      recordAction(createActionEvidence({
        action, status: apiErrorStatus(error), message: apiErrorMessage(error, "DeepSeek 单次请求失败。"),
        nextAction: "确认一次性授权与本地 ENV；不要在页面、日志或 Issue 中记录密钥。", recommendBug: apiErrorStatus(error) === "FAILED",
        updatedAt: new Date().toISOString(),
      }));
    } finally {
      setActiveAction(null);
    }
  }

  return (
    <section className="lab-evidence-section" aria-label="核心工作流操作">
      <div className="section-header detail-section">
        <h2>核心工作流操作</h2>
        <span>所有动作保留结果摘要；不执行 live trading</span>
      </div>
      <div className="lab-header-actions">
        <button className="secondary-button" disabled={busy || missingToken || !candidate} onClick={handleBacktest} type="button">
          {activeAction === "触发本地回测" ? "触发中" : "触发本地回测"}
        </button>
        <button className="secondary-button" disabled={busy || missingToken || !ingestTask} onClick={handleIngest} type="button">
          {activeAction === "导入回测结果并计算评分" ? "导入中" : "导入结果并评分"}
        </button>
        <label className="inline-check">
          <input checked={allowDeepSeek} disabled={busy} onChange={(event) => setAllowDeepSeek(event.target.checked)} type="checkbox" />
          显式授权一次 DeepSeek 调用
        </label>
        <button className="secondary-button" disabled={busy || missingToken || !promptSummary || !allowDeepSeek} onClick={handleDeepSeekSingle} type="button">
          {activeAction === "运行 DeepSeek 单次 E2E" ? "运行中" : "运行 DeepSeek 单次 E2E"}
        </button>
      </div>
      <div className="compact-detail-list">
        <div><dt>本地回测</dt><dd>{candidate ? `候选 strategy_version=${candidate.strategyVersionId}` : "BLOCKED：缺少核心 strategy version。"}</dd></div>
        <div><dt>artifact 导入 / 评分</dt><dd>{ingestTask ? `候选 task=${ingestTask.id}` : "BLOCKED：没有带 artifact path 的核心回测任务。"}</dd></div>
        <div><dt>DeepSeek 单次</dt><dd>默认不调用；必须输入 operator token 并勾选一次性显式授权。</dd></div>
      </div>
    </section>
  );
}

export function PersistentEvidence({
  data,
  error,
  history,
  isLoading,
  onRefresh,
  operatorToken,
  promptSummary,
  recordAction,
  source,
}: {
  data: MvpData;
  error: string | null;
  history: ActionEvidence[];
  isLoading: boolean;
  onRefresh: () => void;
  operatorToken: string;
  promptSummary: string;
  recordAction: RecordActionEvidence;
  source: string;
}) {
  const [refreshPending, setRefreshPending] = useState(false);
  const coreRankingCount = data.ranking.filter((entry) => isCoreDataSource(entry.dataSource)).length;
  const hasCoreEvidence =
    data.strategyVersions.some((version) => isCoreDataSource(version.dataSource)) ||
    data.backtestResults.some((result) => isCoreDataSource(result.dataSource)) ||
    coreRankingCount > 0;
  const evidenceSource = hasCoreEvidence ? "api" : source;
  const evidenceError = hasCoreEvidence ? null : error;

  useEffect(() => {
    if (!refreshPending || isLoading) return;
    recordAction(createActionEvidence({
      action: "刷新数据", status: error ? "FAILED" : "SUCCESS",
      message: error ?? "已重新请求页面使用的 API/DB 数据。",
      nextAction: error ? "检查 API 可用性和数据来源；若可稳定复现，创建 Bug Issue。" : "核对下方核心证据与最新 action feedback。",
      recommendBug: Boolean(error), updatedAt: new Date().toISOString(),
    }));
    setRefreshPending(false);
  }, [error, isLoading, recordAction, refreshPending]);

  function handleRefresh() {
    setRefreshPending(true);
    recordAction(createActionEvidence({
      action: "刷新数据", status: "RUNNING", message: actionStatusMessage("RUNNING"),
      nextAction: "等待 API/DB 快照完成加载。", recommendBug: false, updatedAt: new Date().toISOString(),
    }));
    onRefresh();
  }

  return (
    <section className="lab-results" aria-label="API 和数据库持久证据">
      <div className="section-header">
        <h2>API/DB 持久证据</h2>
        <div className="lab-header-actions">
          <span className="status-pill">{displayLoadState(isLoading, evidenceSource)}</span>
          <button className="secondary-button" disabled={isLoading} onClick={handleRefresh} type="button">
            刷新
          </button>
        </div>
      </div>
      <FallbackNotice
        context="Local Strategy Lab 的策略版本、生成批次、回测任务、回测结果和评分。"
        error={evidenceError}
        isLoading={isLoading}
        source={evidenceSource}
      />
      <ReadinessControlOverview data={data} />
      <DryRunReadinessPanel data={data} recordAction={recordAction} />
      <ControlStatePanel data={data} operatorToken={operatorToken} recordAction={recordAction} />
      <ReadinessDomainPanel data={data} />
      <EvidenceConclusion summary={data.localStrategyLabEvidence} />
      <div className="lab-evidence-summary">
        <div data-testid="lab-strategy-version-count">
          <span>strategy versions</span>
          <strong>{data.strategyVersions.length}</strong>
        </div>
        <div data-testid="lab-backtest-result-count">
          <span>backtest results</span>
          <strong>{data.backtestResults.length}</strong>
        </div>
        <div data-testid="lab-core-ranking-count">
          <span>core ranking</span>
          <strong>{coreRankingCount}</strong>
        </div>
      </div>
      <GenerationRunEvidence runs={data.generationRuns} />
      <StrategyVersionEvidence strategies={data.strategies} versions={data.strategyVersions} />
      <BacktestEvidence runs={data.backtestRuns} tasks={data.backtestTasks} results={data.backtestResults} />
      <RankingEvidence ranking={data.ranking} />
      <ActionEvidenceHistory history={history} />
      <WorkflowActionsPanel
        data={data}
        onRefresh={handleRefresh}
        operatorToken={operatorToken}
        promptSummary={promptSummary}
        recordAction={recordAction}
      />
    </section>
  );
}

export function ResultDetails({ result }: { result: StrategyGenerationApiResult }) {
  const rows = buildSourceRows(result);
  const versions = versionRows(result);

  return (
    <section className="lab-results" aria-label="生成结果">
      <div className="section-header">
        <h2>生成批次</h2>
        <span className={`run-status ${statusClassName(result.run.status)}`}>{displayStatus(result.run.status)}</span>
      </div>
      <dl className="detail-list lab-run-detail-list">
        <div>
          <dt>run id</dt>
          <dd><CopyableValue label="生成记录 ID" value={displayValue(result.run.id)} /></dd>
        </div>
        <div>
          <dt>provider / model</dt>
          <dd>
            {result.run.provider} / {result.run.model}
          </dd>
        </div>
        <div>
          <dt>计数</dt>
          <dd>
            requested {result.run.requestedCount}, generated {result.run.generatedCount}, accepted{" "}
            {result.run.acceptedCount}, failed {result.run.failedCount}
          </dd>
        </div>
        <div>
          <dt>错误</dt>
          <dd>
            {result.run.errorMessage ? (
              <ExpandableText summary="查看完整错误" value={result.run.errorMessage} />
            ) : EMPTY_TEXT}
          </dd>
        </div>
        <div>
          <dt>created_at</dt>
          <dd>{displayValue(result.run.createdAt)}</dd>
        </div>
      </dl>

      <div className="section-header detail-section">
        <h2>Strategy / Version</h2>
        <span>{versions.length} 个版本</span>
      </div>
      <div className="table-shell lab-table-shell">
        <table>
          <thead>
            <tr>
              <th>strategy id</th>
              <th>version id</th>
              <th>名称</th>
              <th>版本</th>
              <th>验证状态</th>
              <th>file path</th>
            </tr>
          </thead>
          <tbody>
            {versions.map(({ strategy, version }) => (
              <tr key={version.id}>
                <td><CopyableValue label="策略 ID" value={displayValue(strategy?.id ?? version.strategyId)} /></td>
                <td><CopyableValue label="版本 ID" value={displayValue(version.id)} /></td>
                <td>{strategy?.name ?? EMPTY_TEXT}</td>
                <td>{version.versionNumber}</td>
                <td>{displayStatus(version.validationStatus)}</td>
                <td className="path-cell"><CopyableValue label="策略文件路径" value={displayValue(version.filePath)} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {versions.length === 0 ? <div className="empty-state">API 未返回 strategy version，不能视为核心成功。</div> : null}

      <div className="section-header detail-section">
        <h2>Data Source</h2>
        <span>source_type / core_data / database_ids</span>
      </div>
      <DataSourceTable rows={rows} />
    </section>
  );
}
