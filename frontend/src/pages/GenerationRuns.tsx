import { useMvpData } from "../api/useMvpData";

export function GenerationRuns() {
  const { data, source, isLoading } = useMvpData();

  return (
    <section className="page">
      <header className="page-header">
        <h1>Generation Runs</h1>
        <span className="status-pill">{isLoading ? "Loading" : source}</span>
      </header>
      <div className="table-shell">
        <table>
          <thead>
            <tr>
              <th>Run</th>
              <th>Status</th>
              <th>Provider</th>
              <th>Model</th>
              <th>Requested</th>
              <th>Generated</th>
              <th>Accepted</th>
              <th>Failed</th>
              <th>Error</th>
            </tr>
          </thead>
          <tbody>
            {data.generationRuns.map((run) => (
              <tr key={run.id}>
                <td>{run.id}</td>
                <td>{run.status}</td>
                <td>{run.provider}</td>
                <td>{run.model}</td>
                <td>{run.requestedCount}</td>
                <td>{run.generatedCount}</td>
                <td>{run.acceptedCount}</td>
                <td>{run.failedCount}</td>
                <td className="path-cell">{run.errorMessage ?? "none"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {data.generationRuns.length === 0 ? (
        <div className="empty-state">No generation runs found.</div>
      ) : null}
    </section>
  );
}
