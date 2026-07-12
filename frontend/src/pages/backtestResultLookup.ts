import type { BacktestMetricSummary, BacktestResultSummary } from "../api/types";

export function emptyBacktestMetrics(): BacktestMetricSummary {
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

export function findBacktestResultForTask(
  results: BacktestResultSummary[],
  taskId: string,
): BacktestResultSummary | null {
  return results.find((result) => result.taskId === taskId) ?? null;
}

export function findBacktestResultForRun(
  results: BacktestResultSummary[],
  runId: string,
): BacktestResultSummary | null {
  return results.find((result) => result.runId === runId) ?? null;
}

export function missingBacktestResultReason(scope: "任务" | "批次"): string {
  return `未找到关联的核心 BacktestResult；该${scope}的指标暂不可用。`;
}
