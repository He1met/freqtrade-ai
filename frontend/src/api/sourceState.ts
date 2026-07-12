import type {
  AcceptanceStateSummary,
  DataSource,
  DataSourceTraceSummary,
  BacktestResultSummary,
  LocalStrategyLabEvidenceRecord,
  LocalStrategyLabEvidenceStage,
  LocalStrategyLabEvidenceSummary,
  MvpDataSetKey,
  MvpDataSources,
  RankingEntry,
  StrategyGenerationApiResult,
  StrategyGenerationRunDetail,
  StrategyGenerationVersion,
  StrategySummary,
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
const NON_CORE_PROVIDER_PATTERNS = [/fake/i, /fixture/i, /offline/i, /mock/i];

export type ProviderProvenance = "real" | "non-core" | "unknown";

export function classifyGenerationProvider(provider: string | null | undefined, model: string | null | undefined): ProviderProvenance {
  const providerName = provider?.trim().toLowerCase() ?? "";
  const modelName = model?.trim().toLowerCase() ?? "";

  if (!providerName || providerName === "unknown" || !modelName || modelName === "unknown") {
    return "unknown";
  }
  if (NON_CORE_PROVIDER_PATTERNS.some((pattern) => pattern.test(providerName) || pattern.test(modelName))) {
    return "non-core";
  }
  return providerName === "deepseek" ? "real" : "unknown";
}

function providerProvenanceDetail(
  provider: string,
  model: string,
  provenance: Exclude<ProviderProvenance, "real">,
): string {
  if (provenance === "non-core") {
    return `Provider ${provider}/${model} is fake, fixture, offline, or mock data; database persistence cannot prove a real Provider run.`;
  }
  return `Provider provenance for ${provider}/${model} is unknown; database persistence cannot prove a real Provider run.`;
}

function applyProviderProvenance(
  source: DataSourceTraceSummary,
  provider: string,
  model: string,
): DataSourceTraceSummary {
  const providerProvenance = classifyGenerationProvider(provider, model);
  if (providerProvenance === "real") {
    return { ...source, providerProvenance, providerName: provider, providerModel: model };
  }

  const detail = providerProvenanceDetail(provider, model, providerProvenance);
  return {
    ...source,
    coreData: false,
    sourceDetail: source.sourceDetail.includes(detail) ? source.sourceDetail : `${source.sourceDetail} ${detail}`,
    providerProvenance,
    providerName: provider,
    providerModel: model,
  };
}

export function applyGenerationRunProviderProvenance(run: StrategyGenerationRunDetail): StrategyGenerationRunDetail {
  return { ...run, dataSource: applyProviderProvenance(run.dataSource, run.provider, run.model) };
}

export function applyGenerationVersionProviderProvenance(
  version: StrategyGenerationVersion,
  run: StrategyGenerationRunDetail | undefined,
): StrategyGenerationVersion {
  if (version.generationRunId === null) {
    return version;
  }
  return {
    ...version,
    dataSource: applyProviderProvenance(
      version.dataSource,
      run?.provider ?? "unknown",
      run?.model ?? "unknown",
    ),
  };
}

export function applyStrategyProviderProvenance(
  strategy: StrategySummary,
  currentVersion: StrategyGenerationVersion | null,
): StrategySummary {
  if (!currentVersion || currentVersion.generationRunId === null) {
    return strategy;
  }
  return { ...strategy, dataSource: { ...currentVersion.dataSource } };
}

export function applyGenerationResponseProviderProvenance(
  result: StrategyGenerationApiResult,
): StrategyGenerationApiResult {
  const run = applyGenerationRunProviderProvenance(result.run);
  const strategyVersions = result.strategyVersions.map((version) =>
    applyGenerationVersionProviderProvenance(version, run),
  );
  const versionByStrategyId = new Map(strategyVersions.map((version) => [version.strategyId, version]));
  const strategies = result.strategies.map((strategy) => {
    const version = versionByStrategyId.get(strategy.id) ?? null;
    if (!version) {
      return strategy;
    }
    return { ...strategy, dataSource: { ...version.dataSource } };
  });
  return {
    ...result,
    run,
    strategies,
    strategyVersions,
    dataSource: applyProviderProvenance(result.dataSource, run.provider, run.model),
  };
}

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
      source.providerProvenance !== "non-core" &&
      source.providerProvenance !== "unknown" &&
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

  if (source?.providerProvenance === "non-core" || source?.providerProvenance === "unknown") {
    return {
      state: "NOT_ACCEPTABLE",
      canAccept: false,
      sourceType,
      reason: source.sourceDetail,
      nextAction: "使用明确的真实 DeepSeek Provider 运行后，刷新并确认 provider/model、database_ids 与 artifact_refs。",
    };
  }

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

type LabEvidenceStageInput = {
  key: LocalStrategyLabEvidenceStage["key"];
  label: string;
  records: LocalStrategyLabEvidenceRecord[];
  isSuccess: (record: LocalStrategyLabEvidenceRecord) => boolean;
  emptyReason: string;
  emptyNextAction: string;
};

function normalizeLabEvidenceStatus(status: string): string {
  return status.trim().toLowerCase();
}

function labEvidenceFailed(status: string): boolean {
  return ["failed", "failure", "cancelled", "error"].includes(normalizeLabEvidenceStatus(status));
}

function labEvidenceBlocked(status: string): boolean {
  return ["blocked", "stale", "unavailable"].includes(normalizeLabEvidenceStatus(status));
}

function summarizeLabEvidenceStage(input: LabEvidenceStageInput): LocalStrategyLabEvidenceStage {
  const coreRecords = input.records.filter((record) => isCoreDataSourceTrace(record.source));
  if (coreRecords.find(input.isSuccess)) {
    return {
      key: input.key, label: input.label, state: "ACCEPTABLE", canAccept: true,
      reason: "已保留可复核的核心 database/api_aggregate 记录及 database_ids。",
      nextAction: "刷新后确认 ID、artifact path 和 data_source 仍保持一致。",
      observedCount: input.records.length, coreCount: coreRecords.length, records: input.records,
    };
  }
  const blocked = input.records.find((record) => record.source.blockedReason || labEvidenceBlocked(record.status));
  if (blocked) {
    const acceptance = getDataSourceAcceptance(blocked.source);
    return {
      key: input.key, label: input.label, state: "BLOCKED", canAccept: false,
      reason: blocked.source.blockedReason ?? acceptance.reason, nextAction: acceptance.nextAction,
      observedCount: input.records.length, coreCount: coreRecords.length, records: input.records,
    };
  }
  const failed = input.records.find((record) => labEvidenceFailed(record.status));
  if (failed) {
    const acceptance = getDataSourceAcceptance(failed.source);
    return {
      key: input.key, label: input.label, state: "FAILED", canAccept: false,
      reason: acceptance.reason, nextAction: acceptance.nextAction,
      observedCount: input.records.length, coreCount: coreRecords.length, records: input.records,
    };
  }
  const nonCore = input.records.find((record) => !isCoreDataSourceTrace(record.source));
  if (nonCore) {
    const acceptance = getDataSourceAcceptance(nonCore.source);
    const providerDetail = nonCore.provider ? `Provider ${nonCore.provider}/${nonCore.model ?? "unknown"}：` : "";
    return {
      key: input.key, label: input.label,
      state: acceptance.state === "API_GAP" ? "API_GAP" : "NOT_ACCEPTABLE", canAccept: false,
      reason: `${providerDetail}${acceptance.reason}`, nextAction: acceptance.nextAction,
      observedCount: input.records.length, coreCount: coreRecords.length, records: input.records,
    };
  }
  return {
    key: input.key, label: input.label, state: "NOT_RUN", canAccept: false,
    reason: input.emptyReason, nextAction: input.emptyNextAction,
    observedCount: 0, coreCount: 0, records: [],
  };
}

function labEvidenceRecord(
  id: string,
  status: string,
  source: DataSourceTraceSummary,
  options: Partial<Omit<LocalStrategyLabEvidenceRecord, "id" | "status" | "source">> = {},
): LocalStrategyLabEvidenceRecord {
  return {
    id, status, source, parentId: options.parentId ?? null, provider: options.provider ?? null,
    model: options.model ?? null, artifactPath: options.artifactPath ?? null,
  };
}

export function buildLocalStrategyLabEvidenceSummary(input: {
  generationRuns: StrategyGenerationRunDetail[];
  strategyVersions: StrategyGenerationVersion[];
  backtestResults: BacktestResultSummary[];
  ranking: RankingEntry[];
}): LocalStrategyLabEvidenceSummary {
  const stages = [
    summarizeLabEvidenceStage({
      key: "generation", label: "Provider / 策略生成",
      records: input.generationRuns.map((run) => labEvidenceRecord(run.id, run.status, run.dataSource, { provider: run.provider, model: run.model })),
      isSuccess: (record) => ["succeeded", "success"].includes(normalizeLabEvidenceStatus(record.status)),
      emptyReason: "尚未观察到真实 Provider 策略生成记录。",
      emptyNextAction: "完成本地 operator 授权和单次真实 Provider 调用后刷新；不要把 fixture 当作成功。",
    }),
    summarizeLabEvidenceStage({
      key: "strategy_file", label: "策略版本 / 文件",
      records: input.strategyVersions.map((version) => labEvidenceRecord(
        version.id, version.fileState?.status ?? version.validationStatus, version.dataSource,
        { parentId: version.strategyId, artifactPath: version.filePath },
      )),
      isSuccess: (record) => !labEvidenceFailed(record.status) && !labEvidenceBlocked(record.status),
      emptyReason: "尚未观察到有可复核文件路径的核心策略版本。",
      emptyNextAction: "先完成真实策略生成并确认 strategy_version_id、strategy_file_path 和 file state。",
    }),
    summarizeLabEvidenceStage({
      key: "backtest", label: "回测结果 / artifact",
      records: input.backtestResults.map((result) => labEvidenceRecord(
        result.id, "SUCCESS", result.dataSource, { parentId: result.taskId, artifactPath: result.resultPath },
      )),
      isSuccess: (record) => Boolean(record.artifactPath),
      emptyReason: "尚未观察到有 database ID 和 artifact path 的核心回测结果。",
      emptyNextAction: "在本地受控回测完成后刷新，并核对 backtest_run_id、backtest_task_id、backtest_result_id 和 artifact path。",
    }),
    summarizeLabEvidenceStage({
      key: "score", label: "评分 / 排行榜",
      records: input.ranking.map((entry) => labEvidenceRecord(
        entry.scoreId, "SUCCESS", entry.dataSource, { parentId: entry.backtestResultId, artifactPath: entry.filePath },
      )),
      isSuccess: (record) => Boolean(record.parentId),
      emptyReason: "尚未观察到关联核心回测结果的评分记录。",
      emptyNextAction: "回测结果入库并完成评分后刷新，确认 strategy_score_id 与 backtest_result_id。",
    }),
  ];
  const incomplete = stages.find((stage) => !stage.canAccept);
  return incomplete
    ? { state: incomplete.state, canAccept: false, reason: `${incomplete.label}：${incomplete.reason}`, nextAction: incomplete.nextAction, stages }
    : {
      state: "ACCEPTABLE", canAccept: true,
      reason: "Provider、策略文件、回测结果和评分均有核心 API/DB 证据。",
      nextAction: "刷新并复核各阶段的 database IDs、artifact paths 和 data_source；仍不代表可进行 live trading。",
      stages,
    };
}
