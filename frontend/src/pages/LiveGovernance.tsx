import type {
  LiveCandidateApprovalRecordSummary,
  LiveCandidateDeploymentRecordSummary,
  LiveCandidateMonitoringSnapshotSummary,
  LiveCandidateProfileSummary,
} from "../api/types";
import { useMvpData } from "../api/useMvpData";
import { reasonText, statusClassName, summarizeText } from "./backtestDisplay";
import { FallbackNotice } from "./FallbackNotice";
import { EMPTY_TEXT, displayBoolean, displayDataOrigin, displayLoadState, displayStatus, displayValue } from "./uiCopy";

function formatValue(value: number | string | null | undefined): string {
  return displayValue(value);
}

function boolValue(value: boolean): string {
  return displayBoolean(value);
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
  return (
    <span aria-label={`状态：${displayStatus(status)}`} className={`run-status ${phase6StatusClassName(status)}`}>
      <span className="sr-only">状态：</span>
      {displayStatus(status)}
      <span className="sr-only">；</span>
    </span>
  );
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
  return deployment.blockers[0] ?? EMPTY_TEXT;
}

function monitoringReason(snapshot: LiveCandidateMonitoringSnapshotSummary): string {
  return (
    snapshot.blockers[0] ??
    snapshot.unavailableReason ??
    snapshot.staleReason ??
    snapshot.warnings[0] ??
    EMPTY_TEXT
  );
}

function CandidateTable({ profiles }: { profiles: LiveCandidateProfileSummary[] }) {
  return (
    <div className="table-shell">
      <table>
        <thead>
          <tr>
            <th>候选</th>
            <th>状态</th>
            <th>范围</th>
            <th>证据</th>
            <th>风险检查</th>
            <th>原因</th>
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
                  人工复核：{boolValue(profile.canEnterHumanApproval)}
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
                  <span>{EMPTY_TEXT}</span>
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
                  <span>{EMPTY_TEXT}</span>
                )}
              </td>
              <td className="reason-cell">{summarizeText(profileReason(profile))}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {profiles.length === 0 ? <div className="empty-state">暂无实盘候选 profile。</div> : null}
    </div>
  );
}

function ApprovalRecords({ approvals }: { approvals: LiveCandidateApprovalRecordSummary[] }) {
  return (
    <section className="detail-section">
      <div className="section-header">
        <h2>审批记录</h2>
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
                  <dt>预检</dt>
                  <dd>{displayStatus(approval.preflightStatus)}</dd>
                </div>
                <div>
                  <dt>审批数</dt>
                  <dd>
                    {approval.completedApprovals} / {approval.requiredApprovals}
                  </dd>
                </div>
                <div>
                  <dt>下一记录</dt>
                  <dd>{approval.canCreateDeploymentRecord ? "允许创建 deployment record" : "已阻塞"}</dd>
                </div>
                <div>
                  <dt>风险摘要</dt>
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
                <p className="phase6-muted">暂无人工审批决策记录。</p>
              )}
              {approval.blockers.length ? (
                <p className="reason-line warning">{summarizeText(approval.blockers[0])}</p>
              ) : null}
            </li>
          ))}
        </ol>
      ) : (
        <div className="empty-state">暂无审批记录。</div>
      )}
    </section>
  );
}

function DeploymentRecords({ deployments }: { deployments: LiveCandidateDeploymentRecordSummary[] }) {
  return (
    <section className="detail-section">
      <div className="section-header">
        <h2>部署治理记录</h2>
        <span>只读</span>
      </div>
      {deployments.length ? (
        <div className="table-shell">
          <table>
            <thead>
              <tr>
                <th>记录</th>
                <th>状态</th>
                <th>环境</th>
                <th>审批</th>
                <th>Rollback Plan</th>
                <th>人工结果</th>
                <th>原因</th>
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
                    <span>{displayStatus(deployment.approvalStatus)}</span>
                    <span>{displayStatus(deployment.preflightStatus)}</span>
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
                      <span>不可用</span>
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
        <div className="empty-state">暂无部署治理记录。</div>
      )}
    </section>
  );
}

