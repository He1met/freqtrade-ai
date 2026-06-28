import { mockRuns } from "../data/mock";

export function GenerationRuns() {
  return (
    <section className="page">
      <header className="page-header">
        <h1>Generation Runs</h1>
      </header>
      <div className="table-shell">
        <table>
          <thead>
            <tr>
              <th>Run</th>
              <th>Status</th>
              <th>Target</th>
              <th>Success</th>
              <th>Failed</th>
            </tr>
          </thead>
          <tbody>
            {mockRuns.map((run) => (
              <tr key={run.name}>
                <td>{run.name}</td>
                <td>{run.status}</td>
                <td>{run.total}</td>
                <td>{run.success}</td>
                <td>{run.failed}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
