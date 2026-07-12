import assert from "node:assert/strict";
import test from "node:test";

import {
  emptyBacktestMetrics,
  findBacktestResultForRun,
  findBacktestResultForTask,
  missingBacktestResultReason,
} from "../src/pages/backtestResultLookup.ts";

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
