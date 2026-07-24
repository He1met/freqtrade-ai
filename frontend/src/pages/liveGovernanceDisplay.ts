import type {
  LiveCandidateApprovalRecordSummary,
  LiveCandidateGovernanceSummary,
  LiveCandidateProfileSummary,
} from "../api/types";

export type CandidateReviewState = "REVIEWABLE" | "BLOCKED" | "FAILED" | "PENDING";

function normalized(status: string): string {
  return status.trim().toLowerCase();
}

function isBlockedStatus(status: string): boolean {
  const value = normalized(status);
  return (
    value.includes("blocked") ||
    value.includes("failed") ||
    value.includes("rejected") ||
    value === "unavailable" ||
    value === "stale"
  );
}

export function candidateReviewState(
  profile: LiveCandidateProfileSummary,
): CandidateReviewState {
  const status = normalized(profile.status);
  if (status.includes("failed")) {
    return "FAILED";
  }
  if (
    profile.blockers.length > 0 ||
    isBlockedStatus(profile.status) ||
    !profile.canEnterHumanApproval
  ) {
    return "BLOCKED";
  }
  if (status === "approved_for_review") {
    return "REVIEWABLE";
  }
  return "PENDING";
}

export function approvalIsComplete(
  approval: LiveCandidateApprovalRecordSummary,
): boolean {
  return Boolean(
    approval.canCreateDeploymentRecord &&
      approval.blockers.length === 0 &&
      approval.requiredApprovals > 0 &&
      approval.completedApprovals >= approval.requiredApprovals &&
      normalized(approval.status).includes("approved"),
  );
}

export function governanceBlockers(
  governance: LiveCandidateGovernanceSummary,
): string[] {
  const values = [
    ...governance.profiles.flatMap((profile) => profile.blockers),
    ...governance.approvals.flatMap((approval) => approval.blockers),
    ...governance.deployments.flatMap((deployment) => deployment.blockers),
    ...governance.monitoringSnapshots.flatMap((snapshot) => [
      ...snapshot.blockers,
      ...(snapshot.unavailableReason ? [snapshot.unavailableReason] : []),
      ...(snapshot.staleReason ? [snapshot.staleReason] : []),
    ]),
  ].map((value) => value.trim()).filter(Boolean);

  return Array.from(new Set(values));
}

export function buildLiveGovernanceOverview(
  governance: LiveCandidateGovernanceSummary,
) {
  const candidateStates = governance.profiles.map(candidateReviewState);
  const blockers = governanceBlockers(governance);
  const approvalCompleteCount = governance.approvals.filter(approvalIsComplete).length;
  const alertCount = governance.monitoringSnapshots.reduce(
    (total, snapshot) => total + snapshot.alerts.length,
    0,
  );
  const degradedSnapshotCount = governance.monitoringSnapshots.filter(
    (snapshot) => isBlockedStatus(snapshot.status) || normalized(snapshot.status) === "warning",
  ).length;

  return {
    candidateCount: governance.profiles.length,
    reviewableCandidateCount: candidateStates.filter((state) => state === "REVIEWABLE").length,
    blockedCandidateCount: candidateStates.filter(
      (state) => state === "BLOCKED" || state === "FAILED",
    ).length,
    approvalCount: governance.approvals.length,
    approvalCompleteCount,
    deploymentRecordCount: governance.deployments.length,
    blockerCount: blockers.length,
    blockers,
    alertCount,
    degradedSnapshotCount,
    readOnlyVerified: governance.readOnly === true,
    executionControlAvailable: false as const,
  };
}
