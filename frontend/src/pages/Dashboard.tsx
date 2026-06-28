import { summary } from "../data/mock";

export function Dashboard() {
  return (
    <section className="page">
      <header className="page-header">
        <h1>Dashboard</h1>
        <span className="status-pill">Phase 0</span>
      </header>
      <div className="metric-grid">
        {summary.map((item) => (
          <article className="metric" key={item.label}>
            <span>{item.label}</span>
            <strong>{item.value}</strong>
          </article>
        ))}
      </div>
    </section>
  );
}
