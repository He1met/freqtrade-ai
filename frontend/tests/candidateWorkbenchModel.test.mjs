import assert from "node:assert/strict";
import test from "node:test";

import { mockMvpData } from "../src/data/mock.ts";
import {
  EMPTY_LAB_SELECTION,
  backtestBlockReason,
  buildLocalBacktestProfile,
  candidateWorkbenchChain,
  DEFAULT_BACKTEST_PROFILE_DRAFT,
  displayOptionalTradeCount,
  ingestBlockReason,
  reconcileLabSelection,
  sanitizedBlockedReasons,
  selectLabEntity,
} from "../src/pages/localStrategyLab/candidateWorkbenchModel.ts";

test("backtest draft does not invent a default pair or timeframe", () => {
  assert.equal(DEFAULT_BACKTEST_PROFILE_DRAFT.pair, "");
  assert.equal(DEFAULT_BACKTEST_PROFILE_DRAFT.timeframe, "");
});

test("missing OOS trade evidence stays UNKNOWN instead of becoming zero", () => {
  assert.equal(displayOptionalTradeCount(undefined), "UNKNOWN");
  assert.equal(displayOptionalTradeCount(null), "UNKNOWN");
  assert.equal(displayOptionalTradeCount(0), "0");
});

function source(ids, overrides = {}) {
  return {
    sourceType: "database",
    sourceDetail: "unit test",
    coreData: true,
    databaseIds: ids,
    artifactRefs: {},
    freshness: null,
    blockedReason: null,
    environment: {
      scope: "current",
      runnable: true,
      migrationVerified: false,
      reason: "current test environment",
    },
    ...overrides,
  };
}

function version(id, strategyId = "10") {
  return {
    id,
    strategyId,
    generationRunId: "1",
    parentVersionId: null,
    versionNumber: 1,
    filePath: `user_data/strategies/generated/V${id}.py`,
    validationStatus: "valid",
    validationErrors: [],
    changeSummary: null,
    fileState: {
      status: "SUCCESS",
      path: `user_data/strategies/generated/V${id}.py`,
      exists: true,
      isFile: true,
      checksum: "abc",
      checksumMatches: true,
      className: `V${id}`,
      blockedReason: null,
      validationErrors: [],
    },
    createdAt: null,
    dataSource: source({ strategy_version_id: Number(id), strategy_id: Number(strategyId) }),
  };
}

function populatedData() {
  const data = structuredClone(mockMvpData);
  data.strategyVersions = [version("201")];
  data.backtestRuns = [{
    id: "501",
    strategyVersionId: "201",
    strategyName: "V201",
    status: "succeeded",
    profileName: "local",
    configSnapshot: {},
    requestedTaskCount: 1,
    completedTaskCount: 1,
    profitPct: null,
    maxDrawdownPct: null,
    artifactManifest: null,
    metrics: {},
    blockedReason: null,
    failedReason: null,
    dataSource: source({ backtest_run_id: 501, strategy_version_id: 201 }),
  }];
  data.backtestTasks = [{
    id: "601",
    runId: "501",
    strategyName: "V201",
    pair: "BTC/USDT",
    timeframe: "5m",
    status: "succeeded",
    configPath: "/tmp/config.json",
    resultPath: "/tmp/result.json",
    profitPct: null,
    errorMessage: null,
    artifactManifest: null,
    metrics: {},
    blockedReason: null,
    failedReason: null,
    dataSource: source({ backtest_task_id: 601, backtest_run_id: 501 }),
  }];
  data.backtestResults = [{
    id: "401",
    runId: "501",
    taskId: "601",
    resultPath: "/tmp/result.json",
    metrics: {},
    createdAt: null,
    dataSource: source({ backtest_result_id: 401, backtest_run_id: 501, backtest_task_id: 601 }),
  }];
  data.ranking = [{
    rank: 1,
    scoreId: "701",
    strategyId: "10",
    strategyVersionId: "201",
    backtestResultId: "401",
    strategyName: "V201",
    versionNumber: 1,
    filePath: "user_data/strategies/generated/V201.py",
    scoringVersion: "v1",
    totalScore: 80,
    rawTotalScore: 80,
    profitScore: 80,
    riskScore: 80,
    stabilityScore: 80,
    qualityScore: 80,
    scoreBreakdown: [],
    elimination: { eliminated: false, reasons: [] },
    warnings: [],
    dataSource: source({
      strategy_score_id: 701,
      strategy_version_id: 201,
      backtest_result_id: 401,
    }, { sourceType: "api_aggregate" }),
  }];
  return data;
}

test("selection contains only five database IDs and a unique chain reconciles from GET evidence", () => {
  const data = populatedData();
  const selection = reconcileLabSelection(data, EMPTY_LAB_SELECTION);

  assert.deepEqual(selection, {
    strategyVersionId: "201",
    backtestRunId: "501",
    backtestTaskId: "601",
    backtestResultId: "401",
    scoreId: "701",
  });
  assert.deepEqual(Object.keys(selection).sort(), [
    "backtestResultId",
    "backtestRunId",
    "backtestTaskId",
    "scoreId",
    "strategyVersionId",
  ]);
});

test("multiple candidates never default to the first record", () => {
  const data = populatedData();
  data.strategyVersions.push(version("202", "11"));

  const selection = reconcileLabSelection(data, EMPTY_LAB_SELECTION);
  assert.equal(selection.strategyVersionId, null);
  assert.equal(candidateWorkbenchChain(data, selection).runs.length, 0);
});

