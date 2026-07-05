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
import { useMvpData } from "../api/useMvpData";
import { summarizeText } from "./backtestDisplay";
import { FallbackNotice } from "./FallbackNotice";
import { EMPTY_TEXT, displayBoolean, displayLoadState, displayStatus, displayValue } from "./uiCopy";

function formatValue(value: string | number | boolean | null | undefined): string {
  return displayValue(value);
}

function statusClassName(status: string): string {
  const normalized = status.toLowerCase();
  if (["ready", "ok", "pass", "accepted", "success"].includes(normalized)) {
    return "status-success";
  }
  if (["failed", "failure", "rejected"].includes(normalized)) {
    return "status-failed";
  }
  if (["blocked", "warning", "stale", "unavailable"].includes(normalized)) {
    return "status-blocked";
  }
  return "status-neutral";
}

function statusPill(status: string) {
  return <span className={`run-status ${statusClassName(status)}`}>{displayStatus(status)}</span>;
}

function firstReason(...values: Array<string | null | undefined>): string {
  return values.find((value) => value?.trim()) ?? EMPTY_TEXT;
}

function reasonSummary(values: string[]): string {
  return values.length > 0 ? summarizeText(values[0]) : EMPTY_TEXT;
}

function RuntimeStatusCard({ title, status }: { title: string; status: RuntimeStatusSummary }) {
  return (
    <article className="overview-panel operator-status-card">
      <div className="operator-card-heading">
        <h2>{title}</h2>
        {statusPill(status.status)}
      </div>
      <p>{summarizeText(status.summary)}</p>
      <span className="phase6-muted">{formatValue(status.sourceRef ?? status.source)}</span>
      <span className="reason-line warning">
        {summarizeText(firstReason(status.blockedReason, status.unavailableReason, status.staleReason))}
      </span>
    </article>
  );
}

function RuntimeContractPanel({ contract }: { contract: RuntimeReadOnlyContractSummary }) {
  return (
    <section className="detail-section">
      <div className="section-header">
        <h2>Runtime Contract</h2>
        {statusPill(contract.status)}
      </div>
      <div className="operator-status-grid">
        <RuntimeStatusCard title="系统" status={contract.systemStatus} />
        <RuntimeStatusCard title="运行就绪" status={contract.runtimeReadiness} />
        <RuntimeStatusCard title="Smoke" status={contract.smokeStatus} />
        <article className="overview-panel operator-status-card">
          <div className="operator-card-heading">
            <h2>Fallback</h2>
            {statusPill(contract.fallbackStatus.status)}
          </div>
          <p>{contract.fallbackStatus.active ? "受控 fallback 已启用。" : "Backend 证据已启用。"}</p>
          <span className="phase6-muted">{contract.fallbackStatus.sources.join(", ") || EMPTY_TEXT}</span>
          <span className="reason-line warning">{summarizeText(contract.fallbackStatus.reason)}</span>
        </article>
      </div>
      <dl className="detail-list operator-boundary-list">
        <div>
          <dt>生成时间</dt>
          <dd>{formatValue(contract.generatedAt)}</dd>
        </div>
        <div>
          <dt>阻塞原因</dt>
          <dd>{reasonSummary(contract.blockedReasons)}</dd>
        </div>
        <div>
          <dt>不可用原因</dt>
          <dd>{reasonSummary(contract.unavailableReasons)}</dd>
        </div>
        <div>
          <dt>安全边界</dt>
          <dd>{contract.safety.boundary}</dd>
        </div>
      </dl>
    </section>
  );
}

