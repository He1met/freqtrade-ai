import { useMvpData } from "../api/useMvpData";

export function BacktestTasks() {
  const { data, source, isLoading } = useMvpData();

  return (
    <section className="page">
      <header className="page-header">
        <h1>Backtest Tasks</h1>
        <span className="status-pill">{isLoading ? "Loading" : source}</span>
      </header>
      <div className="table-shell">
        <table>
          <thead>
            <tr>
              <th>Task</th>
              <th>Run</th>
              <th>Strategy</th>
              <th>Pair</th>
              <th>Timeframe</th>
              <th>Status</th>
              <th>Profit %</th>
              <th>Config</th>
              <th>Result</th>
              <th>Error</th>
            </tr>
          </thead>
          <tbody>
            {data.backtestTasks.map((task) => (
              <tr key={task.id}>
                <td>{task.id}</td>
                <td>{task.runId}</td>
                <td>{task.strategyName}</td>
                <td>{task.pair}</td>
                <td>{task.timeframe}</td>
                <td>{task.status}</td>
                <td>{task.profitPct?.toFixed(2) ?? "none"}</td>
                <td className="path-cell">{task.configPath ?? "none"}</td>
                <td className="path-cell">{task.resultPath ?? "none"}</td>
                <td className="path-cell">{task.errorMessage ?? "none"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {data.backtestTasks.length === 0 ? (
        <div className="empty-state">No backtest tasks found.</div>
      ) : null}
    </section>
  );
}
