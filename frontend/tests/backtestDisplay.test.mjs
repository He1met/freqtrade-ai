import assert from "node:assert/strict";
import test from "node:test";

import {
  emptyBacktestMetrics,
  findBacktestResultForRun,
  findBacktestResultForTask,
  missingBacktestResultReason,
} from "../src/pages/backtestResultLookup.ts";
import {
  backtestResultState,
  buildBacktestMatrixSummary,
  formatMatrixRangeValue,
  formatNumber,
  metricRows,
} from "../src/pages/backtestDisplay.ts";

const result = {
  id: "5",
  runId: "11",
  taskId: "12",
  resultPath: "user_data/backtest_results/result-5.json",
  metrics: {
    profitTotal: 1.2,
    profitPct: 0.01,
    maxDrawdownPct: 0,
    winRate: 0.6364,
    totalTrades: 11,
    timerange: "20240101-20240601",
    sharpe: 1.51,
    sortino: 2.75,
    calmar: 82.23,
  },
  createdAt: "2026-07-11T00:00:00Z",
  dataSource: {
    sourceType: "database",
    sourceDetail: "Persisted backtest result.",
    coreData: true,
    databaseIds: { backtest_result_id: 5 },
    artifactRefs: { result_path: "user_data/backtest_results/result-5.json" },
    freshness: null,
    blockedReason: null,
  },
};

test("run and task resolve the same persisted BacktestResult metrics", () => {
  assert.equal(findBacktestResultForRun([result], "11"), result);
  assert.equal(findBacktestResultForTask([result], "12"), result);
});

test("missing BacktestResult stays explicit instead of falling back to task or run metrics", () => {
  assert.equal(findBacktestResultForRun([result], "unknown"), null);
  assert.equal(findBacktestResultForTask([result], "unknown"), null);
  assert.match(missingBacktestResultReason("任务"), /核心 BacktestResult/);
  assert.deepEqual(emptyBacktestMetrics(), {
    profitTotal: null,
    profitPct: null,
    maxDrawdownPct: null,
    winRate: null,
    totalTrades: null,
    timerange: null,
    sharpe: null,
    sortino: null,
    calmar: null,
  });
});

test("null metrics remain distinct from real zero values", () => {
  assert.equal(formatNumber(null, "%"), "暂无");
  assert.equal(formatNumber(0, "%"), "0.00%");
  assert.equal(formatMatrixRangeValue("交易数", 24, ""), "24");

  const rows = Object.fromEntries(metricRows({
    ...emptyBacktestMetrics(),
    maxDrawdownPct: 0,
    totalTrades: 0,
    winRate: 0,
  }));
  assert.equal(rows["收益"], "暂无");
  assert.equal(rows["回撤"], "0.00%");
  assert.equal(rows["交易数"], "0");
  assert.equal(rows["胜率"], "0.00%");
});

test("a succeeded task without a persisted result is not accepted as matrix success", () => {
  const task = {
    id: "task-1",
    runId: "run-1",
    strategyName: "StrategyA",
    pair: "BTC/USDT",
    timeframe: "1h",
    status: "succeeded",
    configPath: null,
    resultPath: null,
    profitPct: 99,
    errorMessage: null,
    artifactManifest: null,
    metrics: {
      ...emptyBacktestMetrics(),
      profitPct: 99,
      maxDrawdownPct: 20,
      totalTrades: 100,
    },
    blockedReason: null,
    failedReason: null,
  };

  const summary = buildBacktestMatrixSummary([], [task], []);
  assert.equal(backtestResultState("succeeded", false), "RESULT_MISSING");
  assert.equal(summary.status, "RESULT_MISSING");
  assert.equal(summary.statusCounts.RESULT_MISSING, 1);
  assert.equal(summary.metricRanges[0].avg, null);
  assert.match(summary.reasons[0].reason, /BacktestResult/);
});

test("matrix ranges use only linked BacktestResult metrics", () => {
  const task = {
    id: "12",
    runId: "11",
    strategyName: "StrategyA",
    pair: "BTC/USDT",
    timeframe: "1h",
    status: "succeeded",
    configPath: null,
    resultPath: null,
    profitPct: 99,
    errorMessage: null,
    artifactManifest: null,
    metrics: {
      ...emptyBacktestMetrics(),
      profitPct: 99,
      maxDrawdownPct: 99,
      totalTrades: 999,
    },
    blockedReason: null,
    failedReason: null,
  };

  const summary = buildBacktestMatrixSummary([], [task], [result]);
  assert.equal(summary.status, "SUCCESS");
  assert.equal(summary.metricRanges[0].avg, result.metrics.profitPct);
  assert.equal(summary.metricRanges[1].avg, 0);
  assert.equal(summary.metricRanges[3].avg, 11);
});
