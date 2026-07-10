import assert from "node:assert/strict";
import test from "node:test";

import { getDataSourceAcceptance, isCoreDataSourceTrace } from "../src/api/sourceState.ts";

test("database source with database_ids is ACCEPTABLE", () => {
  const source = {
    sourceType: "database",
    sourceDetail: "Persisted strategy row loaded from database.",
    coreData: true,
    databaseIds: { strategy_id: 12 },
    artifactRefs: {},
    freshness: null,
    blockedReason: null,
  };

  const acceptance = getDataSourceAcceptance(source);
  assert.equal(acceptance.state, "ACCEPTABLE");
  assert.equal(acceptance.canAccept, true);
  assert.equal(isCoreDataSourceTrace(source), true);
});

test("database source without database_ids is API_GAP", () => {
  const acceptance = getDataSourceAcceptance({
    sourceType: "database",
    sourceDetail: "Persisted strategy row loaded from database.",
    coreData: true,
    databaseIds: {},
    artifactRefs: {},
    freshness: null,
    blockedReason: null,
  });

  assert.equal(acceptance.state, "API_GAP");
  assert.equal(acceptance.canAccept, false);
  assert.match(acceptance.reason, /database_ids/);
});

test("fixture source is NOT_ACCEPTABLE", () => {
  const acceptance = getDataSourceAcceptance({
    sourceType: "fixture",
    sourceDetail: "Fixture strategy row for local demo only.",
    coreData: false,
    databaseIds: {},
    artifactRefs: {},
    freshness: null,
    blockedReason: null,
  });

  assert.equal(acceptance.state, "NOT_ACCEPTABLE");
});

test("blocked source is BLOCKED", () => {
  const acceptance = getDataSourceAcceptance({
    sourceType: "fallback",
    sourceDetail: "Fallback payload while local data is missing.",
    coreData: false,
    databaseIds: {},
    artifactRefs: {},
    freshness: null,
    blockedReason: "user_data/data 下未找到本地行情数据文件。",
  });

  assert.equal(acceptance.state, "BLOCKED");
  assert.match(acceptance.nextAction, /BLOCKED/);
});

test("failed source is FAILED", () => {
  const acceptance = getDataSourceAcceptance({
    sourceType: "failed",
    sourceDetail: "Backtest execution failed with exit code 2.",
    coreData: false,
    databaseIds: {},
    artifactRefs: {},
    freshness: null,
    blockedReason: null,
  });

  assert.equal(acceptance.state, "FAILED");
});

test("unknown source with not-run detail is NOT_RUN", () => {
  const acceptance = getDataSourceAcceptance({
    sourceType: "unknown",
    sourceDetail: "尚未运行真实本地流程，因此没有可复核证据。",
    coreData: false,
    databaseIds: {},
    artifactRefs: {},
    freshness: null,
    blockedReason: null,
  });

  assert.equal(acceptance.state, "NOT_RUN");
});

test("missing source metadata is API_GAP", () => {
  const acceptance = getDataSourceAcceptance(undefined);
  assert.equal(acceptance.state, "API_GAP");
});
