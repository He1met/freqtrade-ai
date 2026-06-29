import { useMvpData } from "../api/useMvpData";

export function Ranking() {
  const { data, source, isLoading } = useMvpData();

  return (
    <section className="page">
      <header className="page-header">
        <h1>Ranking</h1>
        <span className="status-pill">{isLoading ? "Loading" : source}</span>
      </header>
      <div className="table-shell">
        <table>
          <thead>
            <tr>
              <th>Rank</th>
              <th>Strategy</th>
              <th>Version</th>
              <th>Total</th>
              <th>Profit</th>
              <th>Risk</th>
              <th>Stability</th>
              <th>Quality</th>
              <th>File</th>
            </tr>
          </thead>
          <tbody>
            {data.ranking.map((entry) => (
              <tr key={`${entry.strategyId}-${entry.versionNumber}`}>
                <td>{entry.rank}</td>
                <td>{entry.strategyName}</td>
                <td>{entry.versionNumber}</td>
                <td>{entry.totalScore.toFixed(1)}</td>
                <td>{entry.profitScore?.toFixed(1) ?? "none"}</td>
                <td>{entry.riskScore?.toFixed(1) ?? "none"}</td>
                <td>{entry.stabilityScore?.toFixed(1) ?? "none"}</td>
                <td>{entry.qualityScore?.toFixed(1) ?? "none"}</td>
                <td className="path-cell">{entry.filePath}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {data.ranking.length === 0 ? <div className="empty-state">No scored strategies.</div> : null}
    </section>
  );
}
