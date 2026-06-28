import { useParams } from "react-router-dom";

export function StrategyDetail() {
  const { strategyId } = useParams();

  return (
    <section className="page">
      <header className="page-header">
        <h1>Strategy Detail</h1>
      </header>
      <dl className="detail-list">
        <div>
          <dt>ID</dt>
          <dd>{strategyId}</dd>
        </div>
        <div>
          <dt>Status</dt>
          <dd>draft</dd>
        </div>
        <div>
          <dt>Current Version</dt>
          <dd>none</dd>
        </div>
      </dl>
    </section>
  );
}
