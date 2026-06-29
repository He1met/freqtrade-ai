const DEFAULT_FREQUI_URL = "http://127.0.0.1:8080";

function getConfiguredFreqUIUrl() {
  const env = (import.meta as ImportMeta & { env?: Record<string, string | undefined> }).env;
  const configuredUrl = env?.VITE_FREQUI_URL?.trim();

  return configuredUrl || null;
}

export function FreqUILink() {
  const configuredUrl = getConfiguredFreqUIUrl();
  const frequiUrl = configuredUrl ?? DEFAULT_FREQUI_URL;
  const isConfigured = configuredUrl !== null;

  return (
    <section className="page">
      <header className="page-header">
        <h1>FreqUI</h1>
        <span className="status-pill">{isConfigured ? "Configured" : "Local placeholder"}</span>
      </header>
      <div className="detail-list frequi-panel">
        <div>
          <dt>URL</dt>
          <dd>
            <code>{frequiUrl}</code>
          </dd>
        </div>
        <div>
          <dt>Status</dt>
          <dd>
            {isConfigured
              ? "Using VITE_FREQUI_URL."
              : "VITE_FREQUI_URL is not configured; using the local placeholder."}
          </dd>
        </div>
      </div>
      <a className="primary-link frequi-action" href={frequiUrl} target="_blank" rel="noreferrer">
        Open FreqUI
      </a>
    </section>
  );
}
