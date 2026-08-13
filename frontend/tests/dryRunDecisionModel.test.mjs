import assert from "node:assert/strict";
import test from "node:test";

import { requiredDryRunTargetField } from "../src/api/dryRunTarget.ts";

import {
  candidateIdentityMatches,
  deriveDryRunCandidate,
  deriveDryRunDecision,
  deriveDryRunRequestTarget,
  inactiveActionLabel,
  readinessMatchesCandidate,
  readinessTerminalStatus,
  reconcileCandidateScopedValue,
  runningEvidenceReason,
} from "../src/pages/localStrategyLab/dryRunDecisionModel.ts";

const candidate = { strategyVersionId: "201", strategyName: "Candidate201" };
const selection = {
  strategyVersionId: "201",
  backtestRunId: null,
  backtestTaskId: null,
  backtestResultId: null,
  scoreId: null,
};
const manifest = {
  status: "SUCCESS",
  strategyVersionId: 201,
  manifestPath: "/current/dry-run.json",
};
const snapshot = {
  status: "RUNNING",
  strategyVersionId: 201,
  dryRun: true,
  artifactManifestPath: "/current/dry-run.json",
};
const readiness = {
  status: "READY",
  strategyVersionId: "201",
  blockedReasons: [],
  checks: [{ status: "READY" }],
  configPreview: { dry_run: true, initial_state: "stopped" },
  safety: {
    readiness_only: true,
    starts_freqtrade: false,
    exchange_connection: false,
    live_trading: false,
    real_orders: false,
  },
};

test("RUNNING requires API source, dry_run=true and matching candidate/manifest identity", () => {
  assert.equal(runningEvidenceReason({ candidate, dryRunSource: "api", manifest, snapshot }), null);
  assert.match(runningEvidenceReason({ candidate, dryRunSource: "fixture", manifest, snapshot }), /不是 API/);
  assert.match(runningEvidenceReason({
    candidate,
    dryRunSource: "api",
    manifest,
    snapshot: { ...snapshot, dryRun: false },
  }), /dry_run=true/);
  assert.match(runningEvidenceReason({
    candidate,
    dryRunSource: "api",
    manifest,
    snapshot: { ...snapshot, strategyVersionId: 999 },
  }), /不匹配/);
  assert.match(runningEvidenceReason({
    candidate,
    dryRunSource: "api",
    manifest,
    snapshot: { ...snapshot, artifactManifestPath: "/historical/other.json" },
  }), /manifest/);
});

test("readiness report is READY only for the current candidate and safe config preview", () => {
  assert.equal(readinessMatchesCandidate(readiness, candidate), true);
  assert.equal(readinessMatchesCandidate({ ...readiness, strategyVersionId: "202" }, candidate), false);
  assert.equal(readinessMatchesCandidate({
    ...readiness,
    configPreview: { dry_run: false, initial_state: "stopped" },
  }, candidate), false);
  assert.equal(readinessMatchesCandidate({
    ...readiness,
    safety: { ...readiness.safety, live_trading: true },
  }, candidate), false);
  assert.equal(readinessMatchesCandidate({
    ...readiness,
    checks: [],
  }, candidate), false);
  assert.equal(readinessTerminalStatus(readiness, candidate), "SUCCESS");
  assert.equal(
    readinessTerminalStatus({ ...readiness, strategyVersionId: "999" }, candidate),
    "API_GAP",
  );
  assert.equal(
    readinessTerminalStatus({
      ...readiness,
      safety: { ...readiness.safety, live_trading: true },
    }, candidate),
    "BLOCKED",
  );
});

test("persistent RUNNING wins over action history and exposes only stop", () => {
  const model = deriveDryRunDecision({
    candidate,
    dryRunSource: "api",
    manifest,
    readiness: null,
    runtimeReason: "runtime blocked",
    snapshot,
    transient: { kind: "idle" },
  });
  assert.equal(model.state, "RUNNING");
  assert.equal(model.action, "stop");
  assert.equal(model.blocker, null);
});

test("unsafe RUNNING fails closed and offers stop only for matching current API identity", () => {
  const model = deriveDryRunDecision({
    candidate,
    dryRunSource: "api",
    manifest,
    readiness: null,
    runtimeReason: "runtime blocked",
    snapshot: { ...snapshot, dryRun: false },
    transient: { kind: "idle" },
  });
  assert.equal(model.state, "BLOCKED");
  assert.equal(model.action, "stop");
  assert.match(model.blocker, /dry_run=true/);

  const fixture = deriveDryRunDecision({
    candidate,
    dryRunSource: "fixture",
    manifest,
    readiness: null,
    runtimeReason: "runtime blocked",
    snapshot,
    transient: { kind: "idle" },
  });
  assert.equal(fixture.state, "BLOCKED");
  assert.equal(fixture.action, "check");
});

