import assert from "node:assert/strict";
import test from "node:test";

import {
  buildRankingViewModel,
  isAcceptableRankingEntry,
  rankingConclusion,
  scoreEvidenceStage,
} from "../src/pages/rankingDisplay.ts";

function source(overrides = {}) {
  return {
    sourceType: "api_aggregate",
    sourceDetail: "Persisted StrategyScore ranking entry.",
    coreData: true,
    databaseIds: { strategy_score_id: 6, backtest_result_id: 4 },
    artifactRefs: { strategy_file_path: "user_data/strategies/S.py" },
    freshness: null,
    blockedReason: null,
    providerProvenance: "real",
    ...overrides,
  };
}

function entry(overrides = {}) {
  return {
    rank: 1,
    scoreId: "6",
    strategyId: "2",
    strategyVersionId: "3",
    backtestResultId: "4",
    strategyName: "CoreStrategy",
    versionNumber: 1,
    filePath: "user_data/strategies/S.py",
    scoringVersion: "v1",
    totalScore: 88,
    rawTotalScore: 88,
    profitScore: 90,
    riskScore: 85,
    stabilityScore: 87,
    qualityScore: 89,
    scoreBreakdown: [],
    elimination: { eliminated: false, reasons: [] },
    warnings: [],
    dataSource: source(),
    ...overrides,
  };
}

function stage(overrides = {}) {
  return {
    key: "score",
    label: "评分 / 排行榜",
    state: "ACCEPTABLE",
    canAccept: true,
    reason: "core score exists",
    nextAction: "refresh",
    observedCount: 1,
    coreCount: 1,
    records: [
      {
        id: "6",
        parentId: "4",
        status: "SUCCESS",
        provider: null,
        model: null,
        artifactPath: "user_data/strategies/S.py",
        source: source(),
      },
    ],
    ...overrides,
  };
}

test("score stage is reused from Local Strategy Lab evidence", () => {
  assert.equal(scoreEvidenceStage([{ ...stage(), key: "generation" }, stage()]).key, "score");
});

test("only entries linked to core StrategyScore and BacktestResult evidence are acceptable", () => {
  assert.equal(isAcceptableRankingEntry(entry(), stage()), true);
  assert.equal(isAcceptableRankingEntry(entry({ backtestResultId: null }), stage()), false);
  assert.equal(
    isAcceptableRankingEntry(
      entry({ dataSource: source({ databaseIds: { strategy_score_id: 6 } }) }),
      stage(),
    ),
    false,
  );
});

test("normal ranking contains only acceptable entries", () => {
  const view = buildRankingViewModel({
    entries: [entry(), entry({ scoreId: "7", strategyName: "MissingEvidence" })],
    error: null,
    scoreStage: stage(),
    source: "api",
  });

  assert.equal(view.kind, "normal");
  assert.equal(view.entries.length, 1);
  assert.equal(view.entries[0].strategyName, "CoreStrategy");
});

test("real empty and filtered records have different conclusions", () => {
  const empty = buildRankingViewModel({
    entries: [],
    error: null,
    scoreStage: stage({
      state: "NOT_RUN",
      canAccept: false,
      observedCount: 0,
      coreCount: 0,
      records: [],
      reason: "尚未观察到关联核心回测结果的评分记录。",
    }),
    source: "api",
  });
  const filtered = buildRankingViewModel({
    entries: [],
    error: null,
    scoreStage: stage({
      state: "NOT_ACCEPTABLE",
      canAccept: false,
      observedCount: 2,
      coreCount: 0,
    }),
    source: "api",
  });

  assert.equal(empty.kind, "empty");
  assert.equal(filtered.kind, "filtered");
  assert.match(filtered.summary, /观察到 2 条/);
});

test("failed score evidence remains a failure", () => {
  const view = buildRankingViewModel({
    entries: [],
    error: null,
    scoreStage: stage({ state: "FAILED", canAccept: false, reason: "score failed" }),
    source: "api",
  });

  assert.equal(view.kind, "failed");
  assert.match(view.summary, /score failed/);
});

test("ranking conclusion keeps elimination and warnings explicit", () => {
  assert.equal(rankingConclusion(entry()).label, "已入榜");
  assert.equal(
    rankingConclusion(
      entry({
        warnings: [{ code: "drawdown", severity: "warning", message: "回撤接近阈值" }],
      }),
    ).label,
    "入榜，需关注",
  );
  assert.equal(
    rankingConclusion(
      entry({
        elimination: {
          eliminated: true,
          reasons: [{ code: "risk", severity: "error", message: "风险超限" }],
        },
      }),
    ).label,
    "已淘汰",
  );
});