function MonitoringSnapshots({ snapshots }: { snapshots: LiveCandidateMonitoringSnapshotSummary[] }) {
  return (
    <section className="detail-section">
      <div className="section-header">
        <h2>监控摘要</h2>
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
                  <dt>来源</dt>
                  <dd>
                    {snapshot.source.source}: {snapshot.source.ref}
                  </dd>
                </div>
                <div>
                  <dt>部署状态</dt>
                  <dd>{snapshot.deploymentStatus ? displayStatus(snapshot.deploymentStatus) : EMPTY_TEXT}</dd>
                </div>
                <div>
                  <dt>审批状态</dt>
                  <dd>{snapshot.approvalStatus ? displayStatus(snapshot.approvalStatus) : EMPTY_TEXT}</dd>
                </div>
                <div>
                  <dt>预检状态</dt>
                  <dd>{snapshot.preflightStatus ? displayStatus(snapshot.preflightStatus) : EMPTY_TEXT}</dd>
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
        <div className="empty-state">暂无监控快照。</div>
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
        <h1>实盘候选治理</h1>
        <span className="status-pill">{displayLoadState(isLoading, source)}</span>
      </header>
      <FallbackNotice
        context="Live Governance 实盘候选、审批、部署治理、回滚计划和监控快照。"
        error={error}
        isLoading={isLoading}
        source={source}
      />
      <section className="phase6-summary" aria-label="Phase 6 治理摘要">
        <article className="overview-panel">
          <h2>来源</h2>
          <p>{formatValue(governance.sourceRef)}</p>
          <span className="phase6-muted">{governance.readOnly ? "只读" : "未知模式"}</span>
        </article>
        <article className="overview-panel">
          <h2>候选</h2>
          <div className="status-counts" role="list">
            {Object.entries(profileStatuses).map(([status, count], index, entries) => (
              <span
                aria-label={`${displayStatus(status)}：${count} 个`}
                className={`run-status ${phase6StatusClassName(status)}`}
                key={status}
                role="listitem"
              >
                {displayStatus(status)}：{count}
                {index < entries.length - 1 ? <span className="status-text-gap"> </span> : null}
              </span>
            ))}
            {Object.keys(profileStatuses).length === 0 ? <span>暂无候选</span> : null}
          </div>
        </article>
        <article className="overview-panel">
          <h2>部署治理记录</h2>
          <div className="status-counts" role="list">
            {Object.entries(deploymentStatuses).map(([status, count], index, entries) => (
              <span
                aria-label={`${displayStatus(status)}：${count} 个`}
                className={`run-status ${phase6StatusClassName(status)}`}
                key={status}
                role="listitem"
              >
                {displayStatus(status)}：{count}
                {index < entries.length - 1 ? <span className="status-text-gap"> </span> : null}
              </span>
            ))}
            {Object.keys(deploymentStatuses).length === 0 ? <span>暂无记录</span> : null}
          </div>
        </article>
        <article className="overview-panel">
          <h2>阻塞 / 告警</h2>
          <p>
            {blockedProfiles.length} 个候选已阻塞，{alertCount} 条告警摘要。
          </p>
        </article>
      </section>
      <section className="detail-section">
        <div className="section-header">
          <h2>候选 Profile</h2>
          <span>{governance.profiles.length}</span>
        </div>
        <CandidateTable profiles={governance.profiles} />
      </section>
      <ApprovalRecords approvals={governance.approvals} />
      <DeploymentRecords deployments={governance.deployments} />
      <MonitoringSnapshots snapshots={governance.monitoringSnapshots} />
      <section className="detail-section">
        <div className="section-header">
          <h2>安全状态</h2>
          <span>治理</span>
        </div>
        <dl className="detail-list">
          <div>
            <dt>边界</dt>
            <dd>{governance.safetyBoundary}</dd>
          </div>
          <div>
            <dt>执行控制</dt>
            <dd>不可用</dd>
          </div>
          <div>
            <dt>密钥值</dt>
            <dd>不渲染</dd>
          </div>
          <div>
            <dt>数据来源</dt>
            <dd>{displayDataOrigin(source)}</dd>
          </div>
        </dl>
      </section>
    </section>
  );
}
