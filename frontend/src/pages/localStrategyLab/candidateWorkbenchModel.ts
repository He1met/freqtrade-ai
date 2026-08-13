import type {
  BacktestResultSummary,
  BacktestRunSummary,
  BacktestTaskSummary,
  DataSourceTraceSummary,
  MvpData,
  RankingEntry,
  StrategyGenerationVersion,
} from "../../api/types";
import type { LocalBacktestProfileV2 } from "../../api/workflowApi";

export type BacktestProfileDraft = {
  profileName: string;
  pair: string;
  timeframe: string;
  timerange: string;
};

export const DEFAULT_BACKTEST_PROFILE_DRAFT: BacktestProfileDraft = {
  profileName: "local-strategy-lab",
  pair: "",
  timeframe: "",
  timerange: "",
};

export type LabSelection = {
  strategyVersionId: string | null;
  backtestRunId: string | null;
  backtestTaskId: string | null;
  backtestResultId: string | null;
  scoreId: string | null;
};

export const EMPTY_LAB_SELECTION: LabSelection = {
  strategyVersionId: null,
  backtestRunId: null,
  backtestTaskId: null,
  backtestResultId: null,
  scoreId: null,
};

export function displayOptionalTradeCount(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? String(value) : "UNKNOWN";
}

function exactDatabaseId(
  source: DataSourceTraceSummary | undefined,
  key: string,
  id: string,
  sourceType = "database",
): boolean {
  if (
    !source ||
    source.coreData !== true ||
    source.providerProvenance === "non-core" ||
    source.providerProvenance === "unknown" ||
    source.sourceType !== sourceType ||
    source.environment.scope !== "current" ||
    source.environment.runnable !== true
  ) {
    return false;
  }
  const databaseId = source.databaseIds[key];
  return databaseId !== undefined && String(databaseId) === id;
}

export function isCurrentCoreVersion(version: StrategyGenerationVersion): boolean {
  return (
    exactDatabaseId(version.dataSource, "strategy_version_id", version.id) &&
    Boolean(version.filePath.trim()) &&
    version.fileState?.exists === true &&
    version.fileState.isFile === true &&
    version.validationStatus.toLowerCase() === "valid"
  );
}

export function isCurrentCoreRun(run: BacktestRunSummary): boolean {
  return exactDatabaseId(run.dataSource, "backtest_run_id", run.id);
}

export function isCurrentCoreTask(task: BacktestTaskSummary): boolean {
  return (
    exactDatabaseId(task.dataSource, "backtest_task_id", task.id) &&
    String(task.dataSource?.databaseIds.backtest_run_id ?? "") === task.runId
  );
}

export function isCurrentCoreResult(result: BacktestResultSummary): boolean {
  return (
    exactDatabaseId(result.dataSource, "backtest_result_id", result.id) &&
    String(result.dataSource.databaseIds.backtest_run_id ?? "") === result.runId &&
    String(result.dataSource.databaseIds.backtest_task_id ?? "") === result.taskId
  );
}

export function isCurrentCoreScore(entry: RankingEntry): boolean {
  return (
    exactDatabaseId(entry.dataSource, "strategy_score_id", entry.scoreId, "api_aggregate") &&
    String(entry.dataSource.databaseIds.strategy_version_id ?? "") === entry.strategyVersionId &&
    entry.backtestResultId !== null &&
    String(entry.dataSource.databaseIds.backtest_result_id ?? "") === entry.backtestResultId
  );
}

export function buildLocalBacktestProfile(
  draft: BacktestProfileDraft,
  strategy: { name: string | null | undefined; path: string | null | undefined },
): { profile: LocalBacktestProfileV2 | null; reason: string | null } {
  const profileName = draft.profileName.trim();
  const pair = draft.pair.trim().toUpperCase();
  const timeframe = draft.timeframe.trim().toLowerCase();
  const timerange = draft.timerange.trim();
  const strategyName = strategy.name?.trim() ?? "";
  const strategyPath = strategy.path?.trim() ?? "";
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{1,119}$/.test(profileName)) {
    return { profile: null, reason: "profile_name 只能使用 2-120 位字母、数字、点、下划线或连字符。" };
  }
  if (!/^[A-Z0-9]+\/[A-Z0-9:]+$/.test(pair)) {
    return { profile: null, reason: "pair 必须是本地市场数据使用的 BASE/QUOTE 格式。" };
  }
  if (!/^\d+[mhdw]$/.test(timeframe)) {
    return { profile: null, reason: "timeframe 必须是类似 5m、1h 或 1d 的 Freqtrade 格式。" };
  }
  const match = timerange.match(/^(\d{8})-(\d{8})$/);
  if (!match || match[1] >= match[2]) {
    return { profile: null, reason: "timerange 必须是起始早于结束的 YYYYMMDD-YYYYMMDD。" };
  }
  if (!strategyName || !strategyPath) {
    return { profile: null, reason: "候选缺少 strategy name/path，不能构造 BacktestProfileV2。" };
  }
  return {
    profile: {
      schema_version: "2",
      profile_name: profileName,
      pair,
      timeframe,
      timerange,
      strategy: { name: strategyName, path: strategyPath },
      data_source: {
        kind: "local",
        exchange: "okx",
        datadir: "user_data/data",
      },
      safety: {
        allow_download: false,
        allow_exchange_connection: false,
        allow_dry_run: false,
        allow_live_trading: false,
        allow_hyperopt: false,
      },
    },
    reason: null,
  };
}

