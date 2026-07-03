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
        <span className="status-pill">{isConfigured ? "已配置" : "本地占位"}</span>
      </header>
      <div className="detail-list frequi-panel">
        <div>
          <dt>URL</dt>
          <dd>
            <code>{frequiUrl}</code>
          </dd>
        </div>
        <div>
          <dt>状态</dt>
          <dd>
            {isConfigured
              ? "正在使用 VITE_FREQUI_URL。"
              : "未配置 VITE_FREQUI_URL，当前使用本地占位地址。"}
          </dd>
        </div>
      </div>
      <a className="primary-link frequi-action" href={frequiUrl} target="_blank" rel="noreferrer">
        打开 FreqUI
      </a>
    </section>
  );
}
