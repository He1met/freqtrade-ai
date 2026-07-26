import { useEffect, useRef, useState } from "react";

import {
  CompactText as DisplayCompactText,
  CopyableValue,
  EmptyState,
  ExpandableText,
  StatusBadge,
} from "../../components/DisplayPrimitives";
import {
  StrategyGenerationApiError,
  runDeepSeekSingle,
} from "../../api/client";
import type {
  BacktestResultSummary,
  BacktestRunSummary,
  BacktestTaskSummary,
  DataSource,
  DataSourceTraceSummary,
  LocalStrategyLabEvidenceSummary,
  MvpData,
  RankingEntry,
  StrategyGenerationApiResult,
  StrategyGenerationStrategy,
  StrategyGenerationVersion,
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
  actionStatusMessage,
  createActionEvidence,
  createActionLifecycleId,
  type ActionEvidence,
  type ActionEvidenceEnvironmentScope,
  type ActionEvidenceHistoryState,
} from "./actionEvidence";
import { ActionTimeline, LatestActionFeedback } from "./ActionTimeline";
import { CandidateWorkbench } from "./CandidateWorkbench";
import { useLabSelection } from "./useLabSelection";
import {
  evidenceStateDisplay,
  formatTraceEntries,
  partitionEvidenceRecords,
} from "./evidenceDisplay";
import { deriveProviderCredentialReadiness } from "./generationFormModel";
import type { LabPhase } from "./workflowState";
import { DryRunDecisionPanel } from "./DryRunDecisionPanel";
import "../../styles/local-strategy-lab-evidence.css";

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

type SourceRow = {
  label: string;
  source: DataSourceTraceSummary;
};

type RecordActionEvidence = (entry: ActionEvidence) => void;

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

function sourceEnvironmentScope(source: DataSourceTraceSummary | undefined): ActionEvidenceEnvironmentScope {
  return source?.environment?.scope ?? "unknown";
}