test("historical non-runnable or mismatched database IDs fail closed", () => {
  const data = populatedData();
  data.strategyVersions.push({
    ...version("202"),
    dataSource: source({ strategy_version_id: 999 }),
  });
  data.strategyVersions.push({
    ...version("203"),
    dataSource: source(
      { strategy_version_id: 203 },
      { environment: { scope: "historical", runnable: false, migrationVerified: false, reason: "old" } },
    ),
  });

  const chain = candidateWorkbenchChain(data, EMPTY_LAB_SELECTION);
  assert.deepEqual(chain.versions.map((item) => item.id), ["201"]);
});

test("upstream selection changes clear every downstream ID", () => {
  const selected = {
    strategyVersionId: "201",
    backtestRunId: "501",
    backtestTaskId: "601",
    backtestResultId: "401",
    scoreId: "701",
  };
  assert.deepEqual(selectLabEntity(selected, "strategyVersionId", "202"), {
    ...EMPTY_LAB_SELECTION,
    strategyVersionId: "202",
  });
  assert.deepEqual(selectLabEntity(selected, "backtestTaskId", "602"), {
    ...selected,
    backtestTaskId: "602",
    backtestResultId: null,
    scoreId: null,
  });
});

test("reconcile removes records that disappear or cross the selected candidate chain", () => {
  const data = populatedData();
  const selected = reconcileLabSelection(data, EMPTY_LAB_SELECTION);
  data.backtestTasks[0].runId = "999";

  const reconciled = reconcileLabSelection(data, selected);
  assert.equal(reconciled.strategyVersionId, "201");
  assert.equal(reconciled.backtestRunId, "501");
  assert.equal(reconciled.backtestTaskId, null);
  assert.equal(reconciled.backtestResultId, null);
  assert.equal(reconciled.scoreId, null);
});

test("running backtests block duplicate POST and completed score blocks duplicate ingest", () => {
  const data = populatedData();
  const selection = reconcileLabSelection(data, EMPTY_LAB_SELECTION);
  data.backtestRuns[0].status = "running";
  const profile = buildLocalBacktestProfile({
    profileName: "unit-profile",
    pair: "BTC/USDT",
    timeframe: "5m",
    timerange: "20240101-20240201",
  }, { name: "V201", path: "user_data/strategies/generated/V201.py" }).profile;
  assert.ok(backtestBlockReason(data, selection, "operator-token", profile, null).includes("已有 PENDING/RUNNING"));

  data.backtestRuns[0].status = "succeeded";
  assert.match(ingestBlockReason(data, selection, "operator-token"), /已有持久 BacktestResult 和 StrategyScore/);
});

test("BacktestProfileV2 is complete and invalid or missing timeranges fail closed", () => {
  const valid = buildLocalBacktestProfile({
    profileName: "unit-profile",
    pair: "btc/usdt",
    timeframe: "5M",
    timerange: "20240101-20240201",
  }, { name: "V201", path: "user_data/strategies/generated/V201.py" });
  assert.equal(valid.reason, null);
  assert.deepEqual(valid.profile.strategy, {
    name: "V201",
    path: "user_data/strategies/generated/V201.py",
  });
  assert.equal(valid.profile.pair, "BTC/USDT");
  assert.equal(valid.profile.safety.allow_live_trading, false);

  const missing = buildLocalBacktestProfile({
    profileName: "unit-profile",
    pair: "BTC/USDT",
    timeframe: "5m",
    timerange: "",
  }, { name: "V201", path: "V201.py" });
  assert.equal(missing.profile, null);
  assert.match(missing.reason, /YYYYMMDD/);
});

test("only api_aggregate scores with three exact IDs enter the selected chain", () => {
  const data = populatedData();
  const selection = reconcileLabSelection(data, EMPTY_LAB_SELECTION);
  assert.equal(candidateWorkbenchChain(data, selection).scores.length, 1);

  data.ranking[0].dataSource.sourceType = "database";
  assert.equal(candidateWorkbenchChain(data, selection).scores.length, 0);
  data.ranking[0].dataSource.sourceType = "api_aggregate";
  delete data.ranking[0].dataSource.databaseIds.backtest_result_id;
  assert.equal(candidateWorkbenchChain(data, selection).scores.length, 0);
});

test("same candidate and profile cannot repeat a blocked run and reasons are sanitized", () => {
  const data = populatedData();
  data.backtestRuns[0].status = "blocked";
  data.backtestRuns[0].configSnapshot = {
    profile: {
      profile_name: "unit-profile",
      pair: "BTC/USDT",
      timeframe: "5m",
      timerange: "20240101-20240201",
      strategy: { name: "V201", path: "user_data/strategies/generated/V201.py" },
    },
  };
  const selection = reconcileLabSelection(data, EMPTY_LAB_SELECTION);
  const profile = buildLocalBacktestProfile({
    profileName: "unit-profile",
    pair: "BTC/USDT",
    timeframe: "5m",
    timerange: "20240101-20240201",
  }, { name: "V201", path: "user_data/strategies/generated/V201.py" }).profile;
  assert.match(backtestBlockReason(data, selection, "operator-token", profile, null), /修改 profile/);
  assert.equal(
    sanitizedBlockedReasons(["<b>missing data</b>", "token=secret-value"]),
    "missing data；token=[REDACTED]",
  );
});
