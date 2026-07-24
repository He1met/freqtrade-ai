import type { HyperoptRunSummary } from "../api/types";

const EMPTY_TEXT = "暂无";
const SUCCESS_STATUSES = new Set(["success", "succeeded"]);
const BLOCKED_STATUSES = new Set(["blocked", "stale", "unavailable"]);
const FAILED_STATUSES = new Set(["error", "failed", "failure", "rejected"]);

function normalizeStatus(status: string | null | undefined): string {
  return status?.trim().toLowerCase() ?? "";
}

export function effectiveHyperoptStatus(run: HyperoptRunSummary): string {
  const statuses = [
    run.status,
    run.artifactManifest?.status,
    run.comparison?.status,
  ].filter((status): status is string => Boolean(status));

  if (
    run.blockedReason ||
    run.artifactManifest?.blockedReason ||
    run.comparison?.blockedReason ||
    statuses.some((status) => BLOCKED_STATUSES.has(normalizeStatus(status)))
  ) {
    return "BLOCKED";
  }
  if (
    run.failedReason ||
    run.artifactManifest?.failedReason ||
    run.comparison?.failedReason ||
    statuses.some((status) => FAILED_STATUSES.has(normalizeStatus(status)))
  ) {
    return "FAILED";
  }
  if (statuses.some((status) => normalizeStatus(status) === "running")) {
    return "RUNNING";
  }
  if (statuses.length > 0 && statuses.every((status) => SUCCESS_STATUSES.has(normalizeStatus(status)))) {
    return "SUCCESS";
  }
  const unresolvedStatus = statuses.find((status) => !SUCCESS_STATUSES.has(normalizeStatus(status)));
  return unresolvedStatus?.toUpperCase() ?? "UNKNOWN";
}

export function hyperoptRunReason(run: HyperoptRunSummary): string {
  return (
    run.blockedReason ??
    run.artifactManifest?.blockedReason ??
    run.comparison?.blockedReason ??
    run.failedReason ??
    run.artifactManifest?.failedReason ??
    run.comparison?.failedReason ??
    EMPTY_TEXT
  );
}

export function hasUsableHyperoptBestResult(run: HyperoptRunSummary): boolean {
  return (
    effectiveHyperoptStatus(run) === "SUCCESS" &&
    Object.keys(run.bestParams).length > 0 &&
    run.bestLoss !== null &&
    Boolean(run.resultPath ?? run.artifactManifest?.resultPath)
  );
}

export function firstUsableHyperoptBestRun(
  runs: HyperoptRunSummary[],
): HyperoptRunSummary | null {
  return runs.find(hasUsableHyperoptBestResult) ?? null;
}

export function hyperoptParamsPreview(
  params: Record<string, unknown>,
  limit = 3,
): Array<[string, string]> {
  return Object.entries(params)
    .slice(0, limit)
    .map(([key, value]) => [key, formatHyperoptValue(value)]);
}

export function formatHyperoptValue(value: unknown): string {
  if (value === null || value === undefined || value === "") {
    return EMPTY_TEXT;
  }
  if (typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

export function formatHyperoptParamsJson(params: Record<string, unknown>): string {
  if (Object.keys(params).length === 0) {
    return EMPTY_TEXT;
  }
  return JSON.stringify(params, null, 2);
}

export function countHyperoptStatuses(runs: HyperoptRunSummary[]): Record<string, number> {
  return runs.reduce<Record<string, number>>((counts, run) => {
    const status = effectiveHyperoptStatus(run);
    counts[status] = (counts[status] ?? 0) + 1;
    return counts;
  }, {});
}
