import { mockMvpData } from "../data/mock";
import type { MvpData, MvpDataSources } from "./types";
import { fetchList, fetchValue } from "./http";
import * as N from "./normalizers";

export async function loadMvpData(signal?: AbortSignal): Promise<{
  data: MvpData;
  sources: MvpDataSources;
  usedFallback: boolean;
}> {
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
    fetchList<N.RawStrategySummary>(["/strategies", "/mvp/strategies"], mockMvpData.strategies, signal),
    fetchList<N.RawStrategyGenerationVersion>(
      ["/strategy-versions"],
      mockMvpData.strategyVersions,
      signal,
    ),
    fetchList<N.RawStrategyGenerationRunDetail>(
      ["/strategy-generation-runs", "/mvp/generation-runs"],
      mockMvpData.generationRuns,
      signal,
    ),
    fetchList<N.RawBacktestRunSummary>(
      ["/backtest-runs", "/mvp/backtest-runs"],
      mockMvpData.backtestRuns,
      signal,
    ),
    fetchList<N.RawBacktestTaskSummary>(
      ["/backtest-tasks", "/mvp/backtest-tasks"],
      mockMvpData.backtestTasks,
      signal,
    ),
    fetchList<N.RawBacktestResultSummary>(
      ["/backtest-results"],
      mockMvpData.backtestResults,
      signal,
    ),
    fetchList<N.RawHyperoptRunSummary>(
      ["/hyperopt-runs", "/mvp/hyperopt-runs"],
      mockMvpData.hyperoptRuns,
      signal,
    ),
    fetchValue<N.RawDryRunManagementSummary>(
      ["/dry-run/management", "/dry-run/status", "/mvp/dry-run"],
      mockMvpData.dryRun,
      signal,
    ),
    fetchValue<N.RawLiveCandidateGovernanceSummary>(
      ["/live-candidates/governance", "/live-candidates", "/mvp/live-candidates"],
      mockMvpData.liveCandidates,
      signal,
    ),
    fetchValue<N.RawRuntimeReadOnlyContractSummary>(
      ["/runtime/read-only", "/mvp/runtime/read-only"],
      mockMvpData.operatorDashboard.runtimeContract,
      signal,
    ),
    fetchValue<N.RawOperatorStatusReportSummary>(
      ["/runtime/operator-status", "/mvp/runtime/operator-status"],
      mockMvpData.operatorDashboard.operatorStatus,
      signal,
    ),
    fetchList<N.RawOperatorAuditEventSummary>(
      ["/governance-events", "/audit-log/governance-events", "/mvp/governance-events"],
      mockMvpData.operatorDashboard.auditEvents,
      signal,
    ),
    fetchList<N.RawRankingEntry>(
      ["/ranking", "/strategy-ranking", "/mvp/ranking"],
      mockMvpData.ranking,
      signal,
    ),
    fetchList<N.RawStrategyFailureReason>(
      ["/strategy-failure-reasons", "/mvp/strategy-failure-reasons"],
      mockMvpData.failureReasons,
      signal,
    ),
    fetchList<N.RawStrategyVersionLineageEntry>(
      ["/strategy-version-lineage", "/strategy-versions/lineage", "/mvp/strategy-version-lineage"],
      mockMvpData.versionLineage,
      signal,
    ),
  ]);

  const normalizedStrategyVersions = strategyVersions.items.map(N.normalizeStrategyGenerationVersion);
  const sources: MvpDataSources = {
    strategies: strategies.usedFallback ? "fallback" : "api",
    strategyVersions: strategyVersions.usedFallback ? "fallback" : "api",
    generationRuns: generationRuns.usedFallback ? "fallback" : "api",
    backtestRuns: backtestRuns.usedFallback ? "fallback" : "api",
    backtestTasks: backtestTasks.usedFallback ? "fallback" : "api",
    backtestResults: backtestResults.usedFallback ? "fallback" : "api",
    hyperoptRuns: hyperoptRuns.usedFallback ? "fallback" : "api",
    dryRun: dryRun.usedFallback ? "fallback" : "api",
    liveCandidates: liveCandidates.usedFallback ? "fallback" : "api",
    operatorDashboard:
      runtimeContract.usedFallback || operatorStatus.usedFallback || auditEvents.usedFallback
        ? "fallback"
        : "api",
    ranking: ranking.usedFallback ? "fallback" : "api",
    failureReasons: failureReasons.usedFallback ? "fallback" : "api",
    versionLineage: versionLineage.usedFallback ? "fallback" : "api",
  };

  return {
    data: {
      strategies: strategies.items.map((strategy) => N.normalizeStrategySummary(strategy, normalizedStrategyVersions)),
      strategyVersions: normalizedStrategyVersions,
      generationRuns: generationRuns.items.map(N.normalizeStrategyGenerationRun),
      backtestRuns: backtestRuns.items.map(N.normalizeBacktestRun),
      backtestTasks: backtestTasks.items.map(N.normalizeBacktestTask),
      backtestResults: backtestResults.items.map(N.normalizeBacktestResult),
      hyperoptRuns: hyperoptRuns.items.map(N.normalizeHyperoptRun),
      dryRun: N.normalizeDryRunManagement(dryRun.item),
      liveCandidates: N.normalizeLiveCandidateGovernance(liveCandidates.item),
      operatorDashboard: N.normalizeOperatorDashboard({
        ...mockMvpData.operatorDashboard,
        runtimeContract: runtimeContract.item,
        operatorStatus: operatorStatus.item,
        auditEvents: auditEvents.items,
      }),
      ranking: ranking.items.map(N.normalizeRankingEntry),
      failureReasons: failureReasons.items.map(N.normalizeFailureReason),
      versionLineage: versionLineage.items.map(N.normalizeLineageEntry),
    },
    sources,
    usedFallback: Object.values(sources).some((source) => source === "fallback"),
  };
}
