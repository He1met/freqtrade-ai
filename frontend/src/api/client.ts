import { mockMvpData } from "../data/mock";
import type {
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

export async function loadMvpData(signal?: AbortSignal): Promise<{
  data: MvpData;
  usedFallback: boolean;
}> {
  const [
    strategies,
    generationRuns,
    backtestRuns,
    backtestTasks,
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
    fetchList(["/backtest-runs", "/mvp/backtest-runs"], mockMvpData.backtestRuns, signal),
    fetchList(["/backtest-tasks", "/mvp/backtest-tasks"], mockMvpData.backtestTasks, signal),
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
      backtestRuns: backtestRuns.items,
      backtestTasks: backtestTasks.items,
      ranking: ranking.items.map(normalizeRankingEntry),
      failureReasons: failureReasons.items.map(normalizeFailureReason),
      versionLineage: versionLineage.items.map(normalizeLineageEntry),
    },
    usedFallback:
      strategies.usedFallback ||
      generationRuns.usedFallback ||
      backtestRuns.usedFallback ||
      backtestTasks.usedFallback ||
      ranking.usedFallback ||
      failureReasons.usedFallback ||
      versionLineage.usedFallback,
  };
}
