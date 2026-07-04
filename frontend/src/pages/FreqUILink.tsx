import type { DryRunArtifactManifest, DryRunStatusSnapshot } from "../api/types";
import { useMvpData } from "../api/useMvpData";
import { formatNumber, reasonText, statusClassName, summarizeText } from "./backtestDisplay";

function getConfiguredFreqUIUrl() {
  const env = (import.meta as ImportMeta & { env?: Record<string, string | undefined> }).env;
  const configuredUrl = env?.VITE_FREQUI_URL?.trim();

  return configuredUrl || null;
}

function formatValue(value: number | string | null | undefined): string {
  return value === null || value === undefined || value === "" ? "none" : String(value);
}

function snapshotReason(snapshot: DryRunStatusSnapshot, manifest: DryRunArtifactManifest | null): string {
  return reasonText(
    snapshot.blockedReason ?? manifest?.blockedReason ?? null,
    snapshot.failedReason ?? manifest?.failedReason ?? null,
  );
}

export function FreqUILink() {
  const { data, source, isLoading, error } = useMvpData();
  const { manifest, snapshot, freqUiLink } = data.dryRun;
  const configuredFreqUiUrl = getConfiguredFreqUIUrl();
  const frequiUrl = freqUiLink.baseUrl ?? configuredFreqUiUrl;
  const linkEnabled = (freqUiLink.enabled || configuredFreqUiUrl !== null) && frequiUrl !== null;
  const environmentLabel =
    freqUiLink.baseUrl === null && configuredFreqUiUrl !== null
      ? "local-env"
      : freqUiLink.environmentLabel;
  const reason = snapshotReason(snapshot, manifest);
  const summaryRows = [
    { label: "Status", value: snapshot.status, className: statusClassName(snapshot.status) },
    { label: "Open trades", value: snapshot.openTradesSummary.totalOpenTrades },
    { label: "Pairs", value: snapshot.openTradesSummary.pairCount },
    { label: "Dry-run flag", value: snapshot.dryRun === true ? "true" : "unknown" },
  ];

  return (
    <section className="page">
      <header className="page-header">
        <h1>Dry-run / FreqUI</h1>
        <span className="status-pill">{isLoading ? "Loading" : source}</span>
      </header>
      {error ? <div className="notice">Using fallback data: {error}</div> : null}
      {!isLoading && source === "fallback" && !error ? (
        <div className="notice">Backend API unavailable; showing controlled fallback dry-run data.</div>
      ) : null}
      <section className="dry-run-summary" aria-label="Dry-run summary">
        {summaryRows.map((row) => (
          <article className="metric" key={row.label}>
            <span>{row.label}</span>
            {row.className ? (
              <strong>
                <span className={`run-status ${row.className}`}>{row.value}</span>
              </strong>
            ) : (
              <strong>{row.value}</strong>
            )}
          </article>
        ))}
      </section>
      <section className="dry-run-layout">
        <article className="overview-panel">
          <h2>Runtime Snapshot</h2>
          <dl className="compact-detail-list">
            <div>
              <dt>Profile</dt>
              <dd>{formatValue(snapshot.profileName)}</dd>
            </div>
            <div>
              <dt>Strategy</dt>
              <dd>{formatValue(snapshot.strategyName)}</dd>
            </div>
            <div>
              <dt>Exchange</dt>
              <dd>{formatValue(snapshot.exchange)}</dd>
            </div>
            <div>
              <dt>Pair</dt>
              <dd>{formatValue(snapshot.pair)}</dd>
            </div>
            <div>
              <dt>Timeframe</dt>
              <dd>{formatValue(snapshot.timeframe)}</dd>
            </div>
            <div>
              <dt>Last updated</dt>
              <dd>{formatValue(snapshot.lastUpdated)}</dd>
            </div>
          </dl>
        </article>
        <article className="overview-panel">
          <h2>Balance</h2>
          <dl className="compact-detail-list">
            <div>
              <dt>Currency</dt>
              <dd>{formatValue(snapshot.balanceSummary.currency)}</dd>
            </div>
            <div>
              <dt>Total</dt>
              <dd>{formatNumber(snapshot.balanceSummary.total)}</dd>
            </div>
            <div>
              <dt>Free</dt>
              <dd>{formatNumber(snapshot.balanceSummary.free)}</dd>
            </div>
            <div>
              <dt>Used</dt>
              <dd>{formatNumber(snapshot.balanceSummary.used)}</dd>
            </div>
            <div>
              <dt>Unrealized PnL</dt>
              <dd>{formatNumber(snapshot.balanceSummary.unrealizedProfit)}</dd>
            </div>
          </dl>
        </article>
        <article className="overview-panel">
          <h2>Open Trades</h2>
          <dl className="compact-detail-list">
            <div>
              <dt>Total stake</dt>
              <dd>{formatNumber(snapshot.openTradesSummary.totalStakeAmount)}</dd>
            </div>
            <div>
              <dt>Profit abs</dt>
              <dd>{formatNumber(snapshot.openTradesSummary.totalProfitAbs)}</dd>
            </div>
            <div>
              <dt>Profit pct</dt>
              <dd>{formatNumber(snapshot.openTradesSummary.totalProfitPct)}</dd>
            </div>
            <div>
              <dt>Pairs</dt>
              <dd>
                {snapshot.openTradesSummary.pairs.length
                  ? snapshot.openTradesSummary.pairs.join(", ")
                  : "none"}
              </dd>
            </div>
          </dl>
        </article>
      </section>
      <section className="detail-section">
        <div className="section-header">
          <h2>Artifact Manifest</h2>
          <span>{manifest?.status ?? "none"}</span>
        </div>
        <dl className="detail-list">
          <div>
            <dt>Manifest path</dt>
            <dd>{formatValue(manifest?.manifestPath ?? snapshot.artifactManifestPath)}</dd>
          </div>
          <div>
            <dt>Config path</dt>
            <dd>{formatValue(manifest?.configPath)}</dd>
          </div>
          <div>
            <dt>Return code</dt>
            <dd>{formatValue(manifest?.returnCode)}</dd>
          </div>
          <div>
            <dt>Command shape</dt>
            <dd>{manifest ? `${manifest.commandArgs.length} argument(s), redacted before display` : "none"}</dd>
          </div>
          <div>
            <dt>Reason</dt>
            <dd>{reason}</dd>
          </div>
        </dl>
      </section>
      <section className="detail-section">
        <div className="section-header">
          <h2>FreqUI Link</h2>
          <span>{freqUiLink.accessMode}</span>
        </div>
        <div className="detail-list frequi-panel">
          <div>
            <dt>Environment</dt>
            <dd>{environmentLabel}</dd>
          </div>
          <div>
            <dt>URL</dt>
            <dd>
              <code>{frequiUrl ?? "none"}</code>
            </dd>
          </div>
          <div>
            <dt>Status</dt>
            <dd>{linkEnabled ? "enabled" : freqUiLink.blockedReason ?? "disabled"}</dd>
          </div>
        </div>
        {linkEnabled ? (
          <a className="primary-link frequi-action" href={frequiUrl} target="_blank" rel="noreferrer">
            Open FreqUI
          </a>
        ) : (
          <span className="primary-link disabled-link frequi-action">Unavailable</span>
        )}
      </section>
      <section className="detail-section">
        <div className="section-header">
          <h2>Recent Events</h2>
          <span>{snapshot.recentEvents.length}</span>
        </div>
        {snapshot.recentEvents.length ? (
          <ol className="event-list">
            {snapshot.recentEvents.map((event) => (
              <li key={`${event.timestamp}:${event.eventType}`}>
                <div className="event-heading">
                  <span className={`run-status ${statusClassName(event.severity)}`}>{event.severity}</span>
                  <strong>{event.eventType}</strong>
                  <span>{formatValue(event.timestamp)}</span>
                </div>
                <p>{summarizeText(event.message)}</p>
                <span>{event.source}</span>
              </li>
            ))}
          </ol>
        ) : (
          <div className="empty-state">No dry-run events found.</div>
        )}
      </section>
      <section className="detail-section">
        <div className="section-header">
          <h2>Safety State</h2>
          <span>read-only</span>
        </div>
        <dl className="detail-list">
          <div>
            <dt>Execution controls</dt>
            <dd>unavailable</dd>
          </div>
          <div>
            <dt>Credential values</dt>
            <dd>not rendered</dd>
          </div>
          <div>
            <dt>Fallback source</dt>
            <dd>{source === "fallback" ? "controlled fixture" : "backend API"}</dd>
          </div>
        </dl>
      </section>
    </section>
  );
}
