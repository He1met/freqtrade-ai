import type {
  LiveCandidateApprovalRecordSummary,
  LiveCandidateDeploymentRecordSummary,
  LiveCandidateMonitoringSnapshotSummary,
  LiveCandidateProfileSummary,
} from "../api/types";
import { combineDataSources } from "../api/sourceState";
import { useMvpData } from "../api/useMvpData";
import {
  CompactText,
  CopyableValue,
  EmptyState,
  ExpandableText,
  PageHeader,
  StatusBadge,
} from "../components/DisplayPrimitives";
import "../styles/live-governance.css";
import { FallbackNotice } from "./FallbackNotice";
import {
  approvalIsComplete,
  buildLiveGovernanceOverview,
  candidateReviewState,
} from "./liveGovernanceDisplay";
import {
  EMPTY_TEXT,
  displayBoolean,
  displayDataOrigin,
  displayDateTime,
  displayLoadState,
  displayStatus,
  displayValue,
} from "./uiCopy";

function firstReason(values: Array<string | null | undefined>): string {
  return values.find((value): value is string => Boolean(value?.trim())) ?? EMPTY_TEXT;
}

function ReferenceList({
  emptyText,
  label,
  values,
}: {
  emptyText: string;
  label: string;
  values: string[];
}) {
  if (values.length === 0) {
    return <span>{emptyText}</span>;
  }
  return (
    <ul className="live-reference-list">
      {values.map((value) => (
        <li key={value}>
          <CopyableValue label={label} value={value} />
        </li>
      ))}
    </ul>
  );
}