function profileComparable(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  const record = value as Record<string, unknown>;
  const nested = record.profile;
  return nested && typeof nested === "object" && !Array.isArray(nested)
    ? nested as Record<string, unknown>
    : record;
}

export function backtestProfileKey(profile: LocalBacktestProfileV2): string {
  return JSON.stringify({
    pair: profile.pair,
    profile_name: profile.profile_name,
    strategy: profile.strategy,
    timeframe: profile.timeframe,
    timerange: profile.timerange,
  });
}

function runProfileKey(run: BacktestRunSummary): string | null {
  const profile = profileComparable(run.configSnapshot);
  const strategy = profileComparable(profile.strategy);
  const profileName = String(profile.profile_name ?? run.profileName ?? "");
  const pair = String(profile.pair ?? "");
  const timeframe = String(profile.timeframe ?? "");
  const timerange = String(profile.timerange ?? "");
  const strategyName = String(strategy.name ?? "");
  const strategyPath = String(strategy.path ?? "");
  if (!profileName || !pair || !timeframe || !timerange || !strategyName || !strategyPath) return null;
  return JSON.stringify({
    pair,
    profile_name: profileName,
    strategy: { name: strategyName, path: strategyPath },
    timeframe,
    timerange,
  });
}

export function sanitizedBlockedReasons(value: unknown): string {
  if (!Array.isArray(value)) return "本地回测 preflight 被阻断。";
  const reasons = value
    .filter((item): item is string => typeof item === "string")
    .map((item) => item
      .replace(/<[^>]*>/g, "")
      .replace(/\bBearer\s+\S+/gi, "Bearer [REDACTED]")
      .replace(/((?:api[_-]?key|token|secret|password)\s*[:=]\s*)\S+/gi, "$1[REDACTED]")
      .trim()
      .slice(0, 300))
    .filter(Boolean)
    .slice(0, 8);
  return reasons.length > 0 ? reasons.join("；") : "本地回测 preflight 被阻断。";
}

export type CandidateWorkbenchChain = {
  versions: StrategyGenerationVersion[];
  runs: BacktestRunSummary[];
  tasks: BacktestTaskSummary[];
  results: BacktestResultSummary[];
  scores: RankingEntry[];
};

export function candidateWorkbenchChain(data: MvpData, selection: LabSelection): CandidateWorkbenchChain {
  const versions = data.strategyVersions.filter(isCurrentCoreVersion);
  const runs = selection.strategyVersionId
    ? data.backtestRuns.filter(
        (run) =>
          isCurrentCoreRun(run) &&
          run.strategyVersionId === selection.strategyVersionId &&
          String(run.dataSource?.databaseIds.strategy_version_id ?? "") === selection.strategyVersionId,
      )
    : [];
  const tasks = selection.backtestRunId
    ? data.backtestTasks.filter(
        (task) => isCurrentCoreTask(task) && task.runId === selection.backtestRunId,
      )
    : [];
  const results = selection.backtestTaskId
    ? data.backtestResults.filter(
        (result) =>
          isCurrentCoreResult(result) &&
          result.taskId === selection.backtestTaskId &&
          result.runId === selection.backtestRunId,
      )
    : [];
  const scores = selection.backtestResultId && selection.strategyVersionId
    ? data.ranking.filter(
        (entry) =>
          isCurrentCoreScore(entry) &&
          entry.strategyVersionId === selection.strategyVersionId &&
          entry.backtestResultId === selection.backtestResultId,
      )
    : [];
  return { versions, runs, tasks, results, scores };
}

function keepOrOnly<T extends { id: string }>(currentId: string | null, records: T[]): string | null {
  if (currentId && records.some((record) => record.id === currentId)) return currentId;
  return records.length === 1 ? records[0].id : null;
}

function keepOrOnlyScore(currentId: string | null, records: RankingEntry[]): string | null {
  if (currentId && records.some((record) => record.scoreId === currentId)) return currentId;
  return records.length === 1 ? records[0].scoreId : null;
}

