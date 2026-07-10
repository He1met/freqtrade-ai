import type {
  AcceptanceStateSummary,
  DataSource,
  DataSourceTraceSummary,
  MvpDataSetKey,
  MvpDataSources,
} from "./types";

export type CoreDataSourceType = "database" | "api_aggregate";
export type NonCoreDataSourceType = "fixture" | "fallback" | "mock" | "unknown";

export type DataSourceAcceptance = {
  canAccept: boolean;
  sourceType: string;
  state: AcceptanceStateSummary["state"];
  reason: string;
  nextAction: string;
};

export const CORE_DATA_SOURCE_TYPES: CoreDataSourceType[] = ["database", "api_aggregate"];
export const NON_CORE_DATA_SOURCE_TYPES: NonCoreDataSourceType[] = ["fixture", "fallback", "mock", "unknown"];
const DEFAULT_SOURCE_DETAIL = "Source metadata was not provided.";
const NOT_RUN_PATTERNS = [/not run/i, /not been run/i, /no run/i, /未运行/, /尚未运行/, /未触发/, /等待首次运行/];
const FAILURE_PATTERNS = [/failed/i, /failure/i, /error/i, /exception/i, /crash/i, /退出码/i, /失败/];

export const MVP_DATA_SET_KEYS: MvpDataSetKey[] = [
  "strategies",
  "strategyVersions",
  "generationRuns",
  "backtestRuns",
  "backtestTasks",
  "backtestResults",
  "hyperoptRuns",
  "dryRun",
  "liveCandidates",
  "operatorDashboard",
  "ranking",
  "failureReasons",
  "versionLineage",
];

export function fallbackMvpDataSources(): MvpDataSources {
  return Object.fromEntries(MVP_DATA_SET_KEYS.map((key) => [key, "failed"])) as MvpDataSources;
}

export function combineDataSources(
  sources: MvpDataSources,
  keys: MvpDataSetKey[],
): DataSource {
  if (keys.length === 0) {
    return "failed";
  }
  if (keys.every((key) => sources[key] === "api")) return "api";
  if (keys.some((key) => sources[key] === "failed")) return "failed";
  return "fixture";
}

export function hasDatabaseIds(source: DataSourceTraceSummary | undefined): boolean {
  return Boolean(source && Object.keys(source.databaseIds).length > 0);
}

export function isCoreDataSourceTrace(source: DataSourceTraceSummary | undefined): boolean {
  return Boolean(
    source?.coreData &&
      CORE_DATA_SOURCE_TYPES.includes(source.sourceType as CoreDataSourceType) &&
      hasDatabaseIds(source),
  );
}

export function isNonCoreDataSourceTrace(source: DataSourceTraceSummary | undefined): boolean {
  if (!source) {
    return true;
  }
  return (
    NON_CORE_DATA_SOURCE_TYPES.includes(source.sourceType as NonCoreDataSourceType) ||
    !isCoreDataSourceTrace(source)
  );
}

function hasDefaultMetadata(source: DataSourceTraceSummary | undefined): boolean {
  return !source || !source.sourceType || !source.sourceDetail || source.sourceDetail === DEFAULT_SOURCE_DETAIL;
}

function inferNotRun(source: DataSourceTraceSummary | undefined): boolean {
  const sourceType = source?.sourceType ?? "unknown";
  const detail = source?.sourceDetail ?? "";
  if (sourceType === "unknown" && hasDefaultMetadata(source)) {
    return false;
  }
  return NOT_RUN_PATTERNS.some((pattern) => pattern.test(detail));
}

function inferFailed(source: DataSourceTraceSummary | undefined): boolean {
  const sourceType = source?.sourceType ?? "unknown";
  const detail = source?.sourceDetail ?? "";
  if (sourceType === "failed" || sourceType === "error") {
    return true;
  }
  return FAILURE_PATTERNS.some((pattern) => pattern.test(detail));
}

function isCoreType(sourceType: string): sourceType is CoreDataSourceType {
  return CORE_DATA_SOURCE_TYPES.includes(sourceType as CoreDataSourceType);
}

function getApiGapReason(source: DataSourceTraceSummary | undefined, sourceType: string): string {
  if (!source) {
    return "Backend did not provide data_source metadata.";
  }
  if (isCoreType(sourceType) && !hasDatabaseIds(source)) {
    return `${sourceType} source is missing database_ids.`;
  }
  if (source.coreData && !isCoreType(sourceType)) {
    return `${sourceType} source cannot prove acceptance with core_data=true.`;
  }
  if (sourceType === "unknown") {
    return "Backend did not provide a trustworthy data_source classification.";
  }
  return "Source metadata is missing or incomplete.";
}

export function getDataSourceAcceptance(source: DataSourceTraceSummary | undefined): DataSourceAcceptance {
  const sourceType = source?.sourceType ?? "unknown";

  if (isCoreDataSourceTrace(source)) {
    return {
      state: "ACCEPTABLE",
      canAccept: true,
      sourceType,
      reason: "database/api_aggregate source with core_data=true and database_ids.",
      nextAction: "可用于核心验收；刷新后仍需保持相同 database_ids。",
    };
  }

  if (source?.blockedReason) {
    return {
      state: "BLOCKED",
      canAccept: false,
      sourceType,
      reason: source.blockedReason,
      nextAction: `解除 BLOCKED：${source.blockedReason}`,
    };
  }

  if (inferNotRun(source)) {
    return {
      state: "NOT_RUN",
      canAccept: false,
      sourceType,
      reason: source?.sourceDetail ?? "尚未运行真实本地流程。",
      nextAction: "先运行真实本地流程，再刷新页面确认 database_ids、artifact_refs 和 data_source 证据。",
    };
  }

  if (inferFailed(source)) {
    return {
      state: "FAILED",
      canAccept: false,
      sourceType,
      reason: source?.sourceDetail ?? "真实流程执行失败。",
      nextAction: "检查失败原因并修复后重跑；刷新页面确认失败记录已被新的真实证据替换。",
    };
  }

  if (hasDefaultMetadata(source) || (isCoreType(sourceType) && !hasDatabaseIds(source)) || (source?.coreData && !isCoreType(sourceType))) {
    return {
      state: "API_GAP",
      canAccept: false,
      sourceType,
      reason: getApiGapReason(source, sourceType),
      nextAction: "修复 API data_source contract，返回 source_type、core_data、database_ids、artifact_refs 和可复核说明。",
    };
  }

  if (NON_CORE_DATA_SOURCE_TYPES.includes(sourceType as NonCoreDataSourceType)) {
    return {
      state: "NOT_ACCEPTABLE",
      canAccept: false,
      sourceType,
      reason: `${sourceType} data is non-core and cannot prove real-run success.`,
      nextAction: "运行真实本地流程并确认 API 返回 database/api_aggregate、core_data=true 和 database_ids。",
    };
  }

  if (CORE_DATA_SOURCE_TYPES.includes(sourceType as CoreDataSourceType) && !hasDatabaseIds(source)) {
    return {
      state: "API_GAP",
      canAccept: false,
      sourceType,
      reason: `${sourceType} source is missing database_ids.`,
      nextAction: "修复 API data_source contract，返回 database_ids 后再作为核心验收。",
    };
  }

  return {
    state: "API_GAP",
    canAccept: false,
    sourceType,
    reason: "Source metadata is missing or incomplete.",
    nextAction: "修复 API data_source contract，返回 source_type、core_data、database_ids 和解除条件。",
  };
}