test("transient action state never impersonates persistent RUNNING", () => {
  const model = deriveDryRunDecision({
    candidate,
    dryRunSource: "api",
    manifest: null,
    readiness,
    runtimeReason: "runtime blocked",
    snapshot: { ...snapshot, status: "BLOCKED", artifactManifestPath: null },
    transient: { kind: "starting" },
  });
  assert.equal(model.state, "STARTING");
  assert.equal(model.persistedRunning, false);
  assert.equal(model.action, null);
});

test("missing current core candidate remains fail closed", () => {
  const model = deriveDryRunDecision({
    candidate: null,
    dryRunSource: "api",
    manifest,
    readiness,
    runtimeReason: "runtime blocked",
    snapshot,
    transient: { kind: "idle" },
  });
  assert.equal(model.state, "BLOCKED");
  assert.equal(model.action, null);
  assert.match(model.blocker, /database_ids/);
});

test("historical candidate is excluded even when its core flags and IDs look valid", () => {
  const historicalSource = {
    sourceType: "database",
    coreData: true,
    databaseIds: { strategy_version_id: 201 },
    artifactRefs: {},
    environment: { scope: "historical", runnable: true, migrationVerified: false, reason: "old repo" },
  };
  assert.equal(deriveDryRunCandidate({
    ranking: [],
    strategies: [],
    strategyVersions: [{
      id: "201",
      strategyId: "301",
      filePath: "/current/Historical.py",
      validationStatus: "valid",
      dataSource: historicalSource,
      fileState: { className: "Historical", exists: true, isFile: true },
    }],
  }, selection), null);
});

test("candidate requires an exact strategy_version_id database identity", () => {
  const build = (databaseIds) => deriveDryRunCandidate({
    ranking: [],
    strategies: [],
    strategyVersions: [{
      id: "201",
      strategyId: "301",
      filePath: "/current/Candidate.py",
      validationStatus: "valid",
      dataSource: {
        sourceType: "database",
        coreData: true,
        databaseIds,
        artifactRefs: {},
        environment: { scope: "current", runnable: true, migrationVerified: false, reason: "current" },
      },
      fileState: { className: "Candidate", exists: true, isFile: true },
    }],
  }, selection);
  assert.equal(build({}), null);
  assert.equal(build({ strategy_version_id: 999 }), null);
  assert.equal(build({ strategy_version_id: 201 }).strategyVersionId, "201");
});

test("Dry-run uses only the shared selected strategy version", () => {
  const data = {
    ranking: [],
    strategies: [],
    strategyVersions: [{
      id: "201",
      strategyId: "301",
      filePath: "/current/Candidate.py",
      validationStatus: "valid",
      dataSource: {
        sourceType: "database",
        coreData: true,
        databaseIds: { strategy_version_id: 201 },
        artifactRefs: {},
        environment: { scope: "current", runnable: true, migrationVerified: false, reason: "current" },
      },
      fileState: { className: "Candidate", exists: true, isFile: true },
    }],
  };
  assert.equal(deriveDryRunCandidate(data, { ...selection, strategyVersionId: null }), null);
  assert.equal(deriveDryRunCandidate(data, { ...selection, strategyVersionId: "999" }), null);
  assert.equal(deriveDryRunCandidate(data, selection).strategyVersionId, "201");
});

test("Dry-run target comes only from the selected persisted BacktestProfile", () => {
  const runSource = {
    sourceType: "database",
    coreData: true,
    databaseIds: { backtest_run_id: 501, strategy_version_id: 201 },
    artifactRefs: {},
    environment: { scope: "current", runnable: true, migrationVerified: false, reason: "current" },
  };
  const targetData = {
    strategyVersions: [],
    backtestRuns: [{
      id: "501",
      strategyVersionId: "201",
      configSnapshot: {
        profile: {
          pair: "ETH/USDT:USDT",
          timeframe: "1h",
          data_source: { exchange: "kraken" },
        },
      },
      dataSource: runSource,
    }],
    backtestTasks: [],
    backtestResults: [],
    ranking: [],
  };
  const targetSelection = { ...selection, backtestRunId: "501" };

  assert.deepEqual(deriveDryRunRequestTarget(targetData, targetSelection), {
    pair: "ETH/USDT:USDT",
    timeframe: "1h",
    exchange: "kraken",
  });
  targetData.backtestRuns[0].configSnapshot.profile.data_source = {};
  assert.equal(deriveDryRunRequestTarget(targetData, targetSelection), null);
  assert.equal(deriveDryRunRequestTarget(targetData, { ...targetSelection, backtestRunId: null }), null);
});

