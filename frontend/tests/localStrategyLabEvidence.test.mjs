import assert from "node:assert/strict";
import test from "node:test";

import { buildLocalStrategyLabEvidenceSummary } from "../src/api/sourceState.ts";

function source(overrides = {}) {
  return {
    sourceType: "database",
    sourceDetail: "Persisted database record.",
    coreData: true,
    databaseIds: { id: 1 },
    artifactRefs: {},
    freshness: null,
    blockedReason: null,
    providerProvenance: "real",
    ...overrides,
  };
}

test("fake persisted generation is diagnosed as non-core instead of a successful lab chain", () => {
  const summary = buildLocalStrategyLabEvidenceSummary({
    generationRuns: [{ id: "15", status: "succeeded", provider: "fake", model: "offline-fixture", dataSource: source({ coreData: false, providerProvenance: "non-core" }) }],
    strategyVersions: [],
    backtestResults: [],
    ranking: [],
  });

  assert.equal(summary.state, "NOT_ACCEPTABLE");
  assert.equal(summary.canAccept, false);
  assert.equal(summary.stages[0].records[0].provider, "fake");
  assert.match(summary.reason, /fake/i);
});

test("a complete core provider-to-score chain is acceptable", () => {
  const summary = buildLocalStrategyLabEvidenceSummary({
    generationRuns: [{ id: "1", status: "succeeded", provider: "deepseek", model: "deepseek-chat", dataSource: source({ databaseIds: { strategy_generation_run_id: 1 } }) }],
    strategyVersions: [{ id: "2", strategyId: "3", validationStatus: "passed", filePath: "user_data/strategies/S.py", dataSource: source({ databaseIds: { strategy_version_id: 2 } }) }],
    backtestResults: [{ id: "4", taskId: "5", resultPath: "results/4.json", dataSource: source({ databaseIds: { backtest_result_id: 4 } }) }],
    ranking: [{ scoreId: "6", backtestResultId: "4", filePath: "user_data/strategies/S.py", dataSource: source({ databaseIds: { strategy_score_id: 6 } }) }],
  });

  assert.equal(summary.state, "ACCEPTABLE");
  assert.equal(summary.canAccept, true);
  assert.equal(summary.stages.every((stage) => stage.canAccept), true);
});
