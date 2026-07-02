import type { BacktestMetricSummary } from "../api/types";

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
  return value === null ? "none" : `${value.toFixed(2)}${suffix}`;
}

export function formatInteger(value: number | null): string {
  return value === null ? "none" : String(value);
}

export function summarizeText(value: string | null | undefined): string {
  if (!value?.trim()) {
    return "none";
  }
  const normalized = value.trim().replace(/\s+/g, " ");
  return normalized.length > 180 ? `${normalized.slice(0, 177)}...` : normalized;
}

export function reasonText(blockedReason: string | null, failedReason: string | null, error?: string | null): string {
  return blockedReason ?? failedReason ?? error ?? "none";
}

export function metricRows(metrics: BacktestMetricSummary): Array<[string, string]> {
  return [
    ["Profit", formatNumber(metrics.profitPct, "%")],
    ["Drawdown", formatNumber(metrics.maxDrawdownPct, "%")],
    ["Win rate", formatNumber(metrics.winRate === null ? null : metrics.winRate * 100, "%")],
    ["Trades", formatInteger(metrics.totalTrades)],
    ["Timerange", metrics.timerange ?? "none"],
    ["Sharpe", formatNumber(metrics.sharpe)],
    ["Sortino", formatNumber(metrics.sortino)],
    ["Calmar", formatNumber(metrics.calmar)],
  ];
}
