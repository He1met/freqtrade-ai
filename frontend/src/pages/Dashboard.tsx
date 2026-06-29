import { useMvpData } from "../api/useMvpData";

export function Dashboard() {
  const { data, source, isLoading, error } = useMvpData();
  const succeededBacktests = data.backtestRuns.filter((run) => run.status === "succeeded").length;
  const summary = [
    { label: "Strategies", value: data.strategies.length },
    { label: "Generation Runs", value: data.generationRuns.length },
    { label: "Backtest Runs", value: data.backtestRuns.length },
    { label: "Ranked", value: data.ranking.length },
  ];

  return (
    <section className="page">
      <header className="page-header">
        <h1>Dashboard</h1>
        <span className="status-pill">{isLoading ? "Loading" : source}</span>
      </header>
      {error ? <div className="notice">Using fallback data: {error}</div> : null}
      <div className="metric-grid">
        {summary.map((item) => (
          <article className="metric" key={item.label}>
            <span>{item.label}</span>
            <strong>{item.value}</strong>
          </article>
        ))}
      </div>
      <div className="overview-grid">
        <article className="overview-panel">
          <h2>MVP Data Flow</h2>
          <p>
            {data.generationRuns.length} generation runs, {data.backtestTasks.length} backtest tasks,
            and {succeededBacktests} succeeded backtest runs are available for review.
          </p>
        </article>
        <article className="overview-panel">
          <h2>Ranking Leader</h2>
          {data.ranking[0] ? (
            <p>
              {data.ranking[0].strategyName} leads with total score{" "}
              {data.ranking[0].totalScore.toFixed(1)}.
            </p>
          ) : (
            <p>No ranked strategies are available.</p>
          )}
        </article>
      </div>
    </section>
  );
}
