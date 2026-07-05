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

function formatValue(value: string | number | boolean | null | undefined): string {
  return value === null || value === undefined || value === "" ? "none" : String(value);
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
  return <span className={`run-status ${statusClassName(status)}`}>{status}</span>;
}

function firstReason(...values: Array<string | null | undefined>): string {
  return values.find((value) => value?.trim()) ?? "none";
}

function reasonSummary(values: string[]): string {
  return values.length > 0 ? summarizeText(values[0]) : "none";
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
        <RuntimeStatusCard title="System" status={contract.systemStatus} />
        <RuntimeStatusCard title="Readiness" status={contract.runtimeReadiness} />
        <RuntimeStatusCard title="Smoke" status={contract.smokeStatus} />
        <article className="overview-panel operator-status-card">
          <div className="operator-card-heading">
            <h2>Fallback</h2>
            {statusPill(contract.fallbackStatus.status)}
          </div>
          <p>{contract.fallbackStatus.active ? "Controlled fallback is active." : "Backend evidence is active."}</p>
          <span className="phase6-muted">{contract.fallbackStatus.sources.join(", ") || "none"}</span>
          <span className="reason-line warning">{summarizeText(contract.fallbackStatus.reason)}</span>
        </article>
      </div>
      <dl className="detail-list operator-boundary-list">
        <div>
          <dt>Generated</dt>
          <dd>{formatValue(contract.generatedAt)}</dd>
        </div>
        <div>
          <dt>Blocked</dt>
          <dd>{reasonSummary(contract.blockedReasons)}</dd>
        </div>
        <div>
          <dt>Unavailable</dt>
          <dd>{reasonSummary(contract.unavailableReasons)}</dd>
        </div>
        <div>
          <dt>Safety boundary</dt>
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
        <h2>Diagnostic Checks</h2>
        <span>{checks.length}</span>
      </div>
      <div className="table-shell">
        <table>
          <thead>
            <tr>
              <th>Check</th>
              <th>Status</th>
              <th>Area</th>
              <th>Source</th>
              <th>Evidence</th>
              <th>Reason</th>
            </tr>
          </thead>
          <tbody>
            {checks.map((check) => (
              <tr key={`${check.area}:${check.name}`}>
                <td>
                  <strong>{check.name}</strong>
                  <span className="secondary-cell">{check.required ? "required" : "optional"}</span>
                </td>
                <td>{statusPill(check.status)}</td>
                <td>{check.area}</td>
                <td>{check.source}</td>
                <td className="artifact-cell">
                  <span>{formatValue(check.path)}</span>
                  <span>{check.exists === null ? "exists: unknown" : `exists: ${check.exists}`}</span>
                </td>
                <td className="reason-cell">
                  {summarizeText(firstReason(check.blockedReason, check.unavailableReason, check.summary))}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {checks.length === 0 ? <div className="empty-state">No operator checks found.</div> : null}
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
              <th>Name</th>
              <th>Status</th>
              <th>Group</th>
              <th>Source</th>
              <th>Path</th>
              <th>Exists</th>
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
                <td>{artifact.exists ? "yes" : "no"}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {rows.length === 0 ? <div className="empty-state">No artifact links found.</div> : null}
      </div>
    </section>
  );
}

function EnvPresencePanel({ envPresence }: { envPresence: OperatorEnvPresence[] }) {
  return (
    <section className="detail-section">
      <div className="section-header">
        <h2>ENV Presence</h2>
        <span>values hidden</span>
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
                <dt>Required</dt>
                <dd>{item.required ? "yes" : "no"}</dd>
              </div>
              <div>
                <dt>Source</dt>
                <dd>{item.source}</dd>
              </div>
              <div>
                <dt>Value</dt>
                <dd>{item.valueRendered ? "invalid: rendered" : "hidden"}</dd>
              </div>
            </dl>
          </article>
        ))}
        {envPresence.length === 0 ? <div className="empty-state">No ENV presence checks found.</div> : null}
      </div>
    </section>
  );
}

function AuditEvents({ events }: { events: OperatorAuditEventSummary[] }) {
  return (
    <section className="detail-section">
      <div className="section-header">
        <h2>Governance Events</h2>
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
                <dt>Source</dt>
                <dd>{event.sourceName}</dd>
              </div>
              <div>
                <dt>Reason</dt>
                <dd>{summarizeText(event.reason)}</dd>
              </div>
              <div>
                <dt>Artifacts</dt>
                <dd>
                  {event.artifactLinks.length
                    ? event.artifactLinks.map((artifact) => artifact.path).join(", ")
                    : "none"}
                </dd>
              </div>
            </dl>
          </li>
        ))}
      </ol>
      {events.length === 0 ? <div className="empty-state">No governance events found.</div> : null}
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
    ["Dashboard mode", operatorStatus.safety.readOnly && runtimeContract.safety.readOnly ? "read-only" : "unknown"],
    ["ENV values", operatorStatus.safety.reportsEnvValues ? "invalid: rendered" : "hidden"],
    ["Live trading", runtimeContract.safety.allowLiveTrading ? "enabled" : "disabled"],
    ["Exchange connection", runtimeContract.safety.allowExchangeConnection ? "enabled" : "disabled"],
    ["Deploy control", runtimeContract.safety.allowDeployControl ? "enabled" : "disabled"],
    ["Start / stop bot", runtimeContract.safety.canStartStopBot ? "enabled" : "disabled"],
  ];

  return (
    <section className="detail-section">
      <div className="section-header">
        <h2>Safety State</h2>
        <span>read-only</span>
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
        <h1>Operator Dashboard</h1>
        <span className="status-pill">{isLoading ? "Loading" : source}</span>
      </header>
      {error ? <div className="notice">Using fallback data: {error}</div> : null}
      {!isLoading && source === "fallback" && !error ? (
        <div className="notice">Backend API unavailable; showing controlled Phase 7 operator fallback data.</div>
      ) : null}
      <section className="operator-summary-grid" aria-label="Operator dashboard summary">
        <article className="metric">
          <span>Runtime Contract</span>
          <strong>{runtimeContract.status}</strong>
          {statusPill(runtimeContract.runtimeReadiness.status)}
        </article>
        <article className="metric">
          <span>Operator Status</span>
          <strong>{operatorStatus.status}</strong>
          <span className="phase6-muted">
            {blockedCount} blocked, {unavailableCount} unavailable
          </span>
        </article>
        <article className="metric">
          <span>Smoke Status</span>
          <strong>{runtimeContract.smokeStatus.status}</strong>
          <span className="phase6-muted">{runtimeContract.artifactLinks.length} artifact link(s)</span>
        </article>
        <article className="metric">
          <span>Audit Events</span>
          <strong>{dashboard.auditEvents.length}</strong>
          <span className="phase6-muted">{auditBlockedCount} blocked event(s)</span>
        </article>
      </section>
      <section className="overview-grid">
        <article className="overview-panel">
          <h2>Source</h2>
          <p>{formatValue(dashboard.sourceRef)}</p>
          <span className="phase6-muted">{dashboard.readOnly ? "read-only evidence" : "unknown mode"}</span>
        </article>
        <article className="overview-panel">
          <h2>Boundary</h2>
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
