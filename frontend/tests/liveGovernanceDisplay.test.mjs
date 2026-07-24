import assert from "node:assert/strict";
import test from "node:test";

import {
  approvalIsComplete,
  buildLiveGovernanceOverview,
  candidateReviewState,
  governanceBlockers,
} from "../src/pages/liveGovernanceDisplay.ts";

function profile(overrides = {}) {
  return {
    id: "candidate-1",
    profileName: "candidate-1",
    strategyName: "StrategyA",
    pair: "BTC/USDT:USDT",
    timeframe: "1h",
    status: "APPROVED_FOR_REVIEW",
    profileHash: "hash",
    canEnterHumanApproval: true,
    evidenceRefs: [],
    blockers: [],
    warnings: [],
    riskChecks: [],
    sourceRef: null,
    updatedAt: null,
    ...overrides,
  };
}

function approval(overrides = {}) {
  return {
    recordId: "approval-1",
    profileName: "candidate-1",
    profileHash: "hash",
    status: "APPROVED_FOR_DEPLOYMENT_RECORD",
    preflightStatus: "APPROVED_FOR_REVIEW",
    requiredApprovals: 2,
    completedApprovals: 2,
    canCreateDeploymentRecord: true,
    submittedBy: "operator",
    submittedAt: null,
    riskSummaryRef: null,
    decisions: [],
    blockers: [],
    ...overrides,
  };
}

function governance(overrides = {}) {
  return {
    sourceRef: null,
    readOnly: true,
    safetyBoundary: "Governance only; no execution authority.",
    profiles: [],
    approvals: [],
    deployments: [],
    monitoringSnapshots: [],
    ...overrides,
  };
}

test("only an unblocked approved candidate can enter human review", () => {
  assert.equal(candidateReviewState(profile()), "REVIEWABLE");
  assert.equal(
    candidateReviewState(profile({ blockers: ["missing evidence"] })),
    "BLOCKED",
  );
  assert.equal(
    candidateReviewState(profile({ canEnterHumanApproval: false })),
    "BLOCKED",
  );
  assert.equal(candidateReviewState(profile({ status: "FAILED" })), "FAILED");
});

test("approval count alone never grants a deployment governance record", () => {
  assert.equal(approvalIsComplete(approval()), true);
  assert.equal(
    approvalIsComplete(approval({ canCreateDeploymentRecord: false })),
    false,
  );
  assert.equal(
    approvalIsComplete(approval({ blockers: ["preflight blocked"] })),
    false,
  );
  assert.equal(
    approvalIsComplete(approval({ completedApprovals: 1 })),
    false,
  );
});

test("governance overview stays fail-closed even when readonly metadata is false", () => {
  const overview = buildLiveGovernanceOverview(governance({
    readOnly: false,
    profiles: [profile()],
    approvals: [approval()],
  }));

  assert.equal(overview.readOnlyVerified, false);
  assert.equal(overview.executionControlAvailable, false);
  assert.equal(overview.reviewableCandidateCount, 1);
  assert.equal(overview.approvalCompleteCount, 1);
});

test("blockers include unavailable and stale monitoring evidence without duplicates", () => {
  const snapshot = {
    snapshotId: "snapshot-1",
    status: "UNAVAILABLE",
    profileName: null,
    deploymentRecordId: null,
    deploymentStatus: null,
    approvalStatus: null,
    preflightStatus: null,
    source: { source: "controlled-local-json", ref: "report.json", collectedAt: null },
    alerts: [],
    blockers: ["missing report"],
    warnings: [],
    unavailableReason: "missing report",
    staleReason: "snapshot stale",
    safetyBoundary: "read only",
    updatedAt: null,
  };
  const blockers = governanceBlockers(governance({
    profiles: [profile({ blockers: ["missing report"] })],
    monitoringSnapshots: [snapshot],
  }));

  assert.deepEqual(blockers, ["missing report", "snapshot stale"]);
});
