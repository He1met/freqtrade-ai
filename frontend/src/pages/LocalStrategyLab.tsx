import { type FormEvent, useEffect, useMemo, useRef, useState } from "react";

import {
  StrategyGenerationApiError,
  checkDryRunReadiness,
  createStrategyGenerationRun,
  startControlledDryRun,
  stopControlledDryRun,
} from "../api/client";
import { useMvpData } from "../api/useMvpData";
import type {
  BacktestResultSummary,
  BacktestRunSummary,
  BacktestTaskSummary,
  DataSourceTraceSummary,
  DryRunControlReport,
  DryRunReadinessReport,
  MvpData,
  RankingEntry,
  StrategyGenerationApiResult,
  StrategyGenerationStrategy,
  StrategyGenerationVersion,
} from "../api/types";
import { metricRows, reasonText } from "./backtestDisplay";
import { FallbackNotice } from "./FallbackNotice";
import { isCoreDataSource, SourceMarker } from "./SourceMarker";
import { EMPTY_TEXT, displayBoolean, displayLoadState, displayStatus, displayValue } from "./uiCopy";

type SubmissionState =
  | { kind: "idle" }
  | { kind: "submitting"; promptSummary: string; requestedCount: number }
  | { kind: "success"; result: StrategyGenerationApiResult }
  | { kind: "blocked"; message: string; result?: StrategyGenerationApiResult }
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
  | { kind: "blocked"; report: DryRunReadinessReport }
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

const DEFAULT_IDEA =
  "Build a local dry-run only RSI mean reversion strategy with conservative risk checks and no live trading assumptions.";

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

function latest<T>(items: T[], count = 6): T[] {
  return items.slice(0, count);
}

function isCoreSource(source: DataSourceTraceSummary, allowedTypes: string[]): boolean {
  return source.coreData && allowedTypes.includes(source.sourceType) && Object.keys(source.databaseIds).length > 0;
}