function CandidateCards({ profiles }: { profiles: LiveCandidateProfileSummary[] }) {
  return (
    <div className="live-card-list">
      {profiles.map((profile) => {
        const reviewState = candidateReviewState(profile);
        const reason = firstReason([profile.blockers[0], profile.warnings[0]]);
        return (
          <article
            className={`live-card ${reviewState === "REVIEWABLE" ? "" : "live-card-blocked"}`}
            key={profile.id}
          >
            <header className="live-card-header">
              <div className="live-card-title">
                <strong>{profile.profileName}</strong>
                <span>{profile.strategyName}</span>
              </div>
              <StatusBadge showRaw status={profile.status} />
            </header>
            <dl className="live-primary-grid">
              <div>
                <dt>交易对</dt>
                <dd>{profile.pair}</dd>
              </div>
              <div>
                <dt>Timeframe</dt>
                <dd>{profile.timeframe}</dd>
              </div>
              <div>
                <dt>人工复核</dt>
                <dd>{reviewState === "REVIEWABLE" ? "允许进入复核" : "不可进入"}</dd>
              </div>
              <div>
                <dt>风险检查</dt>
                <dd>{profile.riskChecks.length} 项</dd>
              </div>
            </dl>
            {reason !== EMPTY_TEXT ? <p className="live-card-reason">{reason}</p> : null}
            <details className="live-card-details">
              <summary>查看候选证据与风险详情</summary>
              <div className="live-detail-content">
                <dl className="live-detail-grid">
                  <div>
                    <dt>候选 ID</dt>
                    <dd><CopyableValue label="候选 ID" value={profile.id} /></dd>
                  </div>
                  <div>
                    <dt>Profile Hash</dt>
                    <dd><CopyableValue label="Profile Hash" value={profile.profileHash} /></dd>
                  </div>
                  <div>
                    <dt>来源引用</dt>
                    <dd><CopyableValue label="来源引用" value={profile.sourceRef} /></dd>
                  </div>
                  <div>
                    <dt>更新时间</dt>
                    <dd>{displayDateTime(profile.updatedAt)}</dd>
                  </div>
                </dl>
                <section className="live-detail-subsection">
                  <h3>证据引用</h3>
                  <ReferenceList
                    emptyText="暂无证据引用，不能进入验收。"
                    label="证据引用"
                    values={profile.evidenceRefs}
                  />
                </section>
                <section className="live-detail-subsection">
                  <h3>风险检查</h3>
                  {profile.riskChecks.length > 0 ? (
                    <ul className="live-check-list">
                      {profile.riskChecks.map((check) => (
                        <li key={`${profile.id}:${check.name}`}>
                          <div className="live-inline-heading">
                            <strong>{check.name}</strong>
                            <StatusBadge showRaw status={check.status} />
                          </div>
                          <ExpandableText summary="查看检查结论" value={check.summary} />
                          {check.blockedReason ? (
                            <ExpandableText summary="查看阻塞原因" value={check.blockedReason} />
                          ) : null}
                          <CopyableValue label="风险证据引用" value={check.evidenceRef} />
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <span>暂无风险检查，不能视为通过。</span>
                  )}
                </section>
                {profile.warnings.length > 0 ? (
                  <ExpandableText summary="查看全部警告" value={profile.warnings.join("\n")} />
                ) : null}
              </div>
            </details>
          </article>
        );
      })}
    </div>
  );
}

function ApprovalRecords({
  approvals,
}: {
  approvals: LiveCandidateApprovalRecordSummary[];
}) {
  return (
    <div className="live-card-list">
      {approvals.map((approval) => {
        const complete = approvalIsComplete(approval);
        const reason = firstReason(approval.blockers);
        return (
          <article className={`live-card ${complete ? "" : "live-card-blocked"}`} key={approval.recordId}>
            <header className="live-card-header">
              <div className="live-card-title">
                <strong>{approval.profileName}</strong>
                <span>审批记录 {approval.recordId}</span>
              </div>
              <StatusBadge showRaw status={approval.status} />
            </header>
            <dl className="live-primary-grid">
              <div>
                <dt>审批进度</dt>
                <dd>{approval.completedApprovals} / {approval.requiredApprovals}</dd>
              </div>
              <div>
                <dt>预检</dt>
                <dd>{displayStatus(approval.preflightStatus)}</dd>
              </div>
              <div>
                <dt>治理记录</dt>
                <dd>{complete ? "允许创建" : "不可创建"}</dd>
              </div>
              <div>
                <dt>执行权限</dt>
                <dd>未授予</dd>
              </div>
            </dl>
            {reason !== EMPTY_TEXT ? <p className="live-card-reason">{reason}</p> : null}
            <details className="live-card-details">
              <summary>查看审批依据与决策记录</summary>
              <div className="live-detail-content">
                <dl className="live-detail-grid">
                  <div>
                    <dt>审批记录 ID</dt>
                    <dd><CopyableValue label="审批记录 ID" value={approval.recordId} /></dd>
                  </div>
                  <div>
                    <dt>Profile Hash</dt>
                    <dd><CopyableValue label="Profile Hash" value={approval.profileHash} /></dd>
                  </div>
                  <div>
                    <dt>提交人</dt>
                    <dd>{approval.submittedBy}</dd>
                  </div>
                  <div>
                    <dt>提交时间</dt>
                    <dd>{displayDateTime(approval.submittedAt)}</dd>
                  </div>
                  <div>
                    <dt>风险摘要引用</dt>
                    <dd><CopyableValue label="风险摘要引用" value={approval.riskSummaryRef} /></dd>
                  </div>
                  <div>
                    <dt>允许创建部署治理记录</dt>
                    <dd>{displayBoolean(approval.canCreateDeploymentRecord)}</dd>
                  </div>
                </dl>
                <section className="live-detail-subsection">
                  <h3>人工决策</h3>
                  {approval.decisions.length > 0 ? (
                    <ul className="live-decision-list">
                      {approval.decisions.map((decision) => (
                        <li key={`${approval.recordId}:${decision.actorName}:${decision.decidedAt}`}>
                          <div className="live-inline-heading">
                            <strong>{decision.actorName} · {decision.actorRole}</strong>
                            <StatusBadge showRaw status={decision.decision} />
                          </div>
                          <span>{displayDateTime(decision.decidedAt)}</span>
                          <ExpandableText summary="查看决策依据" value={decision.basis} />
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <span>暂无人工审批决策。</span>
                  )}
                </section>
              </div>
            </details>
          </article>
        );
      })}
    </div>
  );
}

function DeploymentRecords({
  deployments,
}: {
  deployments: LiveCandidateDeploymentRecordSummary[];
}) {
  return (
    <div className="live-card-list">
      {deployments.map((deployment) => {
        const reason = firstReason(deployment.blockers);
        return (
          <article className={`live-card ${reason === EMPTY_TEXT ? "" : "live-card-blocked"}`} key={deployment.recordId}>
            <header className="live-card-header">
              <div className="live-card-title">
                <strong>{deployment.profileName}</strong>
                <span>部署治理记录，不代表已执行部署</span>
              </div>
              <StatusBadge showRaw status={deployment.status} />
            </header>
            <dl className="live-primary-grid">
              <div>
                <dt>计划环境</dt>
                <dd>{deployment.plannedEnvironment}</dd>
              </div>
              <div>
                <dt>审批状态</dt>
                <dd>{displayStatus(deployment.approvalStatus)}</dd>
              </div>
              <div>
                <dt>预检状态</dt>
                <dd>{displayStatus(deployment.preflightStatus)}</dd>
              </div>
              <div>
                <dt>回滚方案</dt>
                <dd>{deployment.rollbackPlan ? "已记录" : "缺失"}</dd>
              </div>
            </dl>
            {reason !== EMPTY_TEXT ? <p className="live-card-reason">{reason}</p> : null}
            <details className="live-card-details">
              <summary>查看治理记录与回滚方案</summary>
              <div className="live-detail-content">
                <dl className="live-detail-grid">
                  <div>
                    <dt>治理记录 ID</dt>
                    <dd><CopyableValue label="治理记录 ID" value={deployment.recordId} /></dd>
                  </div>
                  <div>
                    <dt>规划人</dt>
                    <dd>{deployment.plannedBy}</dd>
                  </div>
                  <div>
                    <dt>规划时间</dt>
                    <dd>{displayDateTime(deployment.plannedAt)}</dd>
                  </div>
                  <div>
                    <dt>人工结果</dt>
                    <dd>{displayValue(deployment.resultStatus)}</dd>
                  </div>
                  <div>
                    <dt>结果记录时间</dt>
                    <dd>{displayDateTime(deployment.resultRecordedAt)}</dd>
                  </div>
                </dl>
                <section className="live-detail-subsection">
                  <h3>回滚方案</h3>
                  {deployment.rollbackPlan ? (
                    <>
                      <CopyableValue label="回滚方案 ID" value={deployment.rollbackPlan.planId} />
                      <ExpandableText summary="查看方案摘要" value={deployment.rollbackPlan.summary} />
                      <ul className="live-rollback-list">
                        {deployment.rollbackPlan.steps.map((step) => (
                          <li key={`${deployment.recordId}:${step.order}`}>
                            <div className="live-inline-heading">
                              <strong>{step.order}. {step.title}</strong>
                              <span>{step.owner}</span>
                            </div>
                            <ExpandableText summary="查看验证要求" value={step.verification} />
                          </li>
                        ))}
                      </ul>
                      <ReferenceList
                        emptyText="暂无回滚证据引用。"
                        label="回滚证据引用"
                        values={deployment.rollbackPlan.evidenceRefs}
                      />
                    </>
                  ) : (
                    <span>未记录回滚方案，不能进入后续治理阶段。</span>
                  )}
                </section>
              </div>
            </details>
          </article>
        );
      })}
    </div>
  );
}

function MonitoringSnapshots({
  snapshots,
}: {
  snapshots: LiveCandidateMonitoringSnapshotSummary[];
}) {
  return (
    <div className="live-card-list">
      {snapshots.map((snapshot) => {
        const reason = firstReason([
          snapshot.blockers[0],
          snapshot.unavailableReason,
          snapshot.staleReason,
          snapshot.warnings[0],
        ]);
        return (
          <article
            className={`live-card ${reason === EMPTY_TEXT && snapshot.status === "OK" ? "" : "live-card-blocked"}`}
            key={snapshot.snapshotId}
          >
            <header className="live-card-header">
              <div className="live-card-title">
                <strong>{snapshot.profileName ?? "未关联候选"}</strong>
                <span>{displayDateTime(snapshot.updatedAt ?? snapshot.source.collectedAt)}</span>
              </div>
              <StatusBadge showRaw status={snapshot.status} />
            </header>
            <dl className="live-primary-grid">
              <div>
                <dt>监控来源</dt>
                <dd>{snapshot.source.source}</dd>
              </div>
              <div>
                <dt>部署状态</dt>
                <dd>{displayStatus(snapshot.deploymentStatus)}</dd>
              </div>
              <div>
                <dt>预检状态</dt>
                <dd>{displayStatus(snapshot.preflightStatus)}</dd>
              </div>
              <div>
                <dt>告警</dt>
                <dd>{snapshot.alerts.length} 条</dd>
              </div>
            </dl>
            {reason !== EMPTY_TEXT ? <p className="live-card-reason">{reason}</p> : null}
            <details className="live-card-details">
              <summary>查看监控来源与告警详情</summary>
              <div className="live-detail-content">
                <dl className="live-detail-grid">
                  <div>
                    <dt>快照 ID</dt>
                    <dd><CopyableValue label="监控快照 ID" value={snapshot.snapshotId} /></dd>
                  </div>
                  <div>
                    <dt>部署治理记录 ID</dt>
                    <dd><CopyableValue label="部署治理记录 ID" value={snapshot.deploymentRecordId} /></dd>
                  </div>
                  <div>
                    <dt>监控来源引用</dt>
                    <dd><CopyableValue label="监控来源引用" value={snapshot.source.ref} /></dd>
                  </div>
                  <div>
                    <dt>审批状态</dt>
                    <dd>{displayStatus(snapshot.approvalStatus)}</dd>
                  </div>
                </dl>
                <ExpandableText summary="查看快照安全边界" value={snapshot.safetyBoundary} />
                <section className="live-detail-subsection">
                  <h3>告警记录</h3>
                  {snapshot.alerts.length > 0 ? (
                    <ul className="live-alert-list">
                      {snapshot.alerts.map((alert) => (
                        <li key={`${snapshot.snapshotId}:${alert.alertId}`}>
                          <div className="live-inline-heading">
                            <strong>{alert.alertId}</strong>
                            <StatusBadge showRaw status={alert.severity} />
                          </div>
                          <ExpandableText summary="查看告警内容" value={alert.message} />
                          <CopyableValue label="告警证据引用" value={alert.evidenceRef} />
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <span>暂无告警记录。</span>
                  )}
                </section>
              </div>
            </details>
          </article>
        );
      })}
    </div>
  );
}

export function LiveGovernance() {
  const { data, sources, isLoading, error } = useMvpData();
  const source = combineDataSources(sources, ["liveCandidates"]);
  const governance = data.liveCandidates;
  const overview = buildLiveGovernanceOverview(governance);
  const hasRecords =
    governance.profiles.length +
      governance.approvals.length +
      governance.deployments.length +
      governance.monitoringSnapshots.length >
    0;

  return (
    <section className="page live-governance-page">
      <PageHeader
        description="只读核对候选资格、人工审批、阻断原因和治理证据。"
        eyebrow="治理与安全"
        status={<StatusBadge label={displayLoadState(isLoading, source)} status={isLoading ? "running" : source} />}
        title="实盘候选治理"
      />
      <FallbackNotice
        context="Live Governance 实盘候选、审批、部署治理、回滚计划和监控快照。"
        error={error}
        isLoading={isLoading}
        source={source}
      />

      <aside className="live-safety-banner" aria-label="实盘治理安全边界">
        <span aria-hidden="true">!</span>
        <div>
          <strong>
            {overview.readOnlyVerified ? "只读治理已确认" : "只读状态无法确认，继续禁止执行"}
          </strong>
          <p>
            本页不启动交易、不连接交易所、不提交真实订单，也不执行生产部署。审批完成只允许形成治理记录，不授予执行权限。
          </p>
        </div>
      </aside>

      {!isLoading ? (
        <section className="live-overview-grid" aria-label="实盘候选治理摘要">
          <article className="live-overview-card">
            <span>候选</span>
            <strong>{overview.candidateCount}</strong>
            <p>{overview.reviewableCandidateCount} 个可进入人工复核，{overview.blockedCandidateCount} 个已阻断。</p>
          </article>
          <article className="live-overview-card">
            <span>审批记录</span>
            <strong>{overview.approvalCompleteCount} / {overview.approvalCount}</strong>
            <p>完成仅表示可创建后续治理记录。</p>
          </article>
          <article className={`live-overview-card ${overview.blockerCount > 0 ? "live-overview-card-warning" : ""}`}>
            <span>阻断与监控</span>
            <strong>{overview.blockerCount}</strong>
            <p>{overview.alertCount} 条告警，{overview.degradedSnapshotCount} 个异常或降级快照。</p>
          </article>
          <article className="live-overview-card">
            <span>运行模式</span>
            <strong>{overview.readOnlyVerified ? "只读" : "未确认"}</strong>
            <p>执行控制始终不可用；共有 {overview.deploymentRecordCount} 条部署治理记录。</p>
          </article>
        </section>
      ) : null}

      {!isLoading && overview.blockers.length > 0 ? (
        <section className="live-blocker-strip" aria-labelledby="live-blocker-title">
          <h2 id="live-blocker-title">当前阻断</h2>
          <ul>
            {overview.blockers.slice(0, 3).map((blocker) => <li key={blocker}>{blocker}</li>)}
          </ul>
          {overview.blockers.length > 3 ? (
            <ExpandableText
              summary={`查看其余 ${overview.blockers.length - 3} 条阻断`}
              value={overview.blockers.slice(3).join("\n")}
            />
          ) : null}
        </section>
      ) : null}

      {!isLoading && !hasRecords ? (
        <EmptyState
          description="当前没有可审计的候选、审批、部署治理或监控记录。空结果不代表实盘就绪。"
          title="暂无实盘候选治理记录"
        />
      ) : null}

      {!isLoading && governance.profiles.length > 0 ? (
        <section className="live-section" aria-labelledby="live-candidates-title">
          <div className="live-section-heading">
            <div>
              <h2 id="live-candidates-title">候选 Profile</h2>
              <p>先看候选资格和阻断，再按需核对证据与风险检查。</p>
            </div>
            <StatusBadge label={`${governance.profiles.length} 个候选`} status="candidate" />
          </div>
          <CandidateCards profiles={governance.profiles} />
        </section>
      ) : null}

      {!isLoading && governance.approvals.length > 0 ? (
        <section className="live-section" aria-labelledby="live-approvals-title">
          <div className="live-section-heading">
            <div>
              <h2 id="live-approvals-title">人工审批</h2>
              <p>审批不等于部署执行；未满足预检或人数要求时保持阻断。</p>
            </div>
            <StatusBadge label={`${governance.approvals.length} 条记录`} status="planned" />
          </div>
          <ApprovalRecords approvals={governance.approvals} />
        </section>
      ) : null}

      {!isLoading && governance.deployments.length > 0 ? (
        <section className="live-section" aria-labelledby="live-deployments-title">
          <div className="live-section-heading">
            <div>
              <h2 id="live-deployments-title">部署治理记录</h2>
              <p>只读展示规划、审批和回滚证据，不代表已执行生产发布。</p>
            </div>
            <StatusBadge label={`${governance.deployments.length} 条记录`} status="planned" />
          </div>
          <DeploymentRecords deployments={governance.deployments} />
        </section>
      ) : null}

      {!isLoading && governance.monitoringSnapshots.length > 0 ? (
        <section className="live-section" aria-labelledby="live-monitoring-title">
          <div className="live-section-heading">
            <div>
              <h2 id="live-monitoring-title">只读监控快照</h2>
              <p>异常、过期和不可用状态均不会解锁任何执行能力。</p>
            </div>
            <StatusBadge label={`${governance.monitoringSnapshots.length} 个快照`} status="warning" />
          </div>
          <MonitoringSnapshots snapshots={governance.monitoringSnapshots} />
        </section>
      ) : null}

      <section className="live-source-panel" aria-label="治理来源与安全边界">
        <StatusBadge
          label={overview.readOnlyVerified ? "只读证据" : "只读状态未确认"}
          status={overview.readOnlyVerified ? "ready" : "blocked"}
        />
        <p>{governance.safetyBoundary}</p>
        <dl className="live-detail-grid">
          <div>
            <dt>治理来源</dt>
            <dd>{displayDataOrigin(source)}</dd>
          </div>
          <div>
            <dt>来源引用</dt>
            <dd><CopyableValue label="治理来源引用" value={governance.sourceRef} /></dd>
          </div>
          <div>
            <dt>执行控制</dt>
            <dd>不可用</dd>
          </div>
          <div>
            <dt>密钥值</dt>
            <dd>不渲染</dd>
          </div>
        </dl>
        <CompactText label="完整安全边界" value={governance.safetyBoundary} />
      </section>
    </section>
  );
}
