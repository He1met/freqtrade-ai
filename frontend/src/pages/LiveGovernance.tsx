import type {
  LiveCandidateApprovalRecordSummary,
  LiveCandidateDeploymentRecordSummary,
  LiveCandidateMonitoringSnapshotSummary,
  LiveCandidateProfileSummary,
} from "../api/types";
import { useMvpData } from "../api/useMvpData";
import { reasonText, statusClassName, summarizeText } from "./backtestDisplay";

function formatValue(value: number | string | null | undefined): string {
  return value === null || value === undefined || value === "" ? "none" : String(value);
}

function boolValue(value: boolean): string {
  return value ? "yes" : "no";
}

function phase6StatusClassName(status: string): string {
  const normalized = status.toLowerCase();
  if (normalized.includes("approved") || normalized === "pass" || normalized === "ok" || normalized === "planned") {
    return "status-success";
  }
  if (normalized.includes("blocked") || normalized === "unavailable" || normalized === "stale") {
    return "status-blocked";
  }
  if (normalized.includes("failed") || normalized.includes("rejected") || normalized === "critical") {
    return "status-failed";
  }
  return statusClassName(status);
}

function statusPill(status: string) {
  return <span className={`run-status ${phase6StatusClassName(status)}`}>{status}</span>;
}

function countByStatus<T>(items: T[], getStatus: (item: T) => string): Record<string, number> {
  return items.reduce<Record<string, number>>((counts, item) => {
    const status = getStatus(item);
    counts[status] = (counts[status] ?? 0) + 1;
    return counts;
  }, {});
}

function profileReason(profile: LiveCandidateProfileSummary): string {
  return reasonText(profile.blockers[0] ?? null, null, profile.warnings[0] ?? null);
}

function deploymentReason(deployment: LiveCandidateDeploymentRecordSummary): string {
  return deployment.blockers[0] ?? "none";
}

function monitoringReason(snapshot: LiveCandidateMonitoringSnapshotSummary): string {
  return (
    snapshot.blockers[0] ??
    snapshot.unavailableReason ??
    snapshot.staleReason ??
    snapshot.warnings[0] ??
    "none"
  );
}

