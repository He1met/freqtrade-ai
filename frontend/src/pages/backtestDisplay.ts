import type {
  BacktestMetricSummary,
  BacktestResultSummary,
  BacktestRunSummary,
  BacktestTaskSummary,
} from "../api/types";

const EMPTY_TEXT = "暂无";

export type MatrixDisplayStatus =
  | "SUCCESS"
  | "FAILED"
  | "BLOCKED"
  | "RESULT_MISSING"
  | "PENDING"
  | "EMPTY";

export type BacktestMatrixMetricRange = {
  label: string;
  min: number | null;
  max: number | null;
  avg: number | null;
  suffix: string;
};

export type BacktestMatrixReasonSummary = {
  status: MatrixDisplayStatus;
  reason: string;
  count: number;
};

export type BacktestMatrixDisplaySummary = {
  status: MatrixDisplayStatus;
  totalTasks: number;
  completedTasks: number;
  strategyCount: number;
  profileCount: number;
  statusCounts: Record<MatrixDisplayStatus, number>;
  metricRanges: BacktestMatrixMetricRange[];
  reasons: BacktestMatrixReasonSummary[];
};

export function statusClassName(status: string): string {
  const normalized = status.toLowerCase();
  if (normalized === "success" || normalized === "succeeded") {
    return "status-success";
  }
  if (normalized === "blocked") {
    return "status-blocked";
  }
  if (normalized === "failed") {
    return "status-failed";
  }
  return "status-neutral";
}

export function formatNumber(value: number | null, suffix = ""): string {
  return value === null ? EMPTY_TEXT : `${value.toFixed(2)}${suffix}`;
}

export function formatInteger(value: number | null): string {
  return value === null ? EMPTY_TEXT : String(value);
}

export function formatMatrixRangeValue(
  label: string,
  value: number | null,
  suffix: string,
): string {
  return label === "交易数" ? formatInteger(value) : formatNumber(value, suffix);
}

export function summarizeText(value: string | null | undefined): string {
  if (!value?.trim()) {
    return EMPTY_TEXT;
  }
  const normalized = value.trim().replace(/\s+/g, " ");
  return normalized.length > 180 ? `${normalized.slice(0, 177)}...` : normalized;
}

export function reasonText(blockedReason: string | null, failedReason: string | null, error?: string | null): string {
  return blockedReason ?? failedReason ?? error ?? EMPTY_TEXT;
}

export function metricRows(metrics: BacktestMetricSummary): Array<[string, string]> {
  return [
    ["收益", formatNumber(metrics.profitPct, "%")],
    ["回撤", formatNumber(metrics.maxDrawdownPct, "%")],
    ["胜率", formatNumber(metrics.winRate === null ? null : metrics.winRate * 100, "%")],
    ["交易数", formatInteger(metrics.totalTrades)],
    ["时间范围", metrics.timerange ?? EMPTY_TEXT],
    ["Sharpe", formatNumber(metrics.sharpe)],
    ["Sortino", formatNumber(metrics.sortino)],
    ["Calmar", formatNumber(metrics.calmar)],
  ];
}

export function backtestResultState(status: string, hasResult: boolean): MatrixDisplayStatus {
  const normalized = status.toLowerCase();
  if ((normalized === "success" || normalized === "succeeded") && !hasResult) {
    return "RESULT_MISSING";
  }
  if (normalized === "success" || normalized === "succeeded") {
    return "SUCCESS";
  }
  if (normalized === "blocked") {
    return "BLOCKED";
  }
  if (normalized === "failed" || normalized === "failure") {
    return "FAILED";
  }
  return "PENDING";
}

export function matrixStatusLabel(status: MatrixDisplayStatus): string {
  if (status === "RESULT_MISSING") {
    return "结果缺失";
  }
  if (status === "EMPTY") {
    return "暂无记录";
  }
  if (status === "PENDING") {
    return "进行中";
  }
  if (status === "SUCCESS") {
    return "成功";
  }
  if (status === "FAILED") {
    return "失败";
  }
  return "已阻塞";
}

