import type { DataSource, DataSourceTraceSummary, MvpDataSetKey, MvpDataSources } from "./types";

export type CoreDataSourceType = "database" | "api_aggregate";
export type NonCoreDataSourceType = "fixture" | "fallback" | "mock" | "unknown";

export type DataSourceAcceptance = {
  canAccept: boolean;
  sourceType: string;
  reason: string;
  requiredAction: string;
};

export const CORE_DATA_SOURCE_TYPES: CoreDataSourceType[] = ["database", "api_aggregate"];
export const NON_CORE_DATA_SOURCE_TYPES: NonCoreDataSourceType[] = ["fixture", "fallback", "mock", "unknown"];

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
  return Object.fromEntries(MVP_DATA_SET_KEYS.map((key) => [key, "fallback"])) as MvpDataSources;
}

export function combineDataSources(
  sources: MvpDataSources,
  keys: MvpDataSetKey[],
): DataSource {
  if (keys.length === 0) {
    return "fallback";
  }
  return keys.every((key) => sources[key] === "api") ? "api" : "fallback";
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

export function getDataSourceAcceptance(source: DataSourceTraceSummary | undefined): DataSourceAcceptance {
  const sourceType = source?.sourceType ?? "unknown";

  if (isCoreDataSourceTrace(source)) {
    return {
      canAccept: true,
      sourceType,
      reason: "database/api_aggregate source with core_data=true and database_ids.",
      requiredAction: "可用于核心验收；刷新后仍需保持相同 database_ids。",
    };
  }

  if (source?.blockedReason) {
    return {
      canAccept: false,
      sourceType,
      reason: source.blockedReason,
      requiredAction: `解除 BLOCKED：${source.blockedReason}`,
    };
  }

  if (NON_CORE_DATA_SOURCE_TYPES.includes(sourceType as NonCoreDataSourceType)) {
    return {
      canAccept: false,
      sourceType,
      reason: `${sourceType} data is non-core and cannot prove real-run success.`,
      requiredAction: "运行真实本地流程并确认 API 返回 database/api_aggregate、core_data=true 和 database_ids。",
    };
  }

  if (CORE_DATA_SOURCE_TYPES.includes(sourceType as CoreDataSourceType) && !hasDatabaseIds(source)) {
    return {
      canAccept: false,
      sourceType,
      reason: `${sourceType} source is missing database_ids.`,
      requiredAction: "修复 API data_source contract，返回 database_ids 后再作为核心验收。",
    };
  }

  return {
    canAccept: false,
    sourceType,
    reason: "Source metadata is missing or incomplete.",
    requiredAction: "修复 API data_source contract，返回 source_type、core_data、database_ids 和解除条件。",
  };
}
