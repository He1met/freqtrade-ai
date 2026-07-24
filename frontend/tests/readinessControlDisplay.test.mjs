import assert from "node:assert/strict";
import test from "node:test";

import {
  controlNextAction,
  dryRunBlockers,
  dryRunSafetyConclusion,
  readinessNextAction,
  readinessReason,
  resolvedControlStatus,
} from "../src/pages/localStrategyLab/readinessControlDisplay.ts";

function snapshot(overrides = {}) {
  return {
    status: "BLOCKED",
    profileName: "local-dry-run",
    strategyVersionId: 7,
    strategyName: "Candidate",
    exchange: "okx",
    pair: "BTC/USDT:USDT",
    timeframe: "15m",
    dryRun: true,
    balanceSummary: {},
    openTradesSummary: {},
    recentEvents: [],
    blockedReason: null,
    failedReason: null,
    skippedReason: null,
    lastUpdated: null,
    artifactManifestPath: null,
    ...overrides,
  };
}

function report(status, snapshotStatus) {
  return {
    status,
    generatedAt: "2026-07-24T00:00:00Z",
    manifestPath: "/tmp/manifest.json",
    configPath: "/tmp/config.json",
    statusSnapshotPath: "/tmp/status.json",
    readiness: null,
    statusSnapshot: snapshot({ status: snapshotStatus }),
    blockedReasons: [],
    failedReason: null,
    skippedReason: null,
    safety: {},
  };
}

test("control lifecycle states never masquerade as each other", () => {
  assert.equal(resolvedControlStatus({ kind: "starting", report: null, persistedStatus: "STOPPED" }), "STARTING");
  assert.equal(resolvedControlStatus({ kind: "stopping", report: null, persistedStatus: "RUNNING" }), "STOPPING");
  assert.equal(resolvedControlStatus({ kind: "failed", report: null, persistedStatus: "RUNNING" }), "FAILED");
  assert.equal(resolvedControlStatus({ kind: "complete", report: report("SUCCESS", "RUNNING"), persistedStatus: "STOPPED" }), "RUNNING");
  assert.equal(resolvedControlStatus({ kind: "complete", report: report("SUCCESS", "STOPPED"), persistedStatus: "RUNNING" }), "STOPPED");
  assert.equal(resolvedControlStatus({ kind: "complete", report: report("BLOCKED", "RUNNING"), persistedStatus: "RUNNING" }), "BLOCKED");
});

test("readiness reason prioritizes concrete fail-closed evidence", () => {
  const reason = readinessReason({
    status: "BLOCKED",
    summary: "summary",
    blockedReason: "缺少人工批准",
    unavailableReason: "unavailable",
    staleReason: "stale",
  });
  assert.equal(reason, "缺少人工批准");
  assert.match(readinessNextAction("BLOCKED", reason), /缺少人工批准/);
  assert.match(readinessNextAction("READY", "ready"), /仅在人工批准后/);
});

test("dry-run safety requires explicit dry_run=true", () => {
  assert.equal(dryRunSafetyConclusion(snapshot({ dryRun: true })).status, "PASS");
  assert.equal(dryRunSafetyConclusion(snapshot({ dryRun: false })).status, "BLOCKED");
  assert.equal(dryRunSafetyConclusion(snapshot({ dryRun: null })).status, "BLOCKED");
  assert.match(dryRunSafetyConclusion(snapshot({ dryRun: true })).reason, /不授予 live trading/);
});

test("persistent blockers are deduplicated and drive the next action", () => {
  const blockers = dryRunBlockers(
    {
      blockedReason: "配置缺失",
      failedReason: null,
      skippedReason: null,
    },
    snapshot({ blockedReason: "配置缺失", failedReason: "进程失败" }),
  );
  assert.deepEqual(blockers, ["配置缺失", "进程失败"]);
  assert.match(controlNextAction("FAILED", blockers), /配置缺失/);
  assert.match(controlNextAction("RUNNING", blockers), /dry_run=true/);
});
