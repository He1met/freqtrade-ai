import { useParams } from "react-router-dom";

import { useMvpData } from "../api/useMvpData";

export function StrategyDetail() {
  const { strategyId } = useParams();
  const { data, source, isLoading } = useMvpData();
  const strategy = data.strategies.find((item) => item.id === strategyId);

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
    </section>
  );
}
