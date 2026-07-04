import { mockMvpData } from "../data/mock";
import type {
  BacktestArtifactManifest,
  BacktestMetricSummary,
  BacktestRunSummary,
  BacktestTaskSummary,
  DryRunArtifactManifest,
  DryRunBalanceSummary,
  DryRunEventSummary,
  DryRunManagementSummary,
  DryRunOpenTradesSummary,
  DryRunStatusSnapshot,
  FreqUILinkMetadata,
  HyperoptArtifactManifest,
  HyperoptComparisonSummary,
  HyperoptMetricComparison,
  HyperoptRunSummary,
  MvpData,
  RankingEliminationSummary,
  RankingEntry,
  RankingScoreBreakdownItem,
  RankingSignalSummary,
  StrategyFailureReasonSummary,
  StrategyVersionLineageEntry,
} from "./types";

const DEFAULT_API_BASE_URL = "/api";

// The frontend keeps a controlled fallback path while backend endpoints are
// still being stabilized. The flag returned by loadMvpData makes that fallback
// visible to pages instead of silently presenting mock data as live data.
type RawStrategyFailureReason = Partial<StrategyFailureReasonSummary> & {
  strategy_id?: string | number;
  strategy_version_id?: string | number;
  reason_type?: string;
  created_at?: string | null;
};

type RawStrategyVersionLineageEntry = Partial<StrategyVersionLineageEntry> & {
  strategy_id?: string | number;
  parent_version_id?: string | number | null;
  version_number?: number;
  change_summary?: string | null;
  diff_snapshot?: Record<string, unknown>;
  has_parent?: boolean;
  created_at?: string | null;
};

type RawBacktestArtifactManifest = Partial<BacktestArtifactManifest> & {
  manifest_version?: number | null;
  config_path?: string | null;
  strategy_name?: string | null;
  result_path?: string | null;
  manifest_path?: string | null;
  command_args?: unknown;
  return_code?: number | null;
  blocked_reason?: string | null;
  failed_reason?: string | null;
  strategy_path?: string | null;
};

type RawBacktestMetricSummary = Partial<BacktestMetricSummary> & {
  profit_total?: number | null;
  profit_pct?: number | null;
  max_drawdown_pct?: number | null;
  win_rate?: number | null;
  total_trades?: number | null;
  metricsSnapshot?: Record<string, unknown>;
  metrics_snapshot?: Record<string, unknown>;
  normalized_metrics?: Record<string, unknown>;
};

type RawBacktestRunSummary = Partial<BacktestRunSummary> & {
  strategy_name?: string;
  profile_name?: string | null;
  requested_task_count?: number;
  completed_task_count?: number;
  profit_pct?: number | null;
  max_drawdown_pct?: number | null;
  artifact_manifest?: RawBacktestArtifactManifest | null;
  manifest?: RawBacktestArtifactManifest | null;
  metrics_snapshot?: Record<string, unknown>;
  blocked_reason?: string | null;
  failed_reason?: string | null;
};

type RawBacktestTaskSummary = Partial<BacktestTaskSummary> & {
  run_id?: string | number;
  backtest_run_id?: string | number;
  strategy_name?: string;
  config_path?: string | null;
  result_path?: string | null;
  profit_pct?: number | null;
  error_message?: string | null;
  artifact_manifest?: RawBacktestArtifactManifest | null;
  manifest?: RawBacktestArtifactManifest | null;
  metrics_snapshot?: Record<string, unknown>;
  blocked_reason?: string | null;
  failed_reason?: string | null;
};

type RawHyperoptArtifactManifest = Partial<HyperoptArtifactManifest> & {
  manifest_version?: number | null;
  config_path?: string | null;
  strategy_name?: string | null;
  result_path?: string | null;
  manifest_path?: string | null;
  command_args?: unknown;
  return_code?: number | null;
  strategy_path?: string | null;
  hyperopt_loss?: string | null;
  blocked_reason?: string | null;
  failed_reason?: string | null;
};

type RawHyperoptMetricComparison = Partial<HyperoptMetricComparison> & {
  metric?: string;
  before_value?: number | null;
  after_value?: number | null;
};