export function reconcileLabSelection(data: MvpData, current: LabSelection): LabSelection {
  const versionIds = data.strategyVersions.filter(isCurrentCoreVersion);
  const strategyVersionId = keepOrOnly(current.strategyVersionId, versionIds);
  const afterVersion: LabSelection = { ...EMPTY_LAB_SELECTION, strategyVersionId };
  const versionChain = candidateWorkbenchChain(data, afterVersion);
  const backtestRunId = keepOrOnly(
    strategyVersionId === current.strategyVersionId ? current.backtestRunId : null,
    versionChain.runs,
  );
  const afterRun = { ...afterVersion, backtestRunId };
  const runChain = candidateWorkbenchChain(data, afterRun);
  const backtestTaskId = keepOrOnly(
    backtestRunId === current.backtestRunId ? current.backtestTaskId : null,
    runChain.tasks,
  );
  const afterTask = { ...afterRun, backtestTaskId };
  const taskChain = candidateWorkbenchChain(data, afterTask);
  const backtestResultId = keepOrOnly(
    backtestTaskId === current.backtestTaskId ? current.backtestResultId : null,
    taskChain.results,
  );
  const afterResult = { ...afterTask, backtestResultId };
  const resultChain = candidateWorkbenchChain(data, afterResult);
  const scoreId = keepOrOnlyScore(
    backtestResultId === current.backtestResultId ? current.scoreId : null,
    resultChain.scores,
  );
  return { ...afterResult, scoreId };
}

export function selectLabEntity(
  current: LabSelection,
  key: keyof LabSelection,
  value: string | null,
): LabSelection {
  if (key === "strategyVersionId") {
    return { ...EMPTY_LAB_SELECTION, strategyVersionId: value };
  }
  if (key === "backtestRunId") {
    return { ...current, backtestRunId: value, backtestTaskId: null, backtestResultId: null, scoreId: null };
  }
  if (key === "backtestTaskId") {
    return { ...current, backtestTaskId: value, backtestResultId: null, scoreId: null };
  }
  if (key === "backtestResultId") {
    return { ...current, backtestResultId: value, scoreId: null };
  }
  return { ...current, scoreId: value };
}

export function backtestBlockReason(
  data: MvpData,
  selection: LabSelection,
  operatorToken: string,
  profile: LocalBacktestProfileV2 | null,
  profileReason: string | null,
): string | null {
  if (!operatorToken) return "需要本地 operator token；不会保存到浏览器。";
  if (!selection.strategyVersionId) return "请选择一个当前环境、可运行且文件有效的 strategy version。";
  if (!profile) return profileReason ?? "BacktestProfileV2 不完整。";
  const version = data.strategyVersions.find((item) => item.id === selection.strategyVersionId);
  if (!version || !isCurrentCoreVersion(version)) return "候选已消失或来源不再可验收，请重新选择。";
  const hasRunning = candidateWorkbenchChain(data, selection).runs.some((run) =>
    ["pending", "running"].includes(run.status.toLowerCase()),
  );
  if (hasRunning) return "该候选已有 PENDING/RUNNING 回测；请等待持久状态更新，避免重复提交。";
  const duplicateBlocked = candidateWorkbenchChain(data, selection).runs.find(
    (run) =>
      run.status.toLowerCase() === "blocked" &&
      runProfileKey(run) === backtestProfileKey(profile),
  );
  if (!duplicateBlocked) return null;
  const blockedReasons = duplicateBlocked.configSnapshot?.blocked_reasons;
  const reason = sanitizedBlockedReasons(blockedReasons);
  return `同一候选和 BacktestProfileV2 已有 BLOCKED run=${duplicateBlocked.id}：${reason} 请修改 profile 后再试。`;
}

export function ingestBlockReason(
  data: MvpData,
  selection: LabSelection,
  operatorToken: string,
): string | null {
  if (!operatorToken) return "需要本地 operator token；不会保存到浏览器。";
  if (!selection.backtestTaskId) return "请选择当前候选链上的持久 BacktestTask。";
  const task = candidateWorkbenchChain(data, selection).tasks.find(
    (item) => item.id === selection.backtestTaskId,
  );
  if (!task) return "任务已消失或跨候选链，请重新选择。";
  if (["pending", "running"].includes(task.status.toLowerCase())) return "回测任务仍在运行，请等待持久结果。";
  if (["failed", "blocked", "cancelled"].includes(task.status.toLowerCase())) {
    return `回测任务为 ${task.status}，请先处理失败或阻断原因。`;
  }
  if (!(task.artifactManifest?.manifestPath || task.resultPath)) {
    return "任务缺少可导入的 artifact manifest/result path。";
  }
  const chain = candidateWorkbenchChain(data, selection);
  if (chain.results.some((result) =>
    chain.scores.some((score) => score.backtestResultId === result.id),
  )) {
    return "当前候选链已有持久 BacktestResult 和 StrategyScore，无需重复导入。";
  }
  return null;
}