function CandidateTable({ profiles }: { profiles: LiveCandidateProfileSummary[] }) {
  return (
    <div className="table-shell">
      <table>
        <thead>
          <tr>
            <th>Candidate</th>
            <th>Status</th>
            <th>Scope</th>
            <th>Evidence</th>
            <th>Risk Checks</th>
            <th>Reason</th>
          </tr>
        </thead>
        <tbody>
          {profiles.map((profile) => (
            <tr key={profile.id}>
              <td>
                <strong>{profile.profileName}</strong>
                <span className="secondary-cell">{formatValue(profile.profileHash)}</span>
              </td>
              <td>
                {statusPill(profile.status)}
                <span className="secondary-cell">
                  human review: {boolValue(profile.canEnterHumanApproval)}
                </span>
              </td>
              <td className="artifact-cell">
                <span>{profile.strategyName}</span>
                <span>{profile.pair}</span>
                <span>{profile.timeframe}</span>
              </td>
              <td className="artifact-cell">
                {profile.evidenceRefs.length ? (
                  profile.evidenceRefs.map((ref) => <span key={`${profile.id}:${ref}`}>{ref}</span>)
                ) : (
                  <span>none</span>
                )}
              </td>
              <td className="comparison-cell">
                {profile.riskChecks.length ? (
                  profile.riskChecks.map((check) => (
                    <span key={`${profile.id}:${check.name}`}>
                      <strong>{check.name}</strong>
                      {statusPill(check.status)}
                      <em>{summarizeText(check.summary)}</em>
                    </span>
                  ))
                ) : (
                  <span>none</span>
                )}
              </td>
              <td className="reason-cell">{summarizeText(profileReason(profile))}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {profiles.length === 0 ? <div className="empty-state">No live-candidate profiles found.</div> : null}
    </div>
  );
}

function ApprovalRecords({ approvals }: { approvals: LiveCandidateApprovalRecordSummary[] }) {
  return (
    <section className="detail-section">
      <div className="section-header">
        <h2>Approval Records</h2>
        <span>{approvals.length}</span>
      </div>
      {approvals.length ? (
        <ol className="phase6-record-list">
          {approvals.map((approval) => (
            <li key={approval.recordId}>
              <div className="phase6-record-heading">
                <strong>{approval.recordId}</strong>
                {statusPill(approval.status)}
              </div>
              <dl className="compact-detail-list">
                <div>
                  <dt>Profile</dt>
                  <dd>{approval.profileName}</dd>
                </div>
                <div>
                  <dt>Preflight</dt>
                  <dd>{approval.preflightStatus}</dd>
                </div>
                <div>
                  <dt>Approvals</dt>
                  <dd>
                    {approval.completedApprovals} / {approval.requiredApprovals}
                  </dd>
                </div>
                <div>
                  <dt>Next record</dt>
                  <dd>{approval.canCreateDeploymentRecord ? "deployment-record allowed" : "blocked"}</dd>
                </div>
                <div>
                  <dt>Risk summary</dt>
                  <dd>{formatValue(approval.riskSummaryRef)}</dd>
                </div>
              </dl>
              {approval.decisions.length ? (
                <div className="phase6-decision-grid">
                  {approval.decisions.map((decision) => (
                    <div key={`${approval.recordId}:${decision.actorName}:${decision.decidedAt}`}>
                      {statusPill(decision.decision)}
                      <strong>{decision.actorName}</strong>
                      <span>{decision.actorRole}</span>
                      <span>{formatValue(decision.decidedAt)}</span>
                      <p>{summarizeText(decision.basis)}</p>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="phase6-muted">No manual approval decisions are recorded.</p>
              )}
              {approval.blockers.length ? (
                <p className="reason-line warning">{summarizeText(approval.blockers[0])}</p>
              ) : null}
            </li>
          ))}
        </ol>
      ) : (
        <div className="empty-state">No approval records found.</div>
      )}
    </section>
  );
}

function DeploymentRecords({ deployments }: { deployments: LiveCandidateDeploymentRecordSummary[] }) {
  return (
    <section className="detail-section">
      <div className="section-header">
        <h2>Deployment Records</h2>
        <span>read-only</span>
      </div>
      {deployments.length ? (
        <div className="table-shell">
          <table>
            <thead>
              <tr>
                <th>Record</th>
                <th>Status</th>
                <th>Environment</th>
                <th>Approval</th>
                <th>Rollback Plan</th>
                <th>Manual Result</th>
                <th>Reason</th>
              </tr>
            </thead>
            <tbody>
              {deployments.map((deployment) => (
                <tr key={deployment.recordId}>
                  <td>
                    <strong>{deployment.recordId}</strong>
                    <span className="secondary-cell">{deployment.profileName}</span>
                  </td>
                  <td>{statusPill(deployment.status)}</td>
                  <td className="artifact-cell">
                    <span>{deployment.plannedEnvironment}</span>
                    <span>{formatValue(deployment.plannedAt)}</span>
                    <span>{deployment.plannedBy}</span>
                  </td>
                  <td className="artifact-cell">
                    <span>{deployment.approvalStatus}</span>
                    <span>{deployment.preflightStatus}</span>
                  </td>
                  <td className="comparison-cell">
                    {deployment.rollbackPlan ? (
                      <>
                        <span>
                          <strong>{deployment.rollbackPlan.planId}</strong>
                          {summarizeText(deployment.rollbackPlan.summary)}
                        </span>
                        {deployment.rollbackPlan.steps.map((step) => (
                          <span key={`${deployment.recordId}:${step.order}`}>
                            <strong>
                              {step.order}. {step.title}
                            </strong>
                            {summarizeText(step.verification)}
                          </span>
                        ))}
                      </>
                    ) : (
                      <span>unavailable</span>
                    )}
                  </td>
                  <td className="artifact-cell">
                    <span>{formatValue(deployment.resultStatus)}</span>
                    <span>{formatValue(deployment.resultRecordedAt)}</span>
                  </td>
                  <td className="reason-cell">{summarizeText(deploymentReason(deployment))}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="empty-state">No deployment records found.</div>
      )}
    </section>
  );
}

function MonitoringSnapshots({ snapshots }: { snapshots: LiveCandidateMonitoringSnapshotSummary[] }) {
  return (
    <section className="detail-section">
      <div className="section-header">
        <h2>Monitoring Summary</h2>
        <span>{snapshots.length}</span>
      </div>
      {snapshots.length ? (
        <ol className="event-list">
          {snapshots.map((snapshot) => (
            <li key={snapshot.snapshotId}>
              <div className="event-heading">
                {statusPill(snapshot.status)}
                <strong>{formatValue(snapshot.profileName)}</strong>
                <span>{formatValue(snapshot.updatedAt ?? snapshot.source.collectedAt)}</span>
              </div>
              <dl className="compact-detail-list">
                <div>
                  <dt>Source</dt>
                  <dd>
                    {snapshot.source.source}: {snapshot.source.ref}
                  </dd>
                </div>
                <div>
                  <dt>Deployment</dt>
                  <dd>{formatValue(snapshot.deploymentStatus)}</dd>
                </div>
                <div>
                  <dt>Approval</dt>
                  <dd>{formatValue(snapshot.approvalStatus)}</dd>
                </div>
                <div>
                  <dt>Preflight</dt>
                  <dd>{formatValue(snapshot.preflightStatus)}</dd>
                </div>
              </dl>
              <p>{summarizeText(monitoringReason(snapshot))}</p>
              {snapshot.alerts.length ? (
                <div className="phase6-alert-grid">
                  {snapshot.alerts.map((alert) => (
                    <div key={`${snapshot.snapshotId}:${alert.alertId}`}>
                      {statusPill(alert.severity)}
                      <strong>{alert.alertId}</strong>
                      <span>{summarizeText(alert.message)}</span>
                      <span>{formatValue(alert.evidenceRef)}</span>
                    </div>
                  ))}
                </div>
              ) : null}
            </li>
          ))}
        </ol>
      ) : (
        <div className="empty-state">No monitoring snapshots found.</div>
      )}
    </section>
  );
}

export function LiveGovernance() {
  const { data, source, isLoading, error } = useMvpData();
  const governance = data.liveCandidates;
  const profileStatuses = countByStatus(governance.profiles, (profile) => profile.status);
  const deploymentStatuses = countByStatus(governance.deployments, (deployment) => deployment.status);
  const blockedProfiles = governance.profiles.filter((profile) => profile.blockers.length > 0);
  const alertCount = governance.monitoringSnapshots.reduce(
    (total, snapshot) => total + snapshot.alerts.length,
    0,
  );

  return (
    <section className="page">
      <header className="page-header">
        <h1>Live Governance</h1>
        <span className="status-pill">{isLoading ? "Loading" : source}</span>
      </header>
      {error ? <div className="notice">Using fallback data: {error}</div> : null}
      {!isLoading && source === "fallback" && !error ? (
        <div className="notice">Backend API unavailable; showing controlled fallback Phase 6 governance data.</div>
      ) : null}
      <section className="phase6-summary" aria-label="Phase 6 governance summary">
        <article className="overview-panel">
          <h2>Source</h2>
          <p>{formatValue(governance.sourceRef)}</p>
          <span className="phase6-muted">{governance.readOnly ? "read-only" : "unknown mode"}</span>
        </article>
        <article className="overview-panel">
          <h2>Candidates</h2>
          <div className="status-counts">
            {Object.entries(profileStatuses).map(([status, count]) => (
              <span className={`run-status ${phase6StatusClassName(status)}`} key={status}>
                {status}: {count}
              </span>
            ))}
            {Object.keys(profileStatuses).length === 0 ? <span>No candidates</span> : null}
          </div>
        </article>
        <article className="overview-panel">
          <h2>Deployment Records</h2>
          <div className="status-counts">
            {Object.entries(deploymentStatuses).map(([status, count]) => (
              <span className={`run-status ${phase6StatusClassName(status)}`} key={status}>
                {status}: {count}
              </span>
            ))}
            {Object.keys(deploymentStatuses).length === 0 ? <span>No records</span> : null}
          </div>
        </article>
        <article className="overview-panel">
          <h2>Blocked / Alerts</h2>
          <p>
            {blockedProfiles.length} blocked candidate(s), {alertCount} alert summary item(s).
          </p>
        </article>
      </section>
      <section className="detail-section">
        <div className="section-header">
          <h2>Candidate Profiles</h2>
          <span>{governance.profiles.length}</span>
        </div>
        <CandidateTable profiles={governance.profiles} />
      </section>
      <ApprovalRecords approvals={governance.approvals} />
      <DeploymentRecords deployments={governance.deployments} />
      <MonitoringSnapshots snapshots={governance.monitoringSnapshots} />
      <section className="detail-section">
        <div className="section-header">
          <h2>Safety State</h2>
          <span>governance</span>
        </div>
        <dl className="detail-list">
          <div>
            <dt>Boundary</dt>
            <dd>{governance.safetyBoundary}</dd>
          </div>
          <div>
            <dt>Execution controls</dt>
            <dd>unavailable</dd>
          </div>
          <div>
            <dt>Credential values</dt>
            <dd>not rendered</dd>
          </div>
          <div>
            <dt>Data source</dt>
            <dd>{source === "fallback" ? "controlled fixture" : "backend API"}</dd>
          </div>
        </dl>
      </section>
    </section>
  );
}