type RawHyperoptComparisonSummary = Partial<HyperoptComparisonSummary> & {
  parent_version_id?: string | number | null;
  optimized_version_id?: string | number | null;
  blocked_reason?: string | null;
  failed_reason?: string | null;
};

type RawHyperoptRunSummary = Partial<HyperoptRunSummary> & {
  strategy_name?: string;
  profile_name?: string;
  best_params?: Record<string, unknown>;
  best_loss?: number | null;
  result_path?: string | null;
  manifest_path?: string | null;
  artifact_manifest?: RawHyperoptArtifactManifest | null;
  manifest?: RawHyperoptArtifactManifest | null;
  blocked_reason?: string | null;
  failed_reason?: string | null;
};

type RawDryRunArtifactManifest = Partial<DryRunArtifactManifest> & {
  manifest_version?: number | null;
  profile_name?: string | null;
  strategy_version_id?: number | null;
  strategy_name?: string | null;
  config_path?: string | null;
  manifest_path?: string | null;
  command_args?: unknown;
  return_code?: number | null;
  strategy_path?: string | null;
  blocked_reason?: string | null;
  failed_reason?: string | null;
  skipped_reason?: string | null;
};

type RawDryRunBalanceSummary = Partial<DryRunBalanceSummary> & {
  realized_profit?: number | null;
  unrealized_profit?: number | null;
};

type RawDryRunOpenTradesSummary = Partial<DryRunOpenTradesSummary> & {
  total_open_trades?: number;
  pair_count?: number;
  total_stake_amount?: number | null;
  total_profit_abs?: number | null;
  total_profit_pct?: number | null;
};

type RawDryRunEventSummary = Partial<DryRunEventSummary> & {
  event_type?: string;
};

type RawDryRunStatusSnapshot = Partial<DryRunStatusSnapshot> & {
  profile_name?: string | null;
  strategy_version_id?: number | null;
  strategy_name?: string | null;
  dry_run?: boolean | null;
  balance_summary?: RawDryRunBalanceSummary;
  open_trades_summary?: RawDryRunOpenTradesSummary;
  recent_events?: RawDryRunEventSummary[];
  blocked_reason?: string | null;
  failed_reason?: string | null;
  skipped_reason?: string | null;
  last_updated?: string | null;
  artifact_manifest_path?: string | null;
};

type RawFreqUILinkMetadata = Partial<FreqUILinkMetadata> & {
  base_url?: string | null;
  environment_label?: string;
  blocked_reason?: string | null;
  access_mode?: string;
};

type RawDryRunManagementSummary = Partial<DryRunManagementSummary> & {
  artifact_manifest?: RawDryRunArtifactManifest | null;
  manifest?: RawDryRunArtifactManifest | null;
  status_snapshot?: RawDryRunStatusSnapshot;
  snapshot?: RawDryRunStatusSnapshot;
  freq_ui_link?: RawFreqUILinkMetadata;
  frequi_link?: RawFreqUILinkMetadata;
};

type RawRankingEntry = Partial<RankingEntry> & {
  strategy_id?: string | number;
  strategy_name?: string;
  strategy_slug?: string;
  version_number?: number;
  file_path?: string;
  scoring_version?: string | null;
  total_score?: number;
  raw_total_score?: number | null;
  profit_score?: number | null;
  risk_score?: number | null;
  stability_score?: number | null;
  quality_score?: number | null;
  score_breakdown?: unknown;
  metricsSnapshot?: Record<string, unknown>;
  metrics_snapshot?: Record<string, unknown>;
};

function getApiBaseUrl() {
  const env = (import.meta as ImportMeta & { env?: Record<string, string | undefined> }).env;
  const configuredUrl = env?.VITE_API_BASE_URL?.trim();

  return (configuredUrl || DEFAULT_API_BASE_URL).replace(/\/$/, "");
}

function normalizeId(value: string | number | undefined): string {
  return value === undefined ? "" : String(value);
}