function DiagnosticTable({ checks }: { checks: OperatorDiagnosticCheck[] }) {
  return (
    <section className="detail-section">
      <div className="section-header">
        <h2>诊断检查</h2>
        <span>{checks.length}</span>
      </div>
      <div className="table-shell">
        <table>
          <thead>
            <tr>
              <th>检查项</th>
              <th>状态</th>
              <th>区域</th>
              <th>来源</th>
              <th>证据</th>
              <th>原因</th>
            </tr>
          </thead>
          <tbody>
            {checks.map((check) => (
              <tr key={`${check.area}:${check.name}`}>
                <td>
                  <strong>{check.name}</strong>
                  <span className="secondary-cell">{check.required ? "必需" : "可选"}</span>
                </td>
                <td>{statusPill(check.status)}</td>
                <td>{check.area}</td>
                <td>{check.source}</td>
                <td className="artifact-cell">
                  <span>{formatValue(check.path)}</span>
                  <span>{check.exists === null ? "存在状态：未知" : `存在：${displayBoolean(check.exists)}`}</span>
                </td>
                <td className="reason-cell">
                  {summarizeText(firstReason(check.blockedReason, check.unavailableReason, check.summary))}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {checks.length === 0 ? <div className="empty-state">暂无运维检查项。</div> : null}
      </div>
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
  const rows = [
    ...runtimeArtifacts.map((artifact) => ({ ...artifact, group: "runtime" })),
    ...operatorArtifacts.map((artifact) => ({ ...artifact, group: "operator" })),
  ];

  return (
    <section className="detail-section">
      <div className="section-header">
        <h2>Artifacts</h2>
        <span>{rows.length}</span>
      </div>
      <div className="table-shell">
        <table>
          <thead>
            <tr>
              <th>名称</th>
              <th>状态</th>
              <th>分组</th>
              <th>来源</th>
              <th>路径</th>
              <th>存在</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((artifact) => (
              <tr key={`${artifact.group}:${artifact.name}:${artifact.path}`}>
                <td>
                  <strong>{artifact.name}</strong>
                </td>
                <td>{statusPill(artifact.status)}</td>
                <td>{artifact.group}</td>
                <td>{artifact.source}</td>
                <td className="path-cell">{artifact.path}</td>
                <td>{displayBoolean(artifact.exists)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {rows.length === 0 ? <div className="empty-state">暂无 artifact 链接。</div> : null}
      </div>
    </section>
  );
}

function EnvPresencePanel({ envPresence }: { envPresence: OperatorEnvPresence[] }) {
  return (
    <section className="detail-section">
      <div className="section-header">
        <h2>ENV Presence</h2>
        <span>值已隐藏</span>
      </div>
      <div className="operator-env-grid">
        {envPresence.map((item) => (
          <article className="overview-panel" key={item.name}>
            <div className="operator-card-heading">
              <h2>{item.name}</h2>
              {statusPill(item.present ? "PRESENT" : item.required ? "BLOCKED" : "MISSING")}
            </div>
            <dl className="compact-detail-list">
              <div>
                <dt>必需</dt>
                <dd>{displayBoolean(item.required)}</dd>
              </div>
              <div>
                <dt>来源</dt>
                <dd>{item.source}</dd>
              </div>
              <div>
                <dt>值</dt>
                <dd>{item.valueRendered ? "无效：已渲染" : "已隐藏"}</dd>
              </div>
            </dl>
          </article>
        ))}
        {envPresence.length === 0 ? <div className="empty-state">暂无 ENV presence 检查。</div> : null}
      </div>
    </section>
  );
}

function AuditEvents({ events }: { events: OperatorAuditEventSummary[] }) {
  return (
    <section className="detail-section">
      <div className="section-header">
        <h2>治理事件</h2>
        <span>{events.length}</span>
      </div>
      <ol className="event-list">
        {events.map((event) => (
          <li key={event.eventId}>
            <div className="event-heading">
              {statusPill(event.status)}
              <strong>{event.eventType}</strong>
              <span>{formatValue(event.createdAt)}</span>
            </div>
            <span>{event.eventId}</span>
            <p>{summarizeText(event.summary)}</p>
            <dl className="compact-detail-list">
              <div>
                <dt>Actor</dt>
                <dd>{event.actor}</dd>
              </div>
              <div>
                <dt>来源</dt>
                <dd>{event.sourceName}</dd>
              </div>
              <div>
                <dt>原因</dt>
                <dd>{summarizeText(event.reason)}</dd>
              </div>
              <div>
                <dt>Artifacts</dt>
                <dd>
                  {event.artifactLinks.length
                    ? event.artifactLinks.map((artifact) => artifact.path).join(", ")
                    : EMPTY_TEXT}
                </dd>
              </div>
            </dl>
          </li>
        ))}
      </ol>
      {events.length === 0 ? <div className="empty-state">暂无治理事件。</div> : null}
    </section>
  );
}

function SafetyPanel({
  operatorStatus,
  runtimeContract,
}: {
  operatorStatus: OperatorStatusReportSummary;
  runtimeContract: RuntimeReadOnlyContractSummary;
}) {
  const safetyRows = [
    ["Dashboard 模式", operatorStatus.safety.readOnly && runtimeContract.safety.readOnly ? "只读" : "未知"],
    ["ENV 值", operatorStatus.safety.reportsEnvValues ? "无效：已渲染" : "已隐藏"],
    ["Live trading", runtimeContract.safety.allowLiveTrading ? "已启用" : "已停用"],
    ["交易所连接", runtimeContract.safety.allowExchangeConnection ? "已启用" : "已停用"],
    ["部署控制", runtimeContract.safety.allowDeployControl ? "已启用" : "已停用"],
    ["Start / stop bot", runtimeContract.safety.canStartStopBot ? "已启用" : "已停用"],
  ];

  return (
    <section className="detail-section">
      <div className="section-header">
        <h2>安全状态</h2>
        <span>只读</span>
      </div>
      <dl className="detail-list">
        {safetyRows.map(([label, value]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

export function OperatorDashboard() {
  const { data, source, isLoading, error } = useMvpData();
  const dashboard = data.operatorDashboard;
  const runtimeContract = dashboard.runtimeContract;
  const operatorStatus = dashboard.operatorStatus;
  const blockedCount = operatorStatus.checks.filter((check) => check.status === "BLOCKED").length;
  const unavailableCount = operatorStatus.checks.filter((check) => check.status === "UNAVAILABLE").length;
  const auditBlockedCount = dashboard.auditEvents.filter((event) => event.status === "BLOCKED").length;

  return (
    <section className="page">
      <header className="page-header">
        <h1>运维面板（Operator Dashboard）</h1>
        <span className="status-pill">{displayLoadState(isLoading, source)}</span>
      </header>
      <FallbackNotice
        context="Operator Dashboard 运行证据、诊断检查、环境存在性和安全边界。"
        error={error}
        isLoading={isLoading}
        note="Backend API unavailable; showing controlled Phase 7 operator fallback data."
        source={source}
      />
      <section className="operator-summary-grid" aria-label="Operator Dashboard 摘要">
        <article className="metric">
          <span>Runtime Contract</span>
          <strong>{displayStatus(runtimeContract.status)}</strong>
          {statusPill(runtimeContract.runtimeReadiness.status)}
        </article>
        <article className="metric">
          <span>Operator 状态</span>
          <strong>{displayStatus(operatorStatus.status)}</strong>
          <span className="phase6-muted">
            {blockedCount} 个阻塞，{unavailableCount} 个不可用
          </span>
        </article>
        <article className="metric">
          <span>Smoke 状态</span>
          <strong>{displayStatus(runtimeContract.smokeStatus.status)}</strong>
          <span className="phase6-muted">{runtimeContract.artifactLinks.length} 个 artifact 链接</span>
        </article>
        <article className="metric">
          <span>审计事件</span>
          <strong>{dashboard.auditEvents.length}</strong>
          <span className="phase6-muted">{auditBlockedCount} 个阻塞事件</span>
        </article>
      </section>
      <section className="overview-grid">
        <article className="overview-panel">
          <h2>来源</h2>
          <p>{formatValue(dashboard.sourceRef)}</p>
          <span className="phase6-muted">{dashboard.readOnly ? "只读证据" : "未知模式"}</span>
        </article>
        <article className="overview-panel">
          <h2>边界</h2>
          <p>{dashboard.safetyBoundary}</p>
        </article>
      </section>
      <RuntimeContractPanel contract={runtimeContract} />
      <DiagnosticTable checks={operatorStatus.checks} />
      <ArtifactTable
        runtimeArtifacts={runtimeContract.artifactLinks}
        operatorArtifacts={operatorStatus.artifacts}
      />
      <EnvPresencePanel envPresence={operatorStatus.envPresence} />
      <AuditEvents events={dashboard.auditEvents} />
      <SafetyPanel operatorStatus={operatorStatus} runtimeContract={runtimeContract} />
    </section>
  );
}
