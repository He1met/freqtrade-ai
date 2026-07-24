import type {
  OperatorArtifactStatus,
  OperatorAuditEventSummary,
  OperatorDiagnosticCheck,
  OperatorEnvPresence,
  OperatorStatusReportSummary,
  RuntimeArtifactLink,
  RuntimeReadOnlyContractSummary,
  RuntimeStatusSummary,
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
import "../styles/operator-dashboard.css";
import { FallbackNotice } from "./FallbackNotice";
import {
  environmentContractViolations,
  isOperatorProblemStatus,
  operatorDiagnosticCounts,
  operatorDiagnosticReason,
  operatorSystemConclusion,
  runtimeStatusReason,
  safetyBoundaryViolations,
  sortOperatorDiagnostics,
} from "./operatorDashboardDisplay";
import { operatorDashboardNotice } from "./operatorDashboardNotice";
import { EMPTY_TEXT, displayBoolean, displayLoadState, displayStatus, displayValue } from "./uiCopy";

function RuntimeStatusCard({ title, status }: { title: string; status: RuntimeStatusSummary }) {
  const reason = runtimeStatusReason(status);

  return (
    <article className={`operator-runtime-card ${isOperatorProblemStatus(status.status) ? "is-problem" : ""}`}>
      <div className="operator-card-heading">
        <h3>{title}</h3>
        <StatusBadge showRaw status={status.status} />
      </div>
      <p>{status.summary}</p>
      <CompactText
        className="operator-source-ref"
        label={`${title}证据来源`}
        mono
        value={status.sourceRef ?? status.source}
      />
      {reason ? <p className="operator-card-reason">{reason}</p> : null}
    </article>
  );
}

function RuntimeContractPanel({ contract }: { contract: RuntimeReadOnlyContractSummary }) {
  return (
    <details className="operator-detail-group" open>
      <summary>
        <span>
          <strong>运行契约明细</strong>
          <small>Runtime Contract · readiness、smoke 与 fallback</small>
        </span>
        <StatusBadge showRaw status={contract.status} />
      </summary>
      <div className="operator-detail-content">
        <div className="operator-runtime-grid">
          <RuntimeStatusCard title="系统状态" status={contract.systemStatus} />
          <RuntimeStatusCard title="运行就绪" status={contract.runtimeReadiness} />
          <RuntimeStatusCard title="研究就绪" status={contract.researchReadiness} />
          <RuntimeStatusCard title="Dry-run 就绪" status={contract.dryRunReadiness} />
          <RuntimeStatusCard title="Live 就绪（只读观察）" status={contract.liveReadiness} />
          <RuntimeStatusCard title="Smoke" status={contract.smokeStatus} />
          <article
            className={`operator-runtime-card ${contract.fallbackStatus.active ? "is-problem" : ""}`}
          >
            <div className="operator-card-heading">
              <h3>Fallback</h3>
              <StatusBadge showRaw status={contract.fallbackStatus.status} />
            </div>
            <p>{contract.fallbackStatus.active ? "受控 fallback 已启用。" : "未启用 fallback。"}</p>
            <CompactText
              label="Fallback 来源"
              mono
              value={contract.fallbackStatus.sources.join(", ") || EMPTY_TEXT}
            />
            {contract.fallbackStatus.reason ? (
              <p className="operator-card-reason">{contract.fallbackStatus.reason}</p>
            ) : null}
          </article>
        </div>
        <dl className="operator-contract-facts">
          <div>
            <dt>Schema</dt>
            <dd><CopyableValue label="Schema version" value={contract.schemaVersion} /></dd>
          </div>
          <div>
            <dt>生成时间</dt>
            <dd>{displayValue(contract.generatedAt)}</dd>
          </div>
          <div>
            <dt>阻断原因</dt>
            <dd>
              <ExpandableText
                emptyText="无已报告阻断"
                summary="展开阻断原因"
                value={contract.blockedReasons.join("\n")}
              />
            </dd>
          </div>
          <div>
            <dt>不可用原因</dt>
            <dd>
              <ExpandableText
                emptyText="无已报告不可用原因"
                summary="展开不可用原因"
                value={contract.unavailableReasons.join("\n")}
              />
            </dd>
          </div>
        </dl>
      </div>
    </details>
  );
}

function DiagnosticTable({ checks }: { checks: OperatorDiagnosticCheck[] }) {
  const sortedChecks = sortOperatorDiagnostics(checks);

  return (
    <section className="operator-section operator-diagnostics">
      <div className="operator-section-heading">
        <div>
          <h2>诊断检查</h2>
          <p>失败、阻断、不可用与过期项优先显示。</p>
        </div>
        <span>{checks.length} 项</span>
      </div>
      {sortedChecks.length === 0 ? (
        <EmptyState description="当前报告没有提供诊断检查项。" title="暂无诊断证据" />
      ) : (
        <div className="table-shell operator-diagnostic-table-shell">
          <table>
            <colgroup>
              <col className="operator-col-check" />
              <col className="operator-col-status" />
              <col className="operator-col-source" />
              <col className="operator-col-evidence" />
              <col className="operator-col-reason" />
            </colgroup>
            <thead>
              <tr>
                <th>检查项</th>
                <th>状态</th>
                <th>来源</th>
                <th>证据</th>
                <th>原因 / 下一步</th>
              </tr>
            </thead>
            <tbody>
              {sortedChecks.map((check) => {
                const reason = operatorDiagnosticReason(check);
                const problem = isOperatorProblemStatus(check.status);
                return (
                  <tr className={problem ? "operator-problem-row" : undefined} key={`${check.area}:${check.name}`}>
                    <td>
                      <strong><CompactText label="检查项" value={check.name} /></strong>
                      <span className="operator-secondary">{check.area} · {check.required ? "必需" : "可选"}</span>
                    </td>
                    <td><StatusBadge showRaw status={check.status} /></td>
                    <td><CompactText label="来源" mono value={check.source} /></td>
                    <td>
                      <CopyableValue label="证据路径" value={check.path} />
                      <span className="operator-secondary">
                        {check.exists === null ? "存在性未知" : `存在：${displayBoolean(check.exists)}`}
                      </span>
                    </td>
                    <td>
                      {reason ? (
                        <ExpandableText
                          className={problem ? "operator-problem-reason" : undefined}
                          summary="展开原因与下一步"
                          value={reason}
                        />
                      ) : (
                        <span className="operator-healthy-copy">未报告问题</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function ArtifactTable({
  runtimeArtifacts,
  operatorArtifacts,
}: {
  runtimeArtifacts: RuntimeArtifactLink[];
  operatorArtifacts: OperatorArtifactStatus[];
}) {
  const artifactLinks = [
    ...runtimeArtifacts.map((artifact) => ({ ...artifact, group: "runtime" })),
    ...operatorArtifacts.map((artifact) => ({ ...artifact, group: "operator" })),
  ];

  return (
    <details className="operator-detail-group">
      <summary>
        <span>
          <strong>Artifacts</strong>
          <small>路径可悬停查看并复制</small>
        </span>
        <span>{artifactLinks.length} 项</span>
      </summary>
      <div className="operator-detail-content">
        {artifactLinks.length === 0 ? (
          <EmptyState description="当前报告没有提供 artifact 引用。" title="暂无 Artifact" />
        ) : (
          <div className="table-shell operator-artifact-table-shell">
            <table>
              <thead>
                <tr>
                  <th>名称</th>
                  <th>状态</th>
                  <th>分组 / 来源</th>
                  <th>路径</th>
                  <th>存在</th>
                </tr>
              </thead>
              <tbody>
                {artifactLinks.map((artifact) => (
                  <tr key={`${artifact.group}:${artifact.name}:${artifact.path}`}>
                    <td><CompactText label="Artifact 名称" value={artifact.name} /></td>
                    <td><StatusBadge showRaw status={artifact.status} /></td>
                    <td>
                      <span>{artifact.group}</span>
                      <CompactText className="operator-secondary" label="Artifact 来源" mono value={artifact.source} />
                    </td>
                    <td><CopyableValue label="Artifact 路径" value={artifact.path} /></td>
                    <td>{displayBoolean(artifact.exists)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </details>
  );
}

function EnvPresencePanel({ envPresence }: { envPresence: OperatorEnvPresence[] }) {
  const violations = environmentContractViolations(envPresence);

  return (
    <details className="operator-detail-group">
      <summary>
        <span>
          <strong>ENV Presence</strong>
          <small>只显示是否存在，绝不显示环境变量值</small>
        </span>
        <span>{envPresence.length} 项</span>
      </summary>
      <div className="operator-detail-content">
        {violations.length > 0 ? (
          <div className="operator-inline-alert" role="alert">
            <strong>ENV 展示契约违规</strong>
            <span>{violations.join("；")}</span>
          </div>
        ) : null}
        {envPresence.length === 0 ? (
          <EmptyState description="当前报告没有提供 ENV presence 检查。" title="暂无 ENV 证据" />
        ) : (
          <div className="operator-env-list">
            {envPresence.map((item) => (
              <article className="operator-env-row" key={item.name}>
                <CopyableValue label="ENV 名称" value={item.name} />
                <StatusBadge
                  label={item.present ? "已配置" : item.required ? "缺失（必需）" : "未配置（可选）"}
                  status={item.present ? "PRESENT" : item.required ? "BLOCKED" : "MISSING"}
                />
                <span>{item.required ? "必需" : "可选"}</span>
                <CompactText label="ENV 来源" mono value={item.source} />
              </article>
            ))}
          </div>
        )}
      </div>
    </details>
  );
}

function AuditEvents({ events }: { events: OperatorAuditEventSummary[] }) {
  return (
    <details className="operator-detail-group">
      <summary>
        <span>
          <strong>治理事件</strong>
          <small>审计结果、原因与 artifact 引用</small>
        </span>
        <span>{events.length} 项</span>
      </summary>
      <div className="operator-detail-content">
        {events.length === 0 ? (
          <EmptyState description="当前报告没有提供治理事件。" title="暂无治理事件" />
        ) : (
          <ol className="operator-event-list">
            {events.map((event) => (
              <li className={isOperatorProblemStatus(event.status) ? "is-problem" : undefined} key={event.eventId}>
                <div className="operator-event-heading">
                  <StatusBadge showRaw status={event.status} />
                  <strong>{event.eventType}</strong>
                  <span>{displayValue(event.createdAt)}</span>
                </div>
                <CopyableValue label="Event ID" value={event.eventId} />
                <p>{event.summary}</p>
                <div className="operator-event-meta">
                  <span>Actor：{event.actor}</span>
                  <span>来源：{event.sourceName}</span>
                </div>
                {event.reason ? <ExpandableText summary="展开事件原因" value={event.reason} /> : null}
                {event.artifactLinks.map((artifact) => (
                  <CopyableValue
                    key={`${event.eventId}:${artifact.path}`}
                    label="事件 Artifact 路径"
                    value={artifact.path}
                  />
                ))}
              </li>
            ))}
          </ol>
        )}
      </div>
    </details>
  );
}

function SafetyPanel({
  operatorStatus,
  runtimeContract,
}: {
  operatorStatus: OperatorStatusReportSummary;
  runtimeContract: RuntimeReadOnlyContractSummary;
}) {
  const violations = safetyBoundaryViolations(runtimeContract, operatorStatus);
  const safetyRows = [
    ["Dashboard 模式", operatorStatus.safety.readOnly && runtimeContract.safety.readOnly ? "只读" : "未确认只读"],
    ["ENV 值", operatorStatus.safety.reportsEnvValues ? "契约违规" : "不展示"],
    ["Live trading", runtimeContract.safety.allowLiveTrading ? "允许" : "禁止"],
    ["交易所连接", runtimeContract.safety.allowExchangeConnection ? "允许" : "禁止"],
    ["部署控制", runtimeContract.safety.allowDeployControl ? "允许" : "禁止"],
    ["Start / stop bot", runtimeContract.safety.canStartStopBot ? "允许" : "禁止"],
  ];

  return (
    <details className="operator-detail-group">
      <summary>
        <span>
          <strong>安全状态明细</strong>
          <small>只读边界与禁止能力</small>
        </span>
        <StatusBadge
          label={violations.length === 0 ? "边界安全" : `${violations.length} 项越界`}
          status={violations.length === 0 ? "READY" : "FAILED"}
        />
      </summary>
      <div className="operator-detail-content">
        <dl className="operator-safety-list">
          {safetyRows.map(([label, value]) => (
            <div key={label}>
              <dt>{label}</dt>
              <dd>{value}</dd>
            </div>
          ))}
        </dl>
      </div>
    </details>
  );
}

export function OperatorDashboard() {
  const { data, sources, isLoading, error } = useMvpData();
  const source = combineDataSources(sources, ["operatorDashboard"]);
  const dashboard = data.operatorDashboard;
  const runtimeContract = dashboard.runtimeContract;
  const operatorStatus = dashboard.operatorStatus;
  const auditEvents = dashboard.auditEvents;
  const conclusion = operatorSystemConclusion(runtimeContract, operatorStatus);
  const counts = operatorDiagnosticCounts(operatorStatus.checks);
  const safetyViolations = safetyBoundaryViolations(runtimeContract, operatorStatus);
  const firstProblem = sortOperatorDiagnostics(operatorStatus.checks).find((check) =>
    isOperatorProblemStatus(check.status),
  );
  const firstProblemReason = firstProblem ? operatorDiagnosticReason(firstProblem) : null;

  return (
    <section className="page operator-dashboard-page">
      <PageHeader
        description="集中查看运行就绪度、阻断诊断与只读安全边界。"
        eyebrow="只读运维证据"
        status={<span className="status-pill">{displayLoadState(isLoading, source)}</span>}
        title="运维面板（Operator Dashboard）"
      />
      <FallbackNotice
        context="Operator Dashboard 运行证据、诊断检查、环境存在性和安全边界。"
        error={error}
        isLoading={isLoading}
        note={operatorDashboardNotice(source)}
        source={source}
      />

      <section className={`operator-conclusion ${isOperatorProblemStatus(conclusion.status) ? "is-problem" : "is-ready"}`}>
        <div>
          <span className="operator-conclusion-label">系统结论</span>
          <h2>{conclusion.label}</h2>
          <p>
            {conclusion.reason ??
              "运行契约与 Operator 报告未声明阻断；仍以本页只读证据和安全边界为准。"}
          </p>
        </div>
        <StatusBadge showRaw status={conclusion.status} />
      </section>

      <section className="operator-summary-grid" aria-label="Operator Dashboard 首屏摘要">
        <article className="operator-summary-card">
          <span>Runtime Contract</span>
          <strong>{displayStatus(runtimeContract.status)}</strong>
          <StatusBadge showRaw status={runtimeContract.status} />
        </article>
        <article className="operator-summary-card">
          <span>Operator 状态</span>
          <strong>{displayStatus(operatorStatus.status)}</strong>
          <StatusBadge showRaw status={operatorStatus.status} />
        </article>
        <article className="operator-summary-card">
          <span>Smoke 状态</span>
          <strong>{displayStatus(runtimeContract.smokeStatus.status)}</strong>
          <CompactText label="Smoke 状态摘要" value={runtimeContract.smokeStatus.summary} />
        </article>
        <article className="operator-summary-card">
          <span>研究就绪</span>
          <strong>{displayStatus(runtimeContract.researchReadiness.status)}</strong>
          <CompactText label="研究状态摘要" value={runtimeContract.researchReadiness.summary} />
        </article>
        <article className="operator-summary-card">
          <span>Dry-run 就绪</span>
          <strong>{displayStatus(runtimeContract.dryRunReadiness.status)}</strong>
          <CompactText label="Dry-run 状态摘要" value={runtimeContract.dryRunReadiness.summary} />
        </article>
        <article className={`operator-summary-card ${counts.totalProblems > 0 ? "is-problem" : ""}`}>
          <span>诊断问题</span>
          <strong>{counts.totalProblems}</strong>
          <span className="operator-secondary">
            失败 {counts.failed} · 阻断 {counts.blocked} · 不可用 {counts.unavailable} · 过期 {counts.stale}
          </span>
        </article>
      </section>

      <section className={`operator-boundary-banner ${safetyViolations.length > 0 ? "is-problem" : ""}`}>
        <div>
          <span>安全边界</span>
          <strong>{safetyViolations.length === 0 ? "只读观察；禁止真实交易与运行控制" : "检测到安全边界越界"}</strong>
          <p>{safetyViolations.length > 0 ? safetyViolations.join("；") : dashboard.safetyBoundary}</p>
        </div>
        <div className="operator-boundary-source">
          <span>{dashboard.readOnly ? "只读证据" : "只读状态未确认"}</span>
          <CopyableValue label="Dashboard 来源" value={dashboard.sourceRef} />
        </div>
      </section>

      {firstProblem ? (
        <aside className="operator-primary-blocker" role="alert">
          <StatusBadge showRaw status={firstProblem.status} />
          <div>
            <strong>{firstProblem.name}</strong>
            <p>{firstProblemReason ?? "该检查项处于问题状态，但报告未提供原因；需要补齐诊断证据。"}</p>
          </div>
        </aside>
      ) : null}

      <DiagnosticTable checks={operatorStatus.checks} />
      <RuntimeContractPanel contract={runtimeContract} />
      <ArtifactTable
        operatorArtifacts={operatorStatus.artifacts}
        runtimeArtifacts={runtimeContract.artifactLinks}
      />
      <EnvPresencePanel envPresence={operatorStatus.envPresence} />
      <AuditEvents events={auditEvents} />
      <SafetyPanel operatorStatus={operatorStatus} runtimeContract={runtimeContract} />
    </section>
  );
}