function normalizeOptionalId(value: string | number | null | undefined): string | null {
  return value === null || value === undefined ? null : String(value);
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function asNumber(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function asOptionalNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function asOptionalString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function asStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.map((item) => String(item)) : [];
}

function redactSensitiveText(value: string): string {
  return value
    .replace(
      /\b(api[_-]?key|api[_-]?secret|secret|password|passphrase|token)(\s*[:=]\s*)([^\s,;]+)/gi,
      "$1$2[REDACTED]",
    )
    .replace(/\bbearer\s+[A-Za-z0-9._~+/=-]+/gi, "Bearer [REDACTED]");
}

function normalizeMetrics(raw: RawBacktestMetricSummary | undefined): BacktestMetricSummary {
  const source = raw ?? {};
  const snapshot = source.metricsSnapshot ?? source.metrics_snapshot ?? {};
  const normalized = asRecord(snapshot.normalized_metrics ?? source.normalized_metrics);
  return {
    profitTotal: asOptionalNumber(source.profitTotal ?? source.profit_total ?? normalized.profit_total),
    profitPct: asOptionalNumber(source.profitPct ?? source.profit_pct ?? normalized.profit_pct),
    maxDrawdownPct: asOptionalNumber(
      source.maxDrawdownPct ?? source.max_drawdown_pct ?? normalized.max_drawdown_pct,
    ),
    winRate: asOptionalNumber(source.winRate ?? source.win_rate ?? normalized.win_rate),
    totalTrades: asOptionalNumber(source.totalTrades ?? source.total_trades ?? normalized.total_trades),
    timerange: asOptionalString(source.timerange ?? normalized.timerange),
    sharpe: asOptionalNumber(source.sharpe ?? normalized.sharpe),
    sortino: asOptionalNumber(source.sortino ?? normalized.sortino),
    calmar: asOptionalNumber(source.calmar ?? normalized.calmar),
  };
}

function normalizeArtifactManifest(
  raw: RawBacktestArtifactManifest | null | undefined,
): BacktestArtifactManifest | null {
  if (!raw) {
    return null;
  }

  return {
    manifestVersion: asOptionalNumber(raw.manifestVersion ?? raw.manifest_version),
    status: raw.status ?? "UNKNOWN",
    configPath: raw.configPath ?? raw.config_path ?? null,
    strategyName: raw.strategyName ?? raw.strategy_name ?? null,
    resultPath: raw.resultPath ?? raw.result_path ?? null,
    manifestPath: raw.manifestPath ?? raw.manifest_path ?? null,
    commandArgs: asStringArray(raw.commandArgs ?? raw.command_args),
    returnCode: asOptionalNumber(raw.returnCode ?? raw.return_code),
    stdout: raw.stdout ?? "",
    stderr: raw.stderr ?? "",
    datadir: raw.datadir ?? null,
    strategyPath: raw.strategyPath ?? raw.strategy_path ?? null,
    userdir: raw.userdir ?? null,
    blockedReason: raw.blockedReason ?? raw.blocked_reason ?? null,
    failedReason: raw.failedReason ?? raw.failed_reason ?? null,
  };
}

function normalizeHyperoptArtifactManifest(
  raw: RawHyperoptArtifactManifest | null | undefined,
): HyperoptArtifactManifest | null {
  if (!raw) {
    return null;
  }

  return {
    manifestVersion: asOptionalNumber(raw.manifestVersion ?? raw.manifest_version),
    status: raw.status ?? "UNKNOWN",
    configPath: raw.configPath ?? raw.config_path ?? null,
    strategyName: raw.strategyName ?? raw.strategy_name ?? null,
    resultPath: raw.resultPath ?? raw.result_path ?? null,
    manifestPath: raw.manifestPath ?? raw.manifest_path ?? null,
    commandArgs: asStringArray(raw.commandArgs ?? raw.command_args),
    returnCode: asOptionalNumber(raw.returnCode ?? raw.return_code),
    stdout: raw.stdout ?? "",
    stderr: raw.stderr ?? "",
    datadir: raw.datadir ?? null,
    strategyPath: raw.strategyPath ?? raw.strategy_path ?? null,
    userdir: raw.userdir ?? null,
    spaces: asStringArray(raw.spaces),
    epochs: asOptionalNumber(raw.epochs),
    hyperoptLoss: raw.hyperoptLoss ?? raw.hyperopt_loss ?? null,
    blockedReason: raw.blockedReason ?? raw.blocked_reason ?? null,
    failedReason: raw.failedReason ?? raw.failed_reason ?? null,
  };
}

function normalizeRankingSignal(raw: unknown): RankingSignalSummary {
  const value = asRecord(raw);
  return {
    code: typeof value.code === "string" ? value.code : null,
    severity: typeof value.severity === "string" ? value.severity : "warning",
    message:
      typeof value.message === "string" && value.message.trim()
        ? value.message
        : "Ranking signal was recorded without a message.",
  };
}

function normalizeScoreBreakdown(raw: unknown): RankingScoreBreakdownItem[] {
  return Array.isArray(raw)
    ? raw.map((item) => {
        const value = asRecord(item);
        return {
          name: typeof value.name === "string" ? value.name : "score",
          score: asNumber(value.score),
          weight: asNumber(value.weight),
          contribution: asNumber(value.contribution),
        };
      })
    : [];
}

function buildFallbackScoreBreakdown(raw: RawRankingEntry): RankingScoreBreakdownItem[] {
  return [
    { name: "profit_score", score: raw.profitScore ?? raw.profit_score ?? 0, weight: 0.35 },
    { name: "risk_score", score: raw.riskScore ?? raw.risk_score ?? 0, weight: 0.25 },
    { name: "stability_score", score: raw.stabilityScore ?? raw.stability_score ?? 0, weight: 0.15 },
    { name: "quality_score", score: raw.qualityScore ?? raw.quality_score ?? 0, weight: 0.25 },
  ].map((item) => ({
    ...item,
    contribution: Number((item.score * item.weight).toFixed(6)),
  }));
}

function normalizeElimination(raw: unknown): RankingEliminationSummary {
  const value = asRecord(raw);
  return {
    eliminated: value.eliminated === true,
    reasons: Array.isArray(value.reasons) ? value.reasons.map(normalizeRankingSignal) : [],
  };
}

function normalizeRankingEntry(raw: RawRankingEntry, index: number): RankingEntry {
  const metricsSnapshot = raw.metricsSnapshot ?? raw.metrics_snapshot ?? {};
  const breakdown =
    normalizeScoreBreakdown(raw.scoreBreakdown ?? raw.score_breakdown ?? metricsSnapshot.score_breakdown);
  const warningSignals = raw.warnings ?? metricsSnapshot.warnings;
  return {
    rank: raw.rank ?? index + 1,
    strategyId: normalizeId(raw.strategyId ?? raw.strategy_id ?? raw.strategy_slug),
    strategyName:
      raw.strategyName ??
      raw.strategy_name ??
      raw.strategy_slug ??
      "Unknown strategy",
    versionNumber: raw.versionNumber ?? raw.version_number ?? 0,
    filePath: raw.filePath ?? raw.file_path ?? "",
    scoringVersion: raw.scoringVersion ?? raw.scoring_version ?? null,
    totalScore: raw.totalScore ?? raw.total_score ?? 0,
    rawTotalScore:
      raw.rawTotalScore ??
      raw.raw_total_score ??
      asOptionalNumber(metricsSnapshot.raw_total_score),
    profitScore: raw.profitScore ?? raw.profit_score ?? null,
    riskScore: raw.riskScore ?? raw.risk_score ?? null,
    stabilityScore: raw.stabilityScore ?? raw.stability_score ?? null,
    qualityScore: raw.qualityScore ?? raw.quality_score ?? null,
    scoreBreakdown: breakdown.length > 0 ? breakdown : buildFallbackScoreBreakdown(raw),
    elimination: normalizeElimination(raw.elimination ?? metricsSnapshot.elimination),
    warnings: Array.isArray(warningSignals) ? warningSignals.map(normalizeRankingSignal) : [],
  };
}

function normalizeFailureReason(raw: RawStrategyFailureReason): StrategyFailureReasonSummary {
  // Backend responses may use snake_case while mock data uses camelCase.
  // Normalize at the API boundary so page components can stay simple.
  return {
    id: normalizeId(raw.id),
    strategyId: normalizeId(raw.strategyId ?? raw.strategy_id),
    strategyVersionId: normalizeId(raw.strategyVersionId ?? raw.strategy_version_id),
    stage: raw.stage ?? "unknown",
    reasonType: raw.reasonType ?? raw.reason_type ?? "unknown",
    severity: raw.severity ?? "error",
    message: raw.message ?? "Failure reason was recorded without a message.",
    details: raw.details ?? {},
    createdAt: raw.createdAt ?? raw.created_at ?? null,
  };
}

function normalizeLineageEntry(raw: RawStrategyVersionLineageEntry): StrategyVersionLineageEntry {
  // Treat absent parent metadata as "no parent" rather than a rendering error.
  const parentVersionId = raw.parentVersionId ?? raw.parent_version_id ?? null;
  return {
    id: normalizeId(raw.id),
    strategyId: normalizeId(raw.strategyId ?? raw.strategy_id),
    parentVersionId: parentVersionId === null ? null : normalizeId(parentVersionId),
    versionNumber: raw.versionNumber ?? raw.version_number ?? 0,
    changeSummary: raw.changeSummary ?? raw.change_summary ?? null,
    diffSnapshot: raw.diffSnapshot ?? raw.diff_snapshot ?? {},
    hasParent: raw.hasParent ?? raw.has_parent ?? parentVersionId !== null,
    createdAt: raw.createdAt ?? raw.created_at ?? null,
  };
}

function normalizeBacktestRun(raw: RawBacktestRunSummary): BacktestRunSummary {
  const artifactManifest = normalizeArtifactManifest(raw.artifactManifest ?? raw.artifact_manifest ?? raw.manifest);
  const metrics = normalizeMetrics(raw);
  return {
    id: normalizeId(raw.id),
    strategyName: raw.strategyName ?? raw.strategy_name ?? artifactManifest?.strategyName ?? "Unknown strategy",
    status: raw.status ?? artifactManifest?.status ?? "unknown",
    profileName: raw.profileName ?? raw.profile_name ?? "default",
    requestedTaskCount: raw.requestedTaskCount ?? raw.requested_task_count ?? 0,
    completedTaskCount: raw.completedTaskCount ?? raw.completed_task_count ?? 0,
    profitPct: raw.profitPct ?? raw.profit_pct ?? metrics.profitPct,
    maxDrawdownPct: raw.maxDrawdownPct ?? raw.max_drawdown_pct ?? metrics.maxDrawdownPct,
    artifactManifest,
    metrics,
    blockedReason: raw.blockedReason ?? raw.blocked_reason ?? artifactManifest?.blockedReason ?? null,
    failedReason: raw.failedReason ?? raw.failed_reason ?? artifactManifest?.failedReason ?? null,
  };
}

function normalizeBacktestTask(raw: RawBacktestTaskSummary): BacktestTaskSummary {
  const artifactManifest = normalizeArtifactManifest(raw.artifactManifest ?? raw.artifact_manifest ?? raw.manifest);
  const metrics = normalizeMetrics(raw);
  return {
    id: normalizeId(raw.id),
    runId: normalizeId(raw.runId ?? raw.run_id ?? raw.backtest_run_id),
    strategyName: raw.strategyName ?? raw.strategy_name ?? artifactManifest?.strategyName ?? "Unknown strategy",
    pair: raw.pair ?? "unknown",
    timeframe: raw.timeframe ?? "unknown",
    status: raw.status ?? artifactManifest?.status ?? "unknown",
    configPath: raw.configPath ?? raw.config_path ?? artifactManifest?.configPath ?? null,
    resultPath: raw.resultPath ?? raw.result_path ?? artifactManifest?.resultPath ?? null,
    profitPct: raw.profitPct ?? raw.profit_pct ?? metrics.profitPct,
    errorMessage: raw.errorMessage ?? raw.error_message ?? null,
    artifactManifest,
    metrics,
    blockedReason: raw.blockedReason ?? raw.blocked_reason ?? artifactManifest?.blockedReason ?? null,
    failedReason: raw.failedReason ?? raw.failed_reason ?? artifactManifest?.failedReason ?? null,
  };
}

function normalizeHyperoptMetricComparison(raw: RawHyperoptMetricComparison): HyperoptMetricComparison {
  const before = asOptionalNumber(raw.before ?? raw.before_value);
  const after = asOptionalNumber(raw.after ?? raw.after_value);
  return {
    label: raw.label ?? raw.metric ?? "metric",
    before,
    after,
    delta: asOptionalNumber(raw.delta) ?? (before === null || after === null ? null : after - before),
    suffix: raw.suffix ?? "",
  };
}

function normalizeHyperoptComparison(
  raw: RawHyperoptComparisonSummary | null | undefined,
): HyperoptComparisonSummary | null {
  if (!raw) {
    return null;
  }

  const parentVersionId = raw.parentVersionId ?? raw.parent_version_id ?? null;
  const optimizedVersionId = raw.optimizedVersionId ?? raw.optimized_version_id ?? null;

  return {
    parentVersionId: normalizeOptionalId(parentVersionId),
    optimizedVersionId: normalizeOptionalId(optimizedVersionId),
    status: raw.status ?? "UNKNOWN",
    metrics: Array.isArray(raw.metrics) ? raw.metrics.map(normalizeHyperoptMetricComparison) : [],
    warnings: Array.isArray(raw.warnings) ? raw.warnings.map(normalizeRankingSignal) : [],
    blockedReason: raw.blockedReason ?? raw.blocked_reason ?? null,
    failedReason: raw.failedReason ?? raw.failed_reason ?? null,
  };
}

function normalizeHyperoptRun(raw: RawHyperoptRunSummary): HyperoptRunSummary {
  const artifactManifest = normalizeHyperoptArtifactManifest(
    raw.artifactManifest ?? raw.artifact_manifest ?? raw.manifest,
  );
  return {
    id: normalizeId(raw.id),
    strategyName: raw.strategyName ?? raw.strategy_name ?? artifactManifest?.strategyName ?? "Unknown strategy",
    status: raw.status ?? artifactManifest?.status ?? "unknown",
    profileName: raw.profileName ?? raw.profile_name ?? "default",
    spaces: asStringArray(raw.spaces ?? artifactManifest?.spaces),
    bestParams: raw.bestParams ?? raw.best_params ?? {},
    bestLoss: asOptionalNumber(raw.bestLoss ?? raw.best_loss),
    score: asOptionalNumber(raw.score),
    epoch: asOptionalNumber(raw.epoch),
    artifactManifest,
    resultPath: raw.resultPath ?? raw.result_path ?? artifactManifest?.resultPath ?? null,
    manifestPath: raw.manifestPath ?? raw.manifest_path ?? artifactManifest?.manifestPath ?? null,
    blockedReason: raw.blockedReason ?? raw.blocked_reason ?? artifactManifest?.blockedReason ?? null,
    failedReason: raw.failedReason ?? raw.failed_reason ?? artifactManifest?.failedReason ?? null,
    comparison: normalizeHyperoptComparison(raw.comparison),
  };
}

function normalizeDryRunManifest(raw: RawDryRunArtifactManifest | null | undefined): DryRunArtifactManifest | null {
  if (!raw) {
    return null;
  }

  return {
    manifestVersion: asOptionalNumber(raw.manifestVersion ?? raw.manifest_version),
    status: raw.status ?? "UNKNOWN",
    profileName: raw.profileName ?? raw.profile_name ?? null,
    strategyVersionId: asOptionalNumber(raw.strategyVersionId ?? raw.strategy_version_id),
    strategyName: raw.strategyName ?? raw.strategy_name ?? null,
    pair: raw.pair ?? null,
    timeframe: raw.timeframe ?? null,
    configPath: raw.configPath ?? raw.config_path ?? null,
    manifestPath: raw.manifestPath ?? raw.manifest_path ?? null,
    commandArgs: asStringArray(raw.commandArgs ?? raw.command_args).map(redactSensitiveText),
    returnCode: asOptionalNumber(raw.returnCode ?? raw.return_code),
    stdout: redactSensitiveText(raw.stdout ?? ""),
    stderr: redactSensitiveText(raw.stderr ?? ""),
    userdir: raw.userdir ?? null,
    strategyPath: raw.strategyPath ?? raw.strategy_path ?? null,
    blockedReason: raw.blockedReason ?? raw.blocked_reason ?? null,
    failedReason: raw.failedReason ?? raw.failed_reason ?? null,
    skippedReason: raw.skippedReason ?? raw.skipped_reason ?? null,
  };
}

function normalizeDryRunBalance(raw: RawDryRunBalanceSummary | undefined): DryRunBalanceSummary {
  const source = raw ?? {};
  return {
    currency: source.currency ?? null,
    total: asOptionalNumber(source.total),
    free: asOptionalNumber(source.free),
    used: asOptionalNumber(source.used),
    realizedProfit: asOptionalNumber(source.realizedProfit ?? source.realized_profit),
    unrealizedProfit: asOptionalNumber(source.unrealizedProfit ?? source.unrealized_profit),
  };
}

function normalizeDryRunOpenTrades(raw: RawDryRunOpenTradesSummary | undefined): DryRunOpenTradesSummary {
  const source = raw ?? {};
  return {
    totalOpenTrades: source.totalOpenTrades ?? source.total_open_trades ?? 0,
    pairCount: source.pairCount ?? source.pair_count ?? 0,
    pairs: asStringArray(source.pairs),
    totalStakeAmount: asOptionalNumber(source.totalStakeAmount ?? source.total_stake_amount),
    totalProfitAbs: asOptionalNumber(source.totalProfitAbs ?? source.total_profit_abs),
    totalProfitPct: asOptionalNumber(source.totalProfitPct ?? source.total_profit_pct),
  };
}

function normalizeDryRunEvent(raw: RawDryRunEventSummary): DryRunEventSummary {
  return {
    timestamp: raw.timestamp ?? "",
    eventType: raw.eventType ?? raw.event_type ?? "status_event",
    severity: raw.severity ?? "INFO",
    message: redactSensitiveText(raw.message ?? "Status event recorded."),
    source: raw.source ?? "unknown",
  };
}

function normalizeDryRunSnapshot(raw: RawDryRunStatusSnapshot | undefined): DryRunStatusSnapshot {
  const source = raw ?? {};
  return {
    status: source.status ?? "BLOCKED",
    profileName: source.profileName ?? source.profile_name ?? null,
    strategyVersionId: asOptionalNumber(source.strategyVersionId ?? source.strategy_version_id),
    strategyName: source.strategyName ?? source.strategy_name ?? null,
    exchange: source.exchange ?? null,
    pair: source.pair ?? null,
    timeframe: source.timeframe ?? null,
    dryRun: source.dryRun ?? source.dry_run ?? null,
    balanceSummary: normalizeDryRunBalance(source.balanceSummary ?? source.balance_summary),
    openTradesSummary: normalizeDryRunOpenTrades(source.openTradesSummary ?? source.open_trades_summary),
    recentEvents: Array.isArray(source.recentEvents ?? source.recent_events)
      ? (source.recentEvents ?? source.recent_events ?? []).map(normalizeDryRunEvent)
      : [],
    blockedReason: source.blockedReason ?? source.blocked_reason ?? null,
    failedReason: source.failedReason ?? source.failed_reason ?? null,
    skippedReason: source.skippedReason ?? source.skipped_reason ?? null,
    lastUpdated: source.lastUpdated ?? source.last_updated ?? null,
    artifactManifestPath: source.artifactManifestPath ?? source.artifact_manifest_path ?? null,
  };
}

function normalizeFreqUILink(raw: RawFreqUILinkMetadata | undefined): FreqUILinkMetadata {
  const source = raw ?? {};
  return {
    enabled: source.enabled === true,
    baseUrl: source.baseUrl ?? source.base_url ?? null,
    environmentLabel: source.environmentLabel ?? source.environment_label ?? "local-dry-run",
    blockedReason: source.blockedReason ?? source.blocked_reason ?? "FreqUI link is not configured",
    accessMode: source.accessMode ?? source.access_mode ?? "read-only-link",
  };
}

function normalizeDryRunManagement(raw: RawDryRunManagementSummary): DryRunManagementSummary {
  return {
    manifest: normalizeDryRunManifest(raw.manifest ?? raw.artifact_manifest),
    snapshot: normalizeDryRunSnapshot(raw.snapshot ?? raw.status_snapshot),
    freqUiLink: normalizeFreqUILink(raw.freqUiLink ?? raw.freq_ui_link ?? raw.frequi_link),
  };
}

async function fetchJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${getApiBaseUrl()}${path}`, {
    headers: { Accept: "application/json" },
    signal,
  });

  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }

  return response.json() as Promise<T>;
}

async function fetchList<T>(
  paths: string[],
  fallback: T[],
  signal?: AbortSignal,
): Promise<{ items: T[]; usedFallback: boolean }> {
  // Try known endpoint candidates in order. This keeps the UI useful during
  // backend iteration while still surfacing usedFallback to the caller.
  for (const path of paths) {
    try {
      return { items: await fetchJson<T[]>(path, signal), usedFallback: false };
    } catch (error) {
      if (signal?.aborted) {
        throw error;
      }
    }
  }

  return { items: fallback, usedFallback: true };
}

async function fetchValue<T>(
  paths: string[],
  fallback: T,
  signal?: AbortSignal,
): Promise<{ item: T; usedFallback: boolean }> {
  for (const path of paths) {
    try {
      return { item: await fetchJson<T>(path, signal), usedFallback: false };
    } catch (error) {
      if (signal?.aborted) {
        throw error;
      }
    }
  }

  return { item: fallback, usedFallback: true };
}

export async function loadMvpData(signal?: AbortSignal): Promise<{
  data: MvpData;
  usedFallback: boolean;
}> {
  const [
    strategies,
    generationRuns,
    backtestRuns,
    backtestTasks,
    hyperoptRuns,
    dryRun,
    ranking,
    failureReasons,
    versionLineage,
  ] = await Promise.all([
    fetchList(["/strategies", "/mvp/strategies"], mockMvpData.strategies, signal),
    fetchList(
      ["/generation-runs", "/strategy-generation-runs", "/mvp/generation-runs"],
      mockMvpData.generationRuns,
      signal,
    ),
    fetchList<RawBacktestRunSummary>(
      ["/backtest-runs", "/mvp/backtest-runs"],
      mockMvpData.backtestRuns,
      signal,
    ),
    fetchList<RawBacktestTaskSummary>(
      ["/backtest-tasks", "/mvp/backtest-tasks"],
      mockMvpData.backtestTasks,
      signal,
    ),
    fetchList<RawHyperoptRunSummary>(
      ["/hyperopt-runs", "/mvp/hyperopt-runs"],
      mockMvpData.hyperoptRuns,
      signal,
    ),
    fetchValue<RawDryRunManagementSummary>(
      ["/dry-run/management", "/dry-run/status", "/mvp/dry-run"],
      mockMvpData.dryRun,
      signal,
    ),
    fetchList<RawRankingEntry>(
      ["/ranking", "/strategy-ranking", "/mvp/ranking"],
      mockMvpData.ranking,
      signal,
    ),
    fetchList<RawStrategyFailureReason>(
      ["/strategy-failure-reasons", "/mvp/strategy-failure-reasons"],
      mockMvpData.failureReasons,
      signal,
    ),
    fetchList<RawStrategyVersionLineageEntry>(
      ["/strategy-version-lineage", "/strategy-versions/lineage", "/mvp/strategy-version-lineage"],
      mockMvpData.versionLineage,
      signal,
    ),
  ]);

  return {
    data: {
      strategies: strategies.items,
      generationRuns: generationRuns.items,
      backtestRuns: backtestRuns.items.map(normalizeBacktestRun),
      backtestTasks: backtestTasks.items.map(normalizeBacktestTask),
      hyperoptRuns: hyperoptRuns.items.map(normalizeHyperoptRun),
      dryRun: normalizeDryRunManagement(dryRun.item),
      ranking: ranking.items.map(normalizeRankingEntry),
      failureReasons: failureReasons.items.map(normalizeFailureReason),
      versionLineage: versionLineage.items.map(normalizeLineageEntry),
    },
    usedFallback:
      strategies.usedFallback ||
      generationRuns.usedFallback ||
      backtestRuns.usedFallback ||
      backtestTasks.usedFallback ||
      hyperoptRuns.usedFallback ||
      dryRun.usedFallback ||
      ranking.usedFallback ||
      failureReasons.usedFallback ||
      versionLineage.usedFallback,
  };
}
