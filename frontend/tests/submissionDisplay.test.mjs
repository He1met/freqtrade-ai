import assert from "node:assert/strict";
import test from "node:test";

import {
  hasPersistentGenerationEvidence,
  submissionDisplayModel,
} from "../src/pages/localStrategyLab/submissionDisplay.ts";

function source(sourceType, databaseIds = { id: 1 }) {
  return {
    sourceType,
    sourceDetail: "persisted record",
    coreData: true,
    databaseIds,
    artifactRefs: {},
    freshness: null,
    blockedReason: null,
    providerProvenance: "real",
  };
}

function result(overrides = {}) {
  return {
    run: {
      id: "11",
      status: "succeeded",
      dataSource: source("database", { strategy_generation_run_id: 11 }),
    },
    strategies: [
      { id: "12", dataSource: source("database", { strategy_id: 12 }) },
    ],
    strategyVersions: [
      {
        id: "13",
        filePath: "user_data/strategies/generated/Test.py",
        dataSource: source("database", { strategy_version_id: 13 }),
      },
    ],
    dataSource: source("api_aggregate", { strategy_generation_run_id: 11 }),
    ...overrides,
  };
}

test("idle and submitting states remain distinct from success", () => {
  assert.equal(submissionDisplayModel({ kind: "idle" }).status, "IDLE");
  assert.equal(
    submissionDisplayModel({ kind: "submitting", promptSummary: "test", requestedCount: 1 }).status,
    "RUNNING",
  );
});

test("success requires complete API and database persistence evidence", () => {
  const persisted = result();
  assert.equal(hasPersistentGenerationEvidence(persisted), true);
  assert.equal(submissionDisplayModel({ kind: "success", result: persisted }).status, "SUCCESS");

  const missingIds = result({
    dataSource: source("api_aggregate", {}),
  });
  assert.equal(hasPersistentGenerationEvidence(missingIds), false);
  assert.equal(submissionDisplayModel({ kind: "success", result: missingIds }).status, "API_GAP");
});

test("blocked response with incomplete result is classified as API_GAP", () => {
  const display = submissionDisplayModel({
    kind: "blocked",
    message: "Backend 响应缺少 database_ids。",
    result: result({ dataSource: source("api_aggregate", {}) }),
  });

  assert.equal(display.status, "API_GAP");
  assert.match(display.nextAction, /database_ids/);
});

test("failed, blocked, and unauthorized states have precise conclusions", () => {
  assert.equal(
    submissionDisplayModel({
      kind: "failed",
      message: "Provider timeout",
      runId: "15",
      statusCode: 502,
      statusText: "Bad Gateway",
    }).status,
    "FAILED",
  );
  assert.equal(
    submissionDisplayModel({ kind: "blocked", message: "缺少策略构想。" }).status,
    "BLOCKED",
  );
  assert.equal(
    submissionDisplayModel({
      kind: "unauthorized",
      message: "invalid operator authorization",
      statusCode: 401,
      statusText: "Unauthorized",
    }).status,
    "UNAUTHORIZED",
  );
});