function isCoreGenerationResult(result: StrategyGenerationApiResult): boolean {
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
  if (normalized === "succeeded" || normalized === "success") {
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

function submissionStatus(submission: SubmissionState): {
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

function submissionMessage(submission: SubmissionState): string {
  if (submission.kind === "success") {
    return "生成请求已由 backend API 写入数据库；仍需后续验证、回测和人工复核。";
  }
  if (submission.kind === "failed") {
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
              <td>{row.source.sourceType}</td>
              <td className="path-cell">{row.source.sourceDetail}</td>
              <td>{displayBoolean(row.source.coreData)}</td>
              <td className="path-cell">{formatRecord(row.source.databaseIds)}</td>
              <td className="path-cell">{formatRecord(row.source.artifactRefs)}</td>
              <td className="path-cell">{row.source.blockedReason ?? EMPTY_TEXT}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
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
              <th>strategy id</th>
              <th>version id</th>
              <th>名称</th>
              <th>版本</th>
              <th>验证</th>
              <th>file state</th>
              <th>file path</th>
              <th>DB trace</th>
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
                  <td>{version.strategyId}</td>
                  <td>{version.id}</td>
                  <td>{strategy?.name ?? EMPTY_TEXT}</td>
                  <td>{version.versionNumber}</td>
                  <td>{displayStatus(version.validationStatus)}</td>
                  <td>
                    {displayStatus(fileState.status)}
                    {fileState.blockedReason ? (
                      <span className="inline-muted"> {fileState.blockedReason}</span>
                    ) : null}
                  </td>
                  <td className="path-cell">{version.filePath}</td>
                  <td className="source-cell">
                    <SourceMarker source={version.dataSource} />
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
              <th>run id</th>
              <th>状态</th>
              <th>provider / model</th>
              <th>计数</th>
              <th>错误</th>
              <th>DB trace</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((run) => (
              <tr key={run.id}>
                <td>{run.id}</td>
                <td>
                  <span className={`run-status ${statusClassName(run.status)}`}>{displayStatus(run.status)}</span>
                </td>
                <td>
                  {run.provider} / {run.model}
                </td>
                <td>
                  requested {run.requestedCount}, accepted {run.acceptedCount}, failed {run.failedCount}
                </td>
                <td className="reason-cell">{run.errorMessage ?? EMPTY_TEXT}</td>
                <td className="source-cell">
                  <SourceMarker source={run.dataSource} />
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
  const resultByTaskId = new Map(results.map((result) => [result.taskId, result]));
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
              <th>task id</th>
              <th>run / version</th>
              <th>状态</th>
              <th>pair</th>
              <th>result id</th>
              <th>指标</th>
              <th>artifact</th>
              <th>source</th>
              <th>原因</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((task) => {
              const run = runById.get(task.runId);
              const result = resultByTaskId.get(task.id);
              return (
                <tr key={task.id}>
                  <td>{task.id}</td>
                  <td>
                    <div>{task.runId}</div>
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
                  <td>{result?.id ?? EMPTY_TEXT}</td>
                  <td className="metric-summary">
                    {metricRows(result?.metrics ?? task.metrics).map(([label, value]) => (
                      <span key={label}>
                        <strong>{label}</strong>
                        {value}
                      </span>
                    ))}
                  </td>
                  <td className="path-cell">
                    {result?.resultPath ?? task.resultPath ?? EMPTY_TEXT}
                  </td>
                  <td className="source-cell">
                    <SourceMarker source={result?.dataSource ?? task.dataSource} />
                  </td>
                  <td className="reason-cell">{reasonText(task.blockedReason, task.failedReason, task.errorMessage)}</td>
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
              <th>rank</th>
              <th>score id</th>
              <th>strategy / version</th>
              <th>backtest result</th>
              <th>总分</th>
              <th>状态</th>
              <th>file path</th>
              <th>DB trace</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((entry) => (
              <tr key={`${entry.scoreId}-${entry.strategyVersionId}`}>
                <td>{entry.rank}</td>
                <td>{displayValue(entry.scoreId)}</td>
                <td>
                  <div>{entry.strategyId}</div>
                  <div className="secondary-cell">version {entry.strategyVersionId}</div>
                </td>
                <td>{entry.backtestResultId ?? EMPTY_TEXT}</td>
                <td className="score-cell">{formatScore(entry.totalScore)}</td>
                <td>
                  <span className={`run-status ${entry.elimination.eliminated ? "status-failed" : "status-success"}`}>
                    {entry.elimination.eliminated ? "已淘汰" : "已入榜"}
                  </span>
                </td>
                <td className="path-cell">{entry.filePath}</td>
                <td className="source-cell">
                  <SourceMarker source={entry.dataSource} />
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
    return {
      strategyVersionId: ranked.strategyVersionId,
      strategyName: ranked.strategyName,
    };
  }

  const strategyById = new Map(data.strategies.map((strategy) => [strategy.id, strategy]));
  const version = data.strategyVersions.find((item) => isCoreDataSource(item.dataSource));
  if (!version) {
    return null;
  }

  return {
    strategyVersionId: version.id,
    strategyName: strategyById.get(version.strategyId)?.name ?? null,
  };
}

function DryRunReadinessPanel({ data }: { data: MvpData }) {
  const [readiness, setReadiness] = useState<ReadinessState>({ kind: "idle" });
  const candidate = readinessCandidate(data);
  const status = readinessStatus(readiness);
  const isChecking = readiness.kind === "checking";
  const report = readiness.kind === "ready" || readiness.kind === "blocked" ? readiness.report : null;

  async function handleCheck() {
    if (!candidate) {
      setReadiness({ kind: "failed", message: "没有可用于 readiness 检查的核心 strategy version。" });
      return;
    }

    setReadiness({ kind: "checking", strategyVersionId: candidate.strategyVersionId });
    try {
      const result = await checkDryRunReadiness({
        strategyName: candidate.strategyName,
        strategyVersionId: candidate.strategyVersionId,
      });
      setReadiness(result.status === "READY" ? { kind: "ready", report: result } : { kind: "blocked", report: result });
    } catch (error) {
      const message =
        error instanceof StrategyGenerationApiError
          ? error.message
          : error instanceof Error
            ? error.message
            : "readiness API 请求失败";
      setReadiness({ kind: "failed", message });
    }
  }

  return (
    <section className="lab-evidence-section" aria-label="本地 dry-run readiness">
      <div className="section-header detail-section">
        <h2>Dry-run readiness</h2>
        <div className="lab-header-actions">
          <span className={`run-status ${status.className}`} title={status.title}>
            {status.label}
          </span>
          <button className="secondary-button" disabled={isChecking || !candidate} onClick={handleCheck} type="button">
            检查
          </button>
        </div>
      </div>
      <dl className="detail-list lab-run-detail-list">
        <div>
          <dt>strategy_version</dt>
          <dd>{candidate?.strategyVersionId ?? EMPTY_TEXT}</dd>
        </div>
        <div>
          <dt>strategy</dt>
          <dd>{candidate?.strategyName ?? EMPTY_TEXT}</dd>
        </div>
        <div>
          <dt>profile</dt>
          <dd>{report?.profileName ?? EMPTY_TEXT}</dd>
        </div>
        <div>
          <dt>generated_at</dt>
          <dd>{report?.generatedAt ?? EMPTY_TEXT}</dd>
        </div>
      </dl>
      {readiness.kind === "failed" ? <div className="empty-state">{readiness.message}</div> : null}
      {report?.blockedReasons.length ? (
        <div className="blocked-list">
          {report.blockedReasons.map((reason) => (
            <div key={reason}>{reason}</div>
          ))}
        </div>
      ) : null}
      {report ? (
        <div className="table-shell lab-table-shell">
          <table>
            <thead>
              <tr>
                <th>check</th>
                <th>status</th>
                <th>summary</th>
                <th>blocked_reason</th>
                <th>evidence</th>
              </tr>
            </thead>
            <tbody>
              {report.checks.map((check) => (
                <tr key={check.name}>
                  <td className="primary-cell">{check.name}</td>
                  <td>
                    <span className={`run-status ${statusClassName(check.status)}`}>{check.status}</span>
                  </td>
                  <td>{check.summary}</td>
                  <td className="reason-cell">{check.blockedReason ?? EMPTY_TEXT}</td>
                  <td className="path-cell">{formatEvidence(check.evidence)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {!candidate ? <div className="empty-state">暂无核心 strategy version，readiness 保持不可用。</div> : null}
    </section>
  );
}

function ControlStatePanel({ data }: { data: MvpData }) {
  const [control, setControl] = useState<ControlState>({ kind: "idle" });
  const [manualApproval, setManualApproval] = useState(false);
  const candidate = readinessCandidate(data);
  const isBusy = control.kind === "starting" || control.kind === "stopping";
  const report = control.kind === "complete" ? control.report : null;
  const controlStatus =
    control.kind === "starting"
      ? "STARTING"
      : control.kind === "stopping"
        ? "STOPPING"
        : report?.status ?? "未启动";

  async function handleStart() {
    if (!candidate) {
      setControl({ kind: "failed", message: "没有可用于受控 dry-run 的核心 strategy version。" });
      return;
    }

    setControl({ kind: "starting", strategyVersionId: candidate.strategyVersionId });
    try {
      const result = await startControlledDryRun({
        manualApproval,
        strategyName: candidate.strategyName,
        strategyVersionId: candidate.strategyVersionId,
      });
      setControl({ kind: "complete", report: result });
    } catch (error) {
      const message =
        error instanceof StrategyGenerationApiError
          ? error.message
          : error instanceof Error
            ? error.message
            : "受控 dry-run 启动边界请求失败";
      setControl({ kind: "failed", message });
    }
  }

  async function handleStop() {
    setControl({ kind: "stopping" });
    try {
      const result = await stopControlledDryRun();
      setControl({ kind: "complete", report: result });
    } catch (error) {
      const message =
        error instanceof StrategyGenerationApiError
          ? error.message
          : error instanceof Error
            ? error.message
            : "受控 dry-run 停止请求失败";
      setControl({ kind: "failed", message });
    }
  }

  return (
    <section className="lab-evidence-section" aria-label="本地受控 dry-run">
      <div className="section-header detail-section">
        <h2>Controlled dry-run</h2>
        <div className="lab-header-actions">
          <span className={`run-status ${statusClassName(controlStatus)}`}>{displayStatus(controlStatus)}</span>
          <label className="inline-check">
            <input
              checked={manualApproval}
              disabled={isBusy}
              onChange={(event) => setManualApproval(event.target.checked)}
              type="checkbox"
            />
            人工批准
          </label>
          <button className="secondary-button" disabled={isBusy || !candidate} onClick={handleStart} type="button">
            启动
          </button>
          <button className="secondary-button" disabled={isBusy} onClick={handleStop} type="button">
            停止
          </button>
        </div>
      </div>
      <dl className="detail-list lab-run-detail-list">
        <div>
          <dt>strategy_version</dt>
          <dd>{candidate?.strategyVersionId ?? EMPTY_TEXT}</dd>
        </div>
        <div>
          <dt>manifest</dt>
          <dd className="path-cell">{report?.manifestPath ?? EMPTY_TEXT}</dd>
        </div>
        <div>
          <dt>status_snapshot</dt>
          <dd className="path-cell">{report?.statusSnapshotPath ?? EMPTY_TEXT}</dd>
        </div>
        <div>
          <dt>snapshot_status</dt>
          <dd>{report?.statusSnapshot.status ?? EMPTY_TEXT}</dd>
        </div>
        <div>
          <dt>dry_run</dt>
          <dd>{report ? displayBoolean(report.statusSnapshot.dryRun === true) : EMPTY_TEXT}</dd>
        </div>
        <div>
          <dt>safety</dt>
          <dd>{report ? formatEvidence(report.safety) : EMPTY_TEXT}</dd>
        </div>
      </dl>
      {control.kind === "failed" ? <div className="empty-state">{control.message}</div> : null}
      {report?.blockedReasons.length ? (
        <div className="blocked-list">
          {report.blockedReasons.map((reason) => (
            <div key={reason}>{reason}</div>
          ))}
        </div>
      ) : null}
      {report?.failedReason ? <div className="blocked-list">{report.failedReason}</div> : null}
      {report?.skippedReason ? <div className="blocked-list">{report.skippedReason}</div> : null}
      {report ? (
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
              {report.statusSnapshot.recentEvents.map((event) => (
                <tr key={`${event.timestamp}:${event.eventType}`}>
                  <td className="primary-cell">{event.eventType}</td>
                  <td>
                    <span className={`run-status ${statusClassName(event.severity)}`}>
                      {displayStatus(event.severity)}
                    </span>
                  </td>
                  <td>{event.message}</td>
                  <td>{event.source}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {!candidate ? <div className="empty-state">暂无核心 strategy version，受控 dry-run 保持不可用。</div> : null}
    </section>
  );
}

function PersistentEvidence({
  data,
  error,
  isLoading,
  onRefresh,
  source,
}: {
  data: MvpData;
  error: string | null;
  isLoading: boolean;
  onRefresh: () => void;
  source: string;
}) {
  const coreRankingCount = data.ranking.filter((entry) => isCoreDataSource(entry.dataSource)).length;
  const hasCoreEvidence =
    data.strategyVersions.some((version) => isCoreDataSource(version.dataSource)) ||
    data.backtestResults.some((result) => isCoreDataSource(result.dataSource)) ||
    coreRankingCount > 0;
  const evidenceSource = hasCoreEvidence ? "api" : source;
  const evidenceError = hasCoreEvidence ? null : error;

  return (
    <section className="lab-results" aria-label="API 和数据库持久证据">
      <div className="section-header">
        <h2>API/DB 持久证据</h2>
        <div className="lab-header-actions">
          <span className="status-pill">{displayLoadState(isLoading, evidenceSource)}</span>
          <button className="secondary-button" disabled={isLoading} onClick={onRefresh} type="button">
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
      <div className="lab-evidence-summary">
        <div>
          <span>strategy versions</span>
          <strong>{data.strategyVersions.length}</strong>
        </div>
        <div>
          <span>backtest results</span>
          <strong>{data.backtestResults.length}</strong>
        </div>
        <div>
          <span>core ranking</span>
          <strong>{coreRankingCount}</strong>
        </div>
      </div>
      <StrategyVersionEvidence strategies={data.strategies} versions={data.strategyVersions} />
      <GenerationRunEvidence runs={data.generationRuns} />
      <BacktestEvidence runs={data.backtestRuns} tasks={data.backtestTasks} results={data.backtestResults} />
      <RankingEvidence ranking={data.ranking} />
      <DryRunReadinessPanel data={data} />
      <ControlStatePanel data={data} />
    </section>
  );
}

function ResultDetails({ result }: { result: StrategyGenerationApiResult }) {
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
          <dd>{displayValue(result.run.id)}</dd>
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
          <dd>{result.run.errorMessage ?? EMPTY_TEXT}</dd>
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
                <td>{displayValue(strategy?.id ?? version.strategyId)}</td>
                <td>{displayValue(version.id)}</td>
                <td>{strategy?.name ?? EMPTY_TEXT}</td>
                <td>{version.versionNumber}</td>
                <td>{displayStatus(version.validationStatus)}</td>
                <td className="path-cell">{displayValue(version.filePath)}</td>
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

export function LocalStrategyLab() {
  const [idea, setIdea] = useState(DEFAULT_IDEA);
  const [requestedCount, setRequestedCount] = useState(1);
  const [submission, setSubmission] = useState<SubmissionState>({ kind: "idle" });
  const [snapshotRefreshToken, setSnapshotRefreshToken] = useState(0);
  const snapshot = useMvpData(snapshotRefreshToken);
  const controllerRef = useRef<AbortController | null>(null);
  const isSubmitting = submission.kind === "submitting";
  const currentStatus = submissionStatus(submission);
  const currentResult = submission.kind === "success" || submission.kind === "blocked" ? submission.result : undefined;

  const statusRows = useMemo(
    () => [
      ["状态", currentStatus.title],
      ["核心成功", submission.kind === "success" ? "是" : "否"],
      [
        "run id",
        submission.kind === "success" || submission.kind === "blocked"
          ? submission.result?.run.id ?? EMPTY_TEXT
          : submission.kind === "failed"
            ? submission.runId ?? EMPTY_TEXT
            : EMPTY_TEXT,
      ],
      [
        "错误 / 阻塞原因",
        submission.kind === "failed" || submission.kind === "blocked" ? submission.message : EMPTY_TEXT,
      ],
    ],
    [currentStatus.title, submission],
  );

  useEffect(() => {
    return () => {
      controllerRef.current?.abort();
    };
  }, []);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const promptSummary = idea.trim();

    if (!promptSummary) {
      setSubmission({
        kind: "blocked",
        message: "Strategy idea 不能为空；未提交 API 请求。",
      });
      return;
    }

    const controller = new AbortController();
    controllerRef.current?.abort();
    controllerRef.current = controller;
    setSubmission({ kind: "submitting", promptSummary, requestedCount });

    try {
      const result = await createStrategyGenerationRun(
        {
          promptSummary,
          requestedCount,
        },
        controller.signal,
      );

      if (isCoreGenerationResult(result)) {
        setSubmission({ kind: "success", result });
        setSnapshotRefreshToken((current) => current + 1);
        return;
      }

      setSubmission({
        kind: "blocked",
        message:
          "Backend 响应缺少可证明的 api_aggregate/database core_data、database_ids 或 strategy file path；未展示为核心成功。",
        result,
      });
      setSnapshotRefreshToken((current) => current + 1);
    } catch (error) {
      if (error instanceof Error && error.name === "AbortError") {
        return;
      }

      if (error instanceof StrategyGenerationApiError) {
        const hasPersistedFailedRun = Boolean(error.strategyGenerationRunId || error.failedReason);
        if (hasPersistedFailedRun) {
          setSubmission({
            kind: "failed",
            message: error.failedReason ?? error.message,
            runId: error.strategyGenerationRunId,
            statusCode: error.status,
            statusText: error.statusText,
          });
          return;
        }

        setSubmission({
          kind: "blocked",
          message: `Strategy generation API 不可用或返回非核心响应：${error.message}`,
        });
        return;
      }

      setSubmission({
        kind: "blocked",
        message: error instanceof Error ? error.message : "Strategy generation API 请求失败。",
      });
    }
  }

  return (
    <section className="page local-strategy-lab">
      <header className="page-header">
        <h1>Local Strategy Lab</h1>
        <span className={`run-status ${currentStatus.className}`}>{currentStatus.label}</span>
      </header>

      <aside className="notice lab-safety-notice" role="status">
        本页只提交本地策略生成请求；不连接交易所、不启动 live trading、不下真实订单，也不表示生产就绪。
      </aside>

      <form className="lab-form" onSubmit={handleSubmit}>
        <label className="field-group" htmlFor="strategy-idea">
          <span>Strategy idea</span>
          <textarea
            id="strategy-idea"
            maxLength={4000}
            minLength={1}
            onChange={(event) => setIdea(event.currentTarget.value)}
            required
            rows={7}
            value={idea}
          />
        </label>
        <div className="lab-form-actions">
          <label className="field-group compact-field" htmlFor="requested-count">
            <span>requested_count</span>
            <input
              id="requested-count"
              max={5}
              min={1}
              onChange={(event) => {
                const nextValue = Number(event.currentTarget.value);
                if (Number.isFinite(nextValue)) {
                  setRequestedCount(Math.min(5, Math.max(1, Math.trunc(nextValue))));
                }
              }}
              type="number"
              value={requestedCount}
            />
          </label>
          <button className="primary-button" disabled={isSubmitting || !idea.trim()} type="submit">
            {isSubmitting ? "提交中" : "提交生成"}
          </button>
        </div>
      </form>

      <section className="lab-status-panel" aria-live="polite">
        <div className="lab-status-heading">
          <span className={`run-status ${currentStatus.className}`}>{currentStatus.label}</span>
          <strong>{submissionMessage(submission)}</strong>
        </div>
        <dl className="compact-detail-list">
          {statusRows.map(([label, value]) => (
            <div key={label}>
              <dt>{label}</dt>
              <dd>{value}</dd>
            </div>
          ))}
          {submission.kind === "failed" ? (
            <div>
              <dt>HTTP</dt>
              <dd>
                {submission.statusCode ?? EMPTY_TEXT} {submission.statusText ?? ""}
              </dd>
            </div>
          ) : null}
        </dl>
      </section>

      <PersistentEvidence
        data={snapshot.data}
        error={snapshot.error}
        isLoading={snapshot.isLoading}
        onRefresh={() => setSnapshotRefreshToken((current) => current + 1)}
        source={snapshot.source}
      />

      {currentResult ? <ResultDetails result={currentResult} /> : null}
    </section>
  );
}