export function buildBacktestMatrixSummary(
  runs: BacktestRunSummary[],
  tasks: BacktestTaskSummary[],
  results: BacktestResultSummary[] = [],
): BacktestMatrixDisplaySummary {
  const rows =
    tasks.length > 0
      ? tasks.map((task) => rowFromTask(task, results.find((result) => result.taskId === task.id) ?? null))
      : runs.map((run) => rowFromRun(run, results.find((result) => result.runId === run.id) ?? null));
  const statusCounts: Record<MatrixDisplayStatus, number> = {
    SUCCESS: 0,
    FAILED: 0,
    BLOCKED: 0,
    RESULT_MISSING: 0,
    PENDING: 0,
    EMPTY: 0,
  };
  const strategies = new Set<string>();
  const profiles = new Set<string>();
  const reasonCounts = new Map<string, BacktestMatrixReasonSummary>();

  for (const row of rows) {
    statusCounts[row.status] += 1;
    strategies.add(row.strategyName);
    profiles.add(row.profileName);

    if (
      row.reason !== EMPTY_TEXT &&
      (row.status === "FAILED" || row.status === "BLOCKED" || row.status === "RESULT_MISSING")
    ) {
      const key = `${row.status}:${row.reason}`;
      const existing = reasonCounts.get(key);
      reasonCounts.set(key, {
        status: row.status,
        reason: row.reason,
        count: (existing?.count ?? 0) + 1,
      });
    }
  }

  if (rows.length === 0) {
    statusCounts.EMPTY = 1;
  }

  return {
    status: matrixStatus(statusCounts, rows.length),
    totalTasks: rows.length,
    completedTasks:
      statusCounts.SUCCESS +
      statusCounts.FAILED +
      statusCounts.BLOCKED +
      statusCounts.RESULT_MISSING,
    strategyCount: strategies.size,
    profileCount: profiles.size,
    statusCounts,
    metricRanges: [
      metricRange("收益", rows.map((row) => row.metrics.profitPct), "%"),
      metricRange("回撤", rows.map((row) => row.metrics.maxDrawdownPct), "%"),
      metricRange(
        "胜率",
        rows.map((row) => (row.metrics.winRate === null ? null : row.metrics.winRate * 100)),
        "%",
      ),
      metricRange("交易数", rows.map((row) => row.metrics.totalTrades), ""),
    ],
    reasons: Array.from(reasonCounts.values()).sort((left, right) => right.count - left.count),
  };
}

function rowFromTask(task: BacktestTaskSummary, result: BacktestResultSummary | null) {
  const status = classifyMatrixStatus(
    task.artifactManifest?.status ?? task.status,
    task.blockedReason,
    Boolean(result),
  );
  return {
    status,
    strategyName: task.strategyName,
    profileName: `${task.pair} ${task.timeframe}`,
    metrics: result?.metrics ?? emptyMetrics(),
    reason:
      status === "RESULT_MISSING"
        ? "未找到关联的核心 BacktestResult；任务指标暂不可用。"
        : reasonText(task.blockedReason, task.failedReason, task.errorMessage),
  };
}

function rowFromRun(run: BacktestRunSummary, result: BacktestResultSummary | null) {
  const status = classifyMatrixStatus(
    run.artifactManifest?.status ?? run.status,
    run.blockedReason,
    Boolean(result),
  );
  return {
    status,
    strategyName: run.strategyName,
    profileName: run.profileName,
    metrics: result?.metrics ?? emptyMetrics(),
    reason:
      status === "RESULT_MISSING"
        ? "未找到关联的核心 BacktestResult；批次指标暂不可用。"
        : reasonText(run.blockedReason, run.failedReason),
  };
}

function classifyMatrixStatus(
  status: string,
  blockedReason: string | null,
  hasResult: boolean,
): MatrixDisplayStatus {
  if (blockedReason) {
    return "BLOCKED";
  }

  return backtestResultState(status, hasResult);
}

function matrixStatus(statusCounts: Record<MatrixDisplayStatus, number>, rowCount: number): MatrixDisplayStatus {
  if (rowCount === 0) {
    return "EMPTY";
  }
  if (statusCounts.BLOCKED > 0) {
    return "BLOCKED";
  }
  if (statusCounts.FAILED > 0) {
    return "FAILED";
  }
  if (statusCounts.RESULT_MISSING > 0) {
    return "RESULT_MISSING";
  }
  if (statusCounts.PENDING > 0) {
    return "PENDING";
  }
  return "SUCCESS";
}

function emptyMetrics(): BacktestMetricSummary {
  return {
    profitTotal: null,
    profitPct: null,
    maxDrawdownPct: null,
    winRate: null,
    totalTrades: null,
    timerange: null,
    sharpe: null,
    sortino: null,
    calmar: null,
  };
}

function metricRange(label: string, values: Array<number | null>, suffix: string): BacktestMatrixMetricRange {
  const numericValues = values.filter((value): value is number => value !== null);
  if (numericValues.length === 0) {
    return { label, min: null, max: null, avg: null, suffix };
  }

  const total = numericValues.reduce((sum, value) => sum + value, 0);
  return {
    label,
    min: Math.min(...numericValues),
    max: Math.max(...numericValues),
    avg: total / numericValues.length,
    suffix,
  };
}