function feedbackEnvironmentScope(sources: Array<DataSourceTraceSummary | undefined>): ActionEvidenceEnvironmentScope {
  const scopes = sources.map(sourceEnvironmentScope);
  if (scopes.includes("current")) return "current";
  if (scopes.includes("historical")) return "historical";
  return "unknown";
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

function AdvancedDeepSeekPanel({
  data,
  history,
  operatorDashboardSource,
  operatorToken,
  promptSummary,
  recordAction,
  onRefresh,
}: {
  data: MvpData;
  history: ActionEvidence[];
  operatorDashboardSource: DataSource;
  operatorToken: string;
  promptSummary: string;
  recordAction: RecordActionEvidence;
  onRefresh: () => void;
}) {
  const [activeAction, setActiveAction] = useState<string | null>(null);
  const [allowDeepSeek, setAllowDeepSeek] = useState(false);
  const busy = activeAction !== null;
  const missingToken = !operatorToken;
  const providerReadiness = deriveProviderCredentialReadiness(
    data.operatorDashboard,
    operatorDashboardSource,
  );

  function start(action: string) {
    const lifecycleId = createActionLifecycleId("generation");
    setActiveAction(action);
    recordAction(createActionEvidence({
      action, lifecycleId, status: "RUNNING", message: actionStatusMessage("RUNNING"), nextAction: "等待 backend API 响应。",
      recommendBug: false, updatedAt: new Date().toISOString(),
    }));
    return lifecycleId;
  }

  async function handleDeepSeekSingle() {
    const action = "运行 DeepSeek 单次 E2E";
    const lifecycleId = start(action);
    try {
      const result = await runDeepSeekSingle(promptSummary, operatorToken, allowDeepSeek);
      const success = isCoreGenerationResult(result);
      recordAction(createActionEvidence({
        action, lifecycleId, status: success ? "SUCCESS" : "BLOCKED",
        message: success ? "DeepSeek 单次结果已返回可追踪的 API/DB 证据。" : "响应没有完整核心证据，未展示为成功。",
        nextAction: success ? "刷新并核对 generation run、策略文件和后续回测证据。" : "检查 provider、database_ids 和策略文件；不要将其视为核心成功。",
        recommendBug: false, databaseIds: { strategy_generation_run_id: result.run.id },
        artifactPaths: result.strategyVersions.map((version) => version.filePath), updatedAt: new Date().toISOString(),
      }));
      onRefresh();
    } catch (error) {
      recordAction(createActionEvidence({
        action, lifecycleId, status: apiErrorStatus(error), message: apiErrorMessage(error, "DeepSeek 单次请求失败。"),
        nextAction: "确认一次性授权与本地 ENV；不要在页面、日志或 Issue 中记录密钥。", recommendBug: apiErrorStatus(error) === "FAILED",
        updatedAt: new Date().toISOString(),
      }));
    } finally {
      setActiveAction(null);
    }
  }

  return (
    <details className="lab-evidence-section">
      <summary>高级 / 受控：DeepSeek 单次 E2E</summary>
      <p>它属于生成阶段，不与普通回测、评分操作并列；默认不会调用真实 Provider。</p>
      <div className="lab-header-actions">
            <label className="inline-check">
              <input checked={allowDeepSeek} disabled={busy} onChange={(event) => setAllowDeepSeek(event.target.checked)} type="checkbox" />
              显式授权一次 DeepSeek 调用
            </label>
            <button className="secondary-button" disabled={busy || missingToken || !promptSummary || !allowDeepSeek || providerReadiness.state !== "ready"} onClick={handleDeepSeekSingle} type="button">
              {activeAction === "运行 DeepSeek 单次 E2E" ? "运行中" : "运行 DeepSeek 单次 E2E"}
            </button>
      </div>
      <p>{providerReadiness.state === "ready" ? `${providerReadiness.label}：${providerReadiness.detail}` : `BLOCKED：${providerReadiness.label}。${providerReadiness.detail}`}</p>
      <p>默认不调用；必须输入 operator token 并勾选一次性显式授权。</p>
      <LatestActionFeedback
        actions={["运行 DeepSeek 单次 E2E"]}
        environmentScope={feedbackEnvironmentScope(data.generationRuns.map((run) => run.dataSource))}
        history={history}
        phase="generation"
      />
    </details>
  );
}

export function PersistentEvidence({
  data,
  dryRunSource,
  error,
  history,
  historyState,
  inspectedPhase,
  isLoading,
  onRefresh,
  operatorDashboardSource,
  onOperatorTokenChange,
  onReconciliationChange,
  operatorToken,
  promptSummary,
  recordAction,
  source,
}: {
  data: MvpData;
  dryRunSource: DataSource;
  error: string | null;
  history: ActionEvidence[];
  historyState: ActionEvidenceHistoryState;
  inspectedPhase: LabPhase;
  isLoading: boolean;
  onRefresh: () => void;
  operatorDashboardSource: DataSource;
  onOperatorTokenChange: (value: string) => void;
  onReconciliationChange: (pending: boolean) => void;
  operatorToken: string;
  promptSummary: string;
  recordAction: RecordActionEvidence;
  source: string;
}) {
  const [refreshPending, setRefreshPending] = useState(false);
  const refreshLifecycleRef = useRef<string | null>(null);
  const { selection, select } = useLabSelection(data);
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
      action: "刷新数据",
      lifecycleId: refreshLifecycleRef.current ?? createActionLifecycleId("system"),
      status: error ? "FAILED" : "SUCCESS",
      message: error ?? "已重新请求页面使用的 API/DB 数据。",
      nextAction: error ? "检查 API 可用性和数据来源；若可稳定复现，创建 Bug Issue。" : "核对下方核心证据与最新 action feedback。",
      recommendBug: Boolean(error), updatedAt: new Date().toISOString(),
    }));
    refreshLifecycleRef.current = null;
    setRefreshPending(false);
  }, [error, isLoading, recordAction, refreshPending]);

  function handleRefresh() {
    const lifecycleId = createActionLifecycleId("system");
    refreshLifecycleRef.current = lifecycleId;
    setRefreshPending(true);
    recordAction(createActionEvidence({
      action: "刷新数据", lifecycleId, status: "RUNNING", message: actionStatusMessage("RUNNING"),
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
      <div className="lab-workflow__stage-heading">
        <span>阶段内容</span>
        <strong>{
          inspectedPhase === "generation"
            ? "策略生成"
            : inspectedPhase === "backtest"
              ? "回测验证"
              : inspectedPhase === "score"
                ? "评分选择"
                : "受控 Dry-run"
        }</strong>
      </div>
      <ActionTimeline
        history={history}
        historyState={historyState}
      />
      {inspectedPhase === "generation" ? (
        <>
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
        </>
      ) : null}
      {inspectedPhase === "backtest" ? (
        <BacktestEvidence runs={data.backtestRuns} tasks={data.backtestTasks} results={data.backtestResults} />
      ) : null}
      {inspectedPhase === "score" ? <RankingEvidence ranking={data.ranking} /> : null}
      {inspectedPhase === "dry-run" ? (
        <DryRunDecisionPanel
          data={data}
          dryRunSource={dryRunSource}
          error={error}
          history={history}
          isLoading={isLoading}
          onOperatorTokenChange={onOperatorTokenChange}
          onReconciliationChange={onReconciliationChange}
          onRefresh={handleRefresh}
          operatorToken={operatorToken}
          recordAction={recordAction}
          selection={selection}
        />
      ) : null}
      <div hidden={inspectedPhase !== "backtest" && inspectedPhase !== "score"}>
        <CandidateWorkbench
          data={data}
          history={history}
          onRefresh={handleRefresh}
          operatorToken={operatorToken}
          recordAction={recordAction}
          selection={selection}
          select={select}
        />
      </div>
      {inspectedPhase === "generation" ? (
        <AdvancedDeepSeekPanel
          data={data}
          history={history}
          onRefresh={handleRefresh}
          operatorDashboardSource={operatorDashboardSource}
          operatorToken={operatorToken}
          promptSummary={promptSummary}
          recordAction={recordAction}
        />
      ) : null}
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
