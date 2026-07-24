import assert from "node:assert/strict";
import test from "node:test";

import {
  environmentContractViolations,
  operatorDiagnosticCounts,
  operatorSystemConclusion,
  runtimeStatusReason,
  safetyBoundaryViolations,
  sortOperatorDiagnostics,
} from "../src/pages/operatorDashboardDisplay.ts";

function diagnostic(status, overrides = {}) {
  return {
    name: `${status} check`,
    area: "runtime",
    status,
    source: "backend",
    summary: `${status} summary`,
    path: null,
    exists: null,
    required: true,
    blockedReason: null,
    unavailableReason: null,
    warnings: [],
    ...overrides,
  };
}

function runtimeStatus(status = "READY", overrides = {}) {
  return {
    name: "runtime",
    status,
    summary: "runtime summary",
    source: "backend",
    sourceRef: null,
    lastUpdated: null,
    blockedReason: null,
    unavailableReason: null,
    staleReason: null,
    warnings: [],
    ...overrides,
  };
}

function safety(overrides = {}) {
  return {
    readOnly: true,
    reportsEnvValues: false,
    allowLiveTrading: false,
    allowRealOrders: false,
    allowExchangeConnection: false,
    allowDeployControl: false,
    canStartStopBot: false,
    boundary: "read only",
    ...overrides,
  };
}

function dashboardContracts() {
  const baseStatus = runtimeStatus();
  const runtimeContract = {
    schemaVersion: "v1",
    status: "READY",
    generatedAt: null,
    systemStatus: baseStatus,
    runtimeReadiness: baseStatus,
    researchReadiness: baseStatus,
    dryRunReadiness: baseStatus,
    liveReadiness: baseStatus,
    fallbackStatus: { active: false, status: "READY", reason: null, sources: [] },
    smokeStatus: baseStatus,
    artifactLinks: [],
    blockedReasons: [],
    unavailableReasons: [],
    safety: safety(),
  };
  const operatorStatus = {
    schemaVersion: "v1",
    status: "READY",
    generatedAt: null,
    repoRoot: "/repo",
    checks: [],
    artifacts: [],
    envPresence: [],
    runtimeContract: {
      status: "READY",
      runtimeReadinessStatus: "READY",
      fallbackActive: false,
      smokeStatus: "READY",
      artifactCount: 0,
      blockedReasons: [],
      unavailableReasons: [],
    },
    blockedReasons: [],
    unavailableReasons: [],
    warnings: [],
    safety: safety(),
  };
  return { runtimeContract, operatorStatus };
}

test("diagnostics are ordered fail-closed and all problem classes are counted", () => {
  const checks = [
    diagnostic("READY"),
    diagnostic("STALE"),
    diagnostic("UNAVAILABLE"),
    diagnostic("BLOCKED"),
    diagnostic("FAILED"),
  ];

  assert.deepEqual(
    sortOperatorDiagnostics(checks).map((check) => check.status),
    ["FAILED", "BLOCKED", "UNAVAILABLE", "STALE", "READY"],
  );
  assert.deepEqual(operatorDiagnosticCounts(checks), {
    failed: 1,
    blocked: 1,
    unavailable: 1,
    stale: 1,
    warning: 0,
    otherProblem: 0,
    totalProblems: 4,
  });
});

test("healthy runtime status does not render a fake warning", () => {
  assert.equal(runtimeStatusReason(runtimeStatus("READY")), null);
  assert.equal(
    runtimeStatusReason(runtimeStatus("STALE", { staleReason: "runtime evidence is old" })),
    "runtime evidence is old",
  );
});

test("system conclusion keeps blocked operator evidence visible", () => {
  const { runtimeContract, operatorStatus } = dashboardContracts();
  operatorStatus.status = "BLOCKED";
  operatorStatus.blockedReasons = ["freqtrade binary missing"];

  assert.deepEqual(operatorSystemConclusion(runtimeContract, operatorStatus), {
    status: "BLOCKED",
    label: "系统当前不可验收",
    reason: "freqtrade binary missing",
  });
});

test("unsafe capabilities and ENV value-rendered claims are reported without values", () => {
  const { runtimeContract, operatorStatus } = dashboardContracts();
  runtimeContract.safety.allowLiveTrading = true;
  operatorStatus.safety.reportsEnvValues = true;

  assert.deepEqual(safetyBoundaryViolations(runtimeContract, operatorStatus), [
    "报告声称会展示 ENV 值",
    "允许 Live trading",
  ]);
  assert.deepEqual(
    environmentContractViolations([
      { name: "SAFE_ENV", present: true, required: true, source: "env", valueRendered: false },
      { name: "BAD_ENV", present: true, required: true, source: "env", valueRendered: true },
    ]),
    ["BAD_ENV：报告声称值已渲染"],
  );
});
