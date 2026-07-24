import assert from "node:assert/strict";
import test from "node:test";

import { buildLocalStrategyLabEvidenceSummary } from "../src/api/sourceState.ts";
import {
  evidenceStateDisplay,
  formatTraceEntries,
  partitionEvidenceRecords,
} from "../src/pages/localStrategyLab/evidenceDisplay.ts";

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

test("an empty API snapshot has a stable NOT_RUN evidence contract", () => {
  const summary = buildLocalStrategyLabEvidenceSummary({
    generationRuns: [],
    strategyVersions: [],
    backtestResults: [],
    ranking: [],
  });

  assert.equal(summary.state, "NOT_RUN");
  assert.equal(summary.canAccept, false);
  assert.equal(summary.stages.every((stage) => stage.state === "NOT_RUN"), true);
  assert.match(summary.reason, /尚未观察到真实 Provider/);
});

test("all evidence conclusion states have stable labels and tones", () => {
  const expected = {
    ACCEPTABLE: ["链路可验收", "success"],
    FAILED: ["链路失败", "danger"],
    BLOCKED: ["链路受阻", "warning"],
    NOT_RUN: ["尚未运行", "neutral"],
    NOT_ACCEPTABLE: ["不可验收", "warning"],
    API_GAP: ["API 证据缺口", "warning"],
  };

  for (const [state, [label, tone]] of Object.entries(expected)) {
    const display = evidenceStateDisplay(state);
    assert.equal(display.label, label);
    assert.equal(display.tone, tone);
  }
});

test("core records and non-core diagnostics are partitioned without mixing", () => {
  const core = source({ databaseIds: { strategy_generation_run_id: 1 } });
  const fixture = source({
    sourceType: "fixture",
    coreData: false,
    databaseIds: {},
    providerProvenance: "non-core",
  });
  const summary = {
    state: "NOT_ACCEPTABLE",
    canAccept: false,
    reason: "fixture only",
    nextAction: "run real flow",
    stages: [
      {
        key: "generation",
        label: "生成记录",
        state: "NOT_ACCEPTABLE",
        canAccept: false,
        reason: "fixture",
        nextAction: "run real provider",
        observedCount: 2,
        coreCount: 1,
        records: [
          { id: "1", parentId: null, status: "succeeded", provider: "deepseek", model: "deepseek-chat", artifactPath: null, source: core },
          { id: "2", parentId: null, status: "succeeded", provider: "fake", model: "offline-fixture", artifactPath: null, source: fixture },
        ],
      },
    ],
  };

  const partition = partitionEvidenceRecords(summary);
  assert.equal(partition.core.length, 1);
  assert.equal(partition.diagnostic.length, 1);
  assert.equal(partition.diagnostic[0].provider, "fake");
});

test("trace records use a copy-friendly stable multiline format", () => {
  assert.equal(
    formatTraceEntries({ strategy_generation_run_id: 11, strategy_version_id: 12 }),
    "strategy_generation_run_id: 11\nstrategy_version_id: 12",
  );
  assert.equal(formatTraceEntries({}), "暂无");
});
