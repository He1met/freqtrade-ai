import { Link } from "react-router-dom";

import { useMvpData } from "../api/useMvpData";

export function Strategies() {
  const { data, source, isLoading } = useMvpData();

  return (
    <section className="page">
      <header className="page-header">
        <h1>Strategies</h1>
        <span className="status-pill">{isLoading ? "Loading" : source}</span>
      </header>
      <div className="table-shell">
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>Status</th>
              <th>Timeframe</th>
              <th>Source</th>
              <th>Version</th>
              <th>File</th>
            </tr>
          </thead>
          <tbody>
            {data.strategies.map((strategy) => (
              <tr key={strategy.id}>
                <td>
                  <Link to={`/strategies/${strategy.id}`}>{strategy.name}</Link>
                </td>
                <td>{strategy.status}</td>
                <td>{strategy.timeframe}</td>
                <td>{strategy.source}</td>
                <td>{strategy.currentVersion?.versionNumber ?? "none"}</td>
                <td className="path-cell">{strategy.currentVersion?.filePath ?? "none"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {data.strategies.length === 0 ? <div className="empty-state">No strategies found.</div> : null}
    </section>
  );
}
