import { useMvpData } from "../api/useMvpData";

export function BacktestRuns() {
  const { data, source, isLoading } = useMvpData();

  return (
    <section className="page">
      <header className="page-header">
        <h1>Backtest Runs</h1>
        <span className="status-pill">{isLoading ? "Loading" : source}</span>
      </header>
      <div className="table-shell">
        <table>
          <thead>
            <tr>
              <th>Run</th>
              <th>Strategy</th>
              <th>Status</th>
              <th>Profile</th>
              <th>Tasks</th>
              <th>Profit %</th>
              <th>Max Drawdown %</th>
            </tr>
          </thead>
          <tbody>
            {data.backtestRuns.map((run) => (
              <tr key={run.id}>
                <td>{run.id}</td>
                <td>{run.strategyName}</td>
                <td>{run.status}</td>
                <td>{run.profileName}</td>
                <td>
                  {run.completedTaskCount}/{run.requestedTaskCount}
                </td>
                <td>{run.profitPct?.toFixed(2) ?? "none"}</td>
                <td>{run.maxDrawdownPct?.toFixed(2) ?? "none"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {data.backtestRuns.length === 0 ? (
        <div className="empty-state">No backtest runs found.</div>
      ) : null}
    </section>
  );
}
