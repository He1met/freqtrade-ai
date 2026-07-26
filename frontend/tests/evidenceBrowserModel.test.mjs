import assert from "node:assert/strict";
import test from "node:test";

import { mockMvpData } from "../src/data/mock.ts";
import {
  buildEvidenceRecords,
  evidenceBrowserEmptyState,
  isEvidenceScope,
  isEvidenceTab,
  partitionBrowserRecords,
} from "../src/pages/localStrategyLab/evidenceBrowserModel.ts";

function source(scope = "current", overrides = {}) {
  return {
    sourceType: "database",
    sourceDetail: "Persisted record with a deliberately long technical path that belongs in details.",
    coreData: true,
    databaseIds: { strategy_version_id: 12 },
    artifactRefs: { strategy_file_path: "/canonical/user_data/strategies/VeryLongStrategyName.py" },
    freshness: null,
    blockedReason: null,
    providerProvenance: "real",
    environment: {
      scope,
      runnable: scope === "current",
      migrationVerified: scope === "current",
      reason: scope === "current" ? "canonical" : "historical read-only evidence",
    },
    ...overrides,
  };
}

test("four browser tabs and both scopes accept only exact URL values", () => {
  for (const tab of ["generation", "versions", "backtests", "scores"]) assert.equal(isEvidenceTab(tab), true);
  assert.equal(isEvidenceTab("dry-run"), false);
  assert.equal(isEvidenceTab("../scores"), false);
  assert.equal(isEvidenceScope("current"), true);
  assert.equal(isEvidenceScope("diagnostic"), true);
  assert.equal(isEvidenceScope("core"), false);
});

test("current core and historical diagnostics stay strictly partitioned", () => {
  const current = {
    ...mockMvpData.strategyVersions[0],
    id: "12",
    filePath: "/canonical/user_data/strategies/Current.py",
    validationStatus: "valid",
    fileState: {
      status: "READY", path: "/canonical/user_data/strategies/Current.py", exists: true, isFile: true,
      checksum: "abc", checksumMatches: true, className: "Current", blockedReason: null, validationErrors: [],
    },
    dataSource: source(),
  };
  const historical = {
    ...current,
    id: "13",
    filePath: "/retired/user_data/strategies/Old.py",
    dataSource: source("historical", {
      databaseIds: { strategy_version_id: 13 },
      artifactRefs: { strategy_file_path: "/retired/user_data/strategies/Old.py" },
    }),
  };
  const records = buildEvidenceRecords(
    { ...mockMvpData, strategyVersions: [current, historical] },
    "versions",
  );
  const partition = partitionBrowserRecords(records);
  assert.deepEqual(partition.current.map((item) => item.id), ["12"]);
  assert.deepEqual(partition.diagnostic.map((item) => item.id), ["13"]);
  assert.equal(partition.current[0].artifactRefs.strategy_file_path, current.filePath);
});

test("backtest list exposes decision metrics while paths remain detail-only", () => {
  const result = {
    id: "result-430",
    runId: "run-430",
    taskId: "task-430",
    createdAt: null,
    metrics: {
      profitTotal: 125,
      profitPct: 12.5,
      maxDrawdownPct: 4.25,
      winRate: 0.625,
      totalTrades: 48,
      timerange: "20260101-20260201",
      sharpe: 1.2,
      sortino: 1.4,
      calmar: 2.1,
    },
    resultPath: "/a/very/long/private/technical/path/result.json",
    dataSource: source("current", {
      databaseIds: { backtest_result_id: 430 },
      artifactRefs: { result_path: "/a/very/long/private/technical/path/result.json" },
    }),
  };
  const record = buildEvidenceRecords(
    { ...mockMvpData, backtestResults: [result] },
    "backtests",
  )[0];
  assert.deepEqual(record.decisionFields.map((field) => field.value), ["12.50%", "4.25%", "62.50%", "48"]);
  assert.equal(record.subtitle.includes(result.resultPath), false);
  assert.equal(record.artifactRefs.result_path, result.resultPath);
});

test("real empty, filtered, API_GAP and BLOCKED states remain distinct", () => {
  const filteredRecord = buildEvidenceRecords(
    {
      ...mockMvpData,
      generationRuns: [{
        ...mockMvpData.generationRuns[0],
        id: "historical",
        dataSource: source("historical", { databaseIds: { strategy_generation_run_id: 1 } }),
      }],
    },
    "generation",
  );
  assert.equal(evidenceBrowserEmptyState(undefined, "generation", "current", filteredRecord).state, "FILTERED");
  assert.equal(evidenceBrowserEmptyState(undefined, "generation", "current", []).state, "NOT_RUN");

  const summary = {
    state: "API_GAP", canAccept: false, reason: "gap", nextAction: "fix",
    stages: [{
      key: "score", label: "评分", state: "API_GAP", canAccept: false, reason: "missing IDs",
      nextAction: "fix API", observedCount: 0, coreCount: 0, records: [],
    }],
  };
  assert.equal(evidenceBrowserEmptyState(summary, "scores", "current", []).state, "API_GAP");
  summary.stages[0].state = "BLOCKED";
  assert.equal(evidenceBrowserEmptyState(summary, "scores", "current", []).state, "BLOCKED");
});

