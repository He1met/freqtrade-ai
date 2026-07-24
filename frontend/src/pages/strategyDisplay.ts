import type { DataSourceTraceSummary, StrategySummary } from "../api/types";

const EMPTY_TEXT = "暂无";

const PROBLEM_STATUSES = new Set([
  "blocked",
  "error",
  "failed",
  "failure",
  "missing",
  "rejected",
  "stale",
  "unavailable",
]);

const DIFF_LABELS: Readonly<Record<string, string>> = {
  added: "新增",
  changed: "变更",
  checksum: "校验和",
  current: "当前值",
  previous: "原值",
  removed: "移除",
  validation_errors: "校验错误",
};

export type StrategyAvailability = {
  isProblem: boolean;
  reason: string | null;
  status: string;
};

function normalizeStatus(status: string | null | undefined): string {
  return status?.trim().toLowerCase() ?? "";
}

export function isStrategyProblemStatus(status: string | null | undefined): boolean {
  return PROBLEM_STATUSES.has(normalizeStatus(status));
}

export function strategyAvailability(strategy: StrategySummary): StrategyAvailability {
  const sourceBlocker = strategy.dataSource?.blockedReason?.trim() || null;
  if (sourceBlocker) {
    return { status: "BLOCKED", reason: sourceBlocker, isProblem: true };
  }

  if (!strategy.currentVersion) {
    return {
      status: "MISSING",
      reason: "尚无当前版本，不能确认策略文件是否可用。",
      isProblem: true,
    };
  }

  const validationStatus = strategy.currentVersion.validationStatus;
  if (isStrategyProblemStatus(validationStatus)) {
    return {
      status: validationStatus,
      reason:
        strategy.currentVersion.validationErrors[0]?.message ??
        "当前版本未通过校验，不能作为可用策略。",
      isProblem: true,
    };
  }

  if (isStrategyProblemStatus(strategy.status)) {
    return {
      status: strategy.status,
      reason: "策略当前处于失败或阻塞状态。",
      isProblem: true,
    };
  }

  return {
    status: validationStatus || strategy.status || "UNKNOWN",
    reason: null,
    isProblem: false,
  };
}

export function formatTraceRecord(record: Record<string, number | string> | undefined): string {
  if (!record) {
    return EMPTY_TEXT;
  }
  const entries = Object.entries(record);
  return entries.length > 0
    ? entries.map(([key, value]) => `${key}: ${value}`).join(", ")
    : EMPTY_TEXT;
}

export function formatSourceTrace(source: DataSourceTraceSummary | undefined): string {
  if (!source) {
    return [
      "来源类型（source_type）：unknown",
      "核心数据（core_data）：否",
      "数据库 ID（database_ids）：暂无",
      "Artifact 引用（artifact_refs）：暂无",
      "详情：后端未提供数据来源元数据。",
    ].join("\n");
  }

  return [
    `来源类型（source_type）：${source.sourceType}`,
    `核心数据（core_data）：${source.coreData ? "是" : "否"}`,
    `数据库 ID（database_ids）：${formatTraceRecord(source.databaseIds)}`,
    `Artifact 引用（artifact_refs）：${formatTraceRecord(source.artifactRefs)}`,
    `详情：${source.sourceDetail}`,
    source.blockedReason ? `阻塞原因：${source.blockedReason}` : null,
  ]
    .filter((line): line is string => Boolean(line))
    .join("\n");
}

export function formatDiffLabel(label: string): string {
  return DIFF_LABELS[label] ?? label.replace(/_/g, " ");
}

export function formatDiffValue(value: unknown): string {
  if (Array.isArray(value)) {
    return value.length > 0 ? value.map((item) => formatDiffValue(item)).join("\n") : EMPTY_TEXT;
  }
  if (value === null || value === undefined || value === "") {
    return EMPTY_TEXT;
  }
  if (typeof value === "object") {
    try {
      return JSON.stringify(value, null, 2);
    } catch {
      return String(value);
    }
  }
  return String(value);
}