test("Dry-run API target guard rejects incomplete fields before request construction", () => {
  assert.throws(
    () => requiredDryRunTargetField("", "pair"),
    /缺少显式 pair；未发送 API 请求/,
  );
  assert.equal(requiredDryRunTargetField(" ETH/USDT:USDT ", "pair"), "ETH/USDT:USDT");
});

test("all nine states expose at most one action and one blocker", () => {
  const base = {
    candidate,
    dryRunSource: "api",
    manifest,
    readiness: null,
    runtimeReason: "runtime blocked",
    snapshot: { ...snapshot, status: "UNKNOWN" },
    transient: { kind: "idle" },
  };
  const cases = [
    ["NOT_CHECKED", {}],
    ["CHECKING", { transient: { kind: "checking" } }],
    ["READY", { readiness }],
    ["BLOCKED", { transient: { kind: "reconcile-blocked", operation: "start", reason: "refresh failed" } }],
    ["STARTING", { transient: { kind: "starting" } }],
    ["RUNNING", { snapshot }],
    ["STOPPING", { transient: { kind: "stopping" } }],
    ["STOPPED", { snapshot: { ...snapshot, status: "STOPPED" } }],
    ["FAILED", { transient: { kind: "failed", reason: "request failed" } }],
  ];
  for (const [expected, override] of cases) {
    const model = deriveDryRunDecision({ ...base, ...override });
    assert.equal(model.state, expected);
    assert.ok(model.action === null || ["check", "refresh", "start", "stop"].includes(model.action));
    assert.ok(model.blocker === null || typeof model.blocker === "string");
  }
});

test("stop request failure cannot demote a verified persistent RUNNING state", () => {
  const model = deriveDryRunDecision({
    candidate,
    dryRunSource: "api",
    manifest,
    readiness: null,
    runtimeReason: "runtime blocked",
    snapshot,
    transient: { kind: "failed", reason: "stop API failed" },
  });
  assert.equal(model.state, "RUNNING");
  assert.equal(model.action, "stop");
  assert.equal(model.blocker, null);
  assert.match(model.nextAction, /停止请求失败/);
});

test("reconciliation blocker wins when refreshed data temporarily loses the candidate", () => {
  const model = deriveDryRunDecision({
    candidate: null,
    dryRunSource: "failed",
    manifest: null,
    readiness: null,
    runtimeReason: "API failed",
    snapshot: { ...snapshot, status: "BLOCKED", strategyVersionId: null },
    transient: { kind: "reconcile-blocked", operation: "start", reason: "控制结果待对账" },
  });
  assert.equal(model.state, "BLOCKED");
  assert.equal(model.blocker, "控制结果待对账");
  assert.equal(model.action, "refresh");
});

test("readiness and approval never survive A to B or A to null to A identity changes", () => {
  const readyA = {
    strategyVersionId: "201",
    value: { readiness, manualApproval: true },
  };

  const switchedToB = reconcileCandidateScopedValue(readyA, "202");
  assert.deepEqual(switchedToB, { strategyVersionId: "202", value: null });
  assert.deepEqual(
    reconcileCandidateScopedValue(switchedToB, "201"),
    { strategyVersionId: "201", value: null },
  );

  const disappeared = reconcileCandidateScopedValue(readyA, null);
  assert.deepEqual(disappeared, { strategyVersionId: null, value: null });
  assert.deepEqual(
    reconcileCandidateScopedValue(disappeared, "201"),
    { strategyVersionId: "201", value: null },
  );
});

test("delayed readiness completion is current only for the same candidate ID and epoch", () => {
  const requestA = { strategyVersionId: "201", epoch: 4 };

  assert.equal(candidateIdentityMatches({ strategyVersionId: "201", epoch: 4 }, requestA), true);
  assert.equal(candidateIdentityMatches({ strategyVersionId: "202", epoch: 5 }, requestA), false);
  assert.equal(candidateIdentityMatches({ strategyVersionId: null, epoch: 5 }, requestA), false);
  assert.equal(
    candidateIdentityMatches({ strategyVersionId: "201", epoch: 6 }, requestA),
    false,
    "A to null to A must reject the completion from the earlier A epoch",
  );
});

test("an actionless missing-candidate state never labels itself as stopping", () => {
  const model = deriveDryRunDecision({
    candidate: null,
    dryRunSource: "api",
    manifest: null,
    readiness: null,
    runtimeReason: "missing",
    snapshot: { ...snapshot, status: "UNKNOWN", strategyVersionId: null },
    transient: { kind: "idle" },
  });
  assert.equal(model.action, null);
  assert.equal(inactiveActionLabel(model.state), "暂无可执行动作");
  assert.equal(inactiveActionLabel("STOPPING"), "停止中");
});