test("every tab requires exact database identity and lineage before entering current scope", () => {
  const generation = {
    ...mockMvpData.generationRuns[0],
    id: "run-10",
    provider: "deepseek",
    model: "deepseek-chat",
    dataSource: source("current", { databaseIds: { strategy_generation_run_id: 999 }, artifactRefs: {} }),
  };
  const version = {
    ...mockMvpData.strategyVersions[0],
    id: "version-20",
    dataSource: source("current", { databaseIds: { strategy_version_id: 999 } }),
  };
  const backtestRun = {
    id: "backtest-run-30", strategyVersionId: "version-20", strategyName: "S", status: "SUCCESS",
    profileName: "p", requestedTaskCount: 1, completedTaskCount: 1, profitPct: 1, maxDrawdownPct: 1,
    artifactManifest: null, metrics: resultMetrics(), blockedReason: null, failedReason: null,
    dataSource: source("current", { databaseIds: { backtest_run_id: "backtest-run-30", strategy_version_id: "version-20" } }),
  };
  const task = {
    id: "task-40", runId: "backtest-run-30", strategyName: "S", pair: "BTC/USDT", timeframe: "5m",
    status: "SUCCESS", configPath: null, resultPath: "result.json", profitPct: 1, errorMessage: null,
    artifactManifest: null, metrics: resultMetrics(), blockedReason: null, failedReason: null,
    dataSource: source("current", { databaseIds: { backtest_task_id: "task-40", backtest_run_id: "backtest-run-30" } }),
  };
  const result = {
    id: "result-50", runId: "backtest-run-30", taskId: "task-40", resultPath: "result.json",
    metrics: resultMetrics(), createdAt: null,
    dataSource: source("current", {
      databaseIds: {
        backtest_result_id: "result-50",
        backtest_run_id: "backtest-run-30",
        backtest_task_id: "task-40",
      },
    }),
  };
  const score = {
    ...mockMvpData.ranking[0],
    scoreId: "score-60", strategyVersionId: "version-20", backtestResultId: "result-50",
    dataSource: source("current", {
      sourceType: "api_aggregate",
      databaseIds: {
        strategy_score_id: "score-60",
        strategy_version_id: "version-20",
        backtest_result_id: "result-50",
      },
    }),
  };
  const data = {
    ...mockMvpData,
    localStrategyLabEvidenceData: {
      generationRuns: [generation], strategyVersions: [version], backtestRuns: [backtestRun],
      backtestTasks: [task], backtestResults: [result], ranking: [score],
    },
  };
  for (const tab of ["generation", "versions", "backtests", "scores"]) {
    const partition = partitionBrowserRecords(buildEvidenceRecords(data, tab));
    assert.equal(partition.current.length, 0, `${tab} must reject wrong or foreign identity`);
    assert.equal(partition.diagnostic.length, 1);
  }

  const validData = {
    ...data,
    localStrategyLabEvidenceData: {
      ...data.localStrategyLabEvidenceData,
      strategyVersions: [{
        ...version,
        dataSource: source("current", { databaseIds: { strategy_version_id: "version-20" } }),
      }],
    },
  };
  assert.equal(partitionBrowserRecords(buildEvidenceRecords(validData, "backtests")).current.length, 1);
  assert.equal(partitionBrowserRecords(buildEvidenceRecords(validData, "scores")).current.length, 1);

  const historicalVersion = {
    ...version,
    dataSource: source("historical", {
      databaseIds: { strategy_version_id: "version-20" },
      artifactRefs: { strategy_file_path: "/retired/Version.py" },
    }),
  };
  const historicalData = {
    ...data,
    localStrategyLabEvidenceData: {
      ...data.localStrategyLabEvidenceData,
      strategyVersions: [historicalVersion],
    },
  };
  assert.equal(partitionBrowserRecords(buildEvidenceRecords(historicalData, "backtests")).current.length, 0);
  assert.equal(partitionBrowserRecords(buildEvidenceRecords(historicalData, "scores")).current.length, 0);

  const crossBoundScore = {
    ...score,
    strategyVersionId: "foreign-version",
    dataSource: source("current", {
      sourceType: "api_aggregate",
      databaseIds: {
        strategy_score_id: "score-60",
        strategy_version_id: "foreign-version",
        backtest_result_id: "result-50",
      },
    }),
  };
  const crossBoundData = {
    ...data,
    localStrategyLabEvidenceData: {
      ...data.localStrategyLabEvidenceData,
      strategyVersions: [{
        ...version,
        id: "foreign-version",
        dataSource: source("current", { databaseIds: { strategy_version_id: "foreign-version" } }),
      }, {
        ...version,
        dataSource: source("current", { databaseIds: { strategy_version_id: "version-20" } }),
      }],
      ranking: [crossBoundScore],
    },
  };
  assert.equal(partitionBrowserRecords(buildEvidenceRecords(crossBoundData, "scores")).current.length, 0);
});

function resultMetrics() {
  return {
    profitTotal: 1, profitPct: 1, maxDrawdownPct: 1, winRate: 0.5, totalTrades: 2,
    timerange: "20260101-20260201", sharpe: 1, sortino: 1, calmar: 1,
  };
}
