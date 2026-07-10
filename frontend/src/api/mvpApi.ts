import { mockMvpData } from "../data/mock";
import type { MvpData, MvpDataSources } from "./types";
import { fetchList, fetchValue } from "./http";
import * as N from "./normalizers";
import { isCoreDataSourceTrace, MVP_DATA_SET_KEYS } from "./sourceState";

function fixtureModeEnabled(): boolean {
  const env = (import.meta as ImportMeta & { env?: Record<string, string | undefined> }).env;
  return env?.VITE_ENABLE_DEV_FIXTURES === "true";
}

export function emptyMvpData(): MvpData {
  return {
    strategies: [],
    strategyVersions: [],
    generationRuns: [],
    backtestRuns: [],
    backtestTasks: [],
    backtestResults: [],
    hyperoptRuns: [],
    dryRun: N.normalizeDryRunManagement({}),
    liveCandidates: N.normalizeLiveCandidateGovernance({}),
    operatorDashboard: N.normalizeOperatorDashboard({}),
    ranking: [],
    failureReasons: [],
    versionLineage: [],
  };
}

export async function loadMvpData(signal?: AbortSignal): Promise<{
  data: MvpData;
  sources: MvpDataSources;
  usedFallback: boolean;
}> {
  if (fixtureModeEnabled()) {
    const sources = Object.fromEntries(MVP_DATA_SET_KEYS.map((key) => [key, "fixture"])) as MvpDataSources;
    return { data: mockMvpData, sources, usedFallback: true };
  }
  const [
    strategies,
    strategyVersions,
    generationRuns,
    backtestRuns,
    backtestTasks,
    backtestResults,
    hyperoptRuns,
    dryRun,
    liveCandidates,
    runtimeContract,
    operatorStatus,
    auditEvents,
    ranking,
    failureReasons,
    versionLineage,
  ] = await Promise.all([
    fetchList<N.RawStrategySummary>(["/strategies", "/mvp/strategies"], signal),
    fetchList<N.RawStrategyGenerationVersion>(
      ["/strategy-versions"],
      signal,
    ),
    fetchList<N.RawStrategyGenerationRunDetail>(
      ["/strategy-generation-runs", "/mvp/generation-runs"],
      signal,
    ),
    fetchList<N.RawBacktestRunSummary>(
      ["/backtest-runs", "/mvp/backtest-runs"],
      signal,
    ),
    fetchList<N.RawBacktestTaskSummary>(
      ["/backtest-tasks", "/mvp/backtest-tasks"],
      signal,
    ),
    fetchList<N.RawBacktestResultSummary>(
      ["/backtest-results"],
      signal,
    ),
    fetchList<N.RawHyperoptRunSummary>(
      ["/hyperopt-runs", "/mvp/hyperopt-runs"],
      signal,
    ),
    fetchValue<N.RawDryRunManagementSummary>(
      ["/dry-run/management", "/dry-run/status", "/mvp/dry-run"],
      signal,
    ),
    fetchValue<N.RawLiveCandidateGovernanceSummary>(
      ["/live-candidates/governance", "/live-candidates", "/mvp/live-candidates"],
      signal,
    ),
    fetchValue<N.RawRuntimeReadOnlyContractSummary>(
      ["/runtime/read-only", "/mvp/runtime/read-only"],
      signal,
    ),
    fetchValue<N.RawOperatorStatusReportSummary>(
      ["/runtime/operator-status", "/mvp/runtime/operator-status"],
      signal,
    ),
    fetchList<N.RawOperatorAuditEventSummary>(
      ["/governance-events", "/audit-log/governance-events", "/mvp/governance-events"],
      signal,
    ),
    fetchList<N.RawRankingEntry>(
      ["/ranking", "/strategy-ranking", "/mvp/ranking"],
      signal,
    ),
    fetchList<N.RawStrategyFailureReason>(
      ["/strategy-failure-reasons", "/mvp/strategy-failure-reasons"],
      signal,
    ),
    fetchList<N.RawStrategyVersionLineageEntry>(
      ["/strategy-version-lineage", "/strategy-versions/lineage", "/mvp/strategy-version-lineage"],
      signal,
    ),
  ]);

  const normalizedStrategyVersions = strategyVersions.items
    .map(N.normalizeStrategyGenerationVersion)
    .filter((item) => isCoreDataSourceTrace(item.dataSource));
  const sources = Object.fromEntries(MVP_DATA_SET_KEYS.map((key) => [key, "api"])) as MvpDataSources;

  return {
    data: {
      strategies: strategies.items
        .map((strategy) => N.normalizeStrategySummary(strategy, normalizedStrategyVersions))
        .filter((item) => isCoreDataSourceTrace(item.dataSource)),
      strategyVersions: normalizedStrategyVersions,
      generationRuns: generationRuns.items.map(N.normalizeStrategyGenerationRun).filter((item) => isCoreDataSourceTrace(item.dataSource)),
      backtestRuns: backtestRuns.items.map(N.normalizeBacktestRun).filter((item) => isCoreDataSourceTrace(item.dataSource)),
      backtestTasks: backtestTasks.items.map(N.normalizeBacktestTask).filter((item) => isCoreDataSourceTrace(item.dataSource)),
      backtestResults: backtestResults.items.map(N.normalizeBacktestResult).filter((item) => isCoreDataSourceTrace(item.dataSource)),
      hyperoptRuns: hyperoptRuns.items.map(N.normalizeHyperoptRun),
      dryRun: N.normalizeDryRunManagement(dryRun.item),
      liveCandidates: N.normalizeLiveCandidateGovernance(liveCandidates.item),
      operatorDashboard: N.normalizeOperatorDashboard({
        runtimeContract: runtimeContract.item,
        operatorStatus: operatorStatus.item,
        auditEvents: auditEvents.items,
      }),
      ranking: ranking.items.map(N.normalizeRankingEntry).filter((item) => isCoreDataSourceTrace(item.dataSource)),
      failureReasons: failureReasons.items.map(N.normalizeFailureReason),
      versionLineage: versionLineage.items.map(N.normalizeLineageEntry),
    },
    sources,
    usedFallback: false,
  };
}
