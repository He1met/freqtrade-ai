import { useParams } from "react-router-dom";

import { useMvpData } from "../api/useMvpData";

export function StrategyDetail() {
  const { strategyId } = useParams();
  const { data, source, isLoading } = useMvpData();
  const strategy = data.strategies.find((item) => item.id === strategyId);
  const currentVersionId = strategy?.currentVersion?.id;
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
