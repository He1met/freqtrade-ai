import { Link } from "react-router-dom";

import { mockStrategies } from "../data/mock";

export function Strategies() {
  return (
    <section className="page">
      <header className="page-header">
        <h1>Strategies</h1>
      </header>
      <div className="table-shell">
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>Status</th>
              <th>Timeframe</th>
              <th>Source</th>
            </tr>
          </thead>
          <tbody>
            {mockStrategies.map((strategy) => (
              <tr key={strategy.id}>
                <td>
                  <Link to={`/strategies/${strategy.id}`}>{strategy.name}</Link>
                </td>
                <td>{strategy.status}</td>
                <td>{strategy.timeframe}</td>
                <td>{strategy.source}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
