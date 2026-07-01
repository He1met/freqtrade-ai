import { useParams } from "react-router-dom";

import { useMvpData } from "../api/useMvpData";

function formatDiffLabel(label: string) {
  return label.split("_").join(" ");
}

function formatDiffValue(value: unknown) {
  if (Array.isArray(value)) {
    return value.length > 0 ? value.map((item) => String(item)).join(", ") : "none";
  }

  if (value === null || value === undefined || value === "") {
    return "none";
  }

  if (typeof value === "object") {
    return JSON.stringify(value);
  }

  return String(value);
}

export function StrategyDetail() {
  const { strategyId } = useParams();
  const { data, source, isLoading, error } = useMvpData();
  const strategy = data.strategies.find((item) => item.id === strategyId);
  const currentVersionId = strategy?.currentVersion?.id;
  const versionLineage = data.versionLineage
    .filter((entry) => entry.strategyId === strategy?.id)
    .sort((left, right) => left.versionNumber - right.versionNumber);
  const currentLineage = versionLineage.find((entry) => entry.id === currentVersionId);
  const currentDiffStatus = currentLineage
    ? currentLineage.hasParent
      ? "has parent"
      : "no parent"
    : "missing";
  const currentDiffEntries = Object.entries(currentLineage?.diffSnapshot ?? {});
  const validationErrors = strategy?.currentVersion?.validationErrors ?? [];
  const failureReasons = data.failureReasons.filter((reason) => {
    if (reason.strategyId !== strategy?.id) {
      return false;
    }

    return !currentVersionId || reason.strategyVersionId === currentVersionId;
  });

  if (!strategy) {
    return (
      <section className="page">
        <header className="page-header">
          <h1>Strategy Detail</h1>
          <span className="status-pill">{isLoading ? "Loading" : source}</span>
        </header>
        {error ? <div className="notice">API data unavailable. Showing fallback data. {error}</div> : null}
        <div className="empty-state">Strategy not found.</div>
      </section>
    );
  }

  return (
    <section className="page">
      <header className="page-header">
        <h1>{strategy.name}</h1>
        <span className="status-pill">{isLoading ? "Loading" : source}</span>
      </header>
      {error ? <div className="notice">API data unavailable. Showing fallback data. {error}</div> : null}
      <dl className="detail-list">
        <div>
          <dt>ID</dt>
          <dd>{strategy.id}</dd>
        </div>
        <div>
          <dt>Status</dt>
          <dd>{strategy.status}</dd>
        </div>
        <div>
          <dt>Timeframe</dt>
          <dd>{strategy.timeframe}</dd>
        </div>
        <div>
          <dt>Current Version</dt>
          <dd>{strategy.currentVersion?.versionNumber ?? "none"}</dd>
        </div>
        <div>
          <dt>Strategy File</dt>
          <dd>
            <code>{strategy.currentVersion?.filePath ?? "none"}</code>
          </dd>
        </div>
        <div>
          <dt>Description</dt>
          <dd>{strategy.description}</dd>
        </div>
        <div>
          <dt>Tags</dt>
          <dd>{strategy.tags.join(", ") || "none"}</dd>
        </div>
      </dl>
      <section className="detail-section">
        <div className="section-header">
          <h2>Version Lineage</h2>
          <span>{versionLineage.length}</span>
        </div>
        {versionLineage.length > 0 ? (
          <ol className="lineage-list">
            {versionLineage.map((entry) => (
              <li
                className={
                  entry.id === currentVersionId
                    ? "lineage-item lineage-item-current"
                    : "lineage-item"
                }
                key={entry.id}
              >
                <div className="lineage-heading">
                  <strong>Version {entry.versionNumber}</strong>
                  {entry.id === currentVersionId ? (
                    <span className="status-pill">current</span>
                  ) : null}
                </div>
                <dl className="lineage-meta">
                  <div>
                    <dt>Parent</dt>
                    <dd>{entry.parentVersionId ?? "none"}</dd>
                  </div>
                  <div>
                    <dt>Change</dt>
                    <dd>{entry.changeSummary ?? "No change summary recorded."}</dd>
                  </div>
                </dl>
              </li>
            ))}
          </ol>
        ) : (
          <div className="empty-state">No version lineage recorded for this strategy.</div>
        )}
      </section>
      <section className="detail-section">
        <div className="section-header">
          <h2>Current Version Diff</h2>
          <span>{currentDiffStatus}</span>
        </div>
        {currentLineage ? (
          <div className="diff-panel">
            <dl className="lineage-meta">
              <div>
                <dt>Parent Version</dt>
                <dd>{currentLineage.parentVersionId ?? "none"}</dd>
              </div>
              <div>
                <dt>Summary</dt>
                <dd>{currentLineage.changeSummary ?? "No diff summary recorded."}</dd>
              </div>
            </dl>
            {currentDiffEntries.length > 0 ? (
              <dl className="diff-grid">
                {currentDiffEntries.map(([key, value]) => (
                  <div key={key}>
                    <dt>{formatDiffLabel(key)}</dt>
                    <dd>{formatDiffValue(value)}</dd>
                  </div>
                ))}
              </dl>
            ) : (
              <div className="empty-state">No diff snapshot recorded for this version.</div>
            )}
          </div>
        ) : (
          <div className="empty-state">No diff data recorded for the current version.</div>
        )}
      </section>
      <section className="detail-section">
        <div className="section-header">
          <h2>Validation Errors</h2>
          <span>{validationErrors.length}</span>
        </div>
        {validationErrors.length > 0 ? (
          <ul className="issue-list">
            {validationErrors.map((error) => (
              <li key={`${error.field ?? "strategy"}-${error.code ?? error.message}`}>
                <strong>{error.field ?? "strategy"}</strong>
                <span>{error.message}</span>
                {error.code ? <code>{error.code}</code> : null}
              </li>
            ))}
          </ul>
        ) : (
          <div className="empty-state">No validation errors recorded for this version.</div>
        )}
      </section>
      <section className="detail-section">
        <div className="section-header">
          <h2>Failure Reasons</h2>
          <span>{failureReasons.length}</span>
        </div>
        {failureReasons.length > 0 ? (
          <ul className="issue-list">
            {failureReasons.map((reason) => (
              <li key={reason.id}>
                <div className="reason-heading">
                  <strong>{reason.stage}</strong>
                  <span className={`severity severity-${reason.severity}`}>{reason.severity}</span>
                </div>
                <span>{reason.message}</span>
                <code>{reason.reasonType}</code>
              </li>
            ))}
          </ul>
        ) : (
          <div className="empty-state">No failure reasons recorded for this version.</div>
        )}
      </section>
    </section>
  );
}
