import type { DryRunArtifactManifest, DryRunStatusSnapshot } from "../api/types";
import { useMvpData } from "../api/useMvpData";
import { formatNumber, reasonText, statusClassName, summarizeText } from "./backtestDisplay";
import { FallbackNotice } from "./FallbackNotice";
import { EMPTY_TEXT, displayDataOrigin, displayLoadState, displayStatus, displayValue } from "./uiCopy";

function getConfiguredFreqUIUrl() {
  const env = (import.meta as ImportMeta & { env?: Record<string, string | undefined> }).env;
  const configuredUrl = env?.VITE_FREQUI_URL?.trim();

  return configuredUrl || null;
}

function formatValue(value: number | string | null | undefined): string {
  return displayValue(value);
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
    { label: "状态", value: displayStatus(snapshot.status), className: statusClassName(snapshot.status) },
    { label: "开放交易", value: snapshot.openTradesSummary.totalOpenTrades },
    { label: "交易对", value: snapshot.openTradesSummary.pairCount },
    { label: "Dry-run 标记", value: snapshot.dryRun === true ? "true" : "未知" },
  ];

  return (
    <section className="page">
      <header className="page-header">
        <h1>Dry-run / FreqUI</h1>
        <span className="status-pill">{displayLoadState(isLoading, source)}</span>
      </header>
      <FallbackNotice
        context="Dry-run / FreqUI 只读运行快照、manifest、余额和安全边界。"
        error={error}
        isLoading={isLoading}
        source={source}
      />
      <section className="dry-run-summary" aria-label="Dry-run 摘要">
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
          <h2>运行快照</h2>
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
              <dt>交易所</dt>
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
              <dt>最后更新</dt>
              <dd>{formatValue(snapshot.lastUpdated)}</dd>
            </div>
          </dl>
        </article>
        <article className="overview-panel">
          <h2>余额</h2>
          <dl className="compact-detail-list">
            <div>
              <dt>币种</dt>
              <dd>{formatValue(snapshot.balanceSummary.currency)}</dd>
            </div>
            <div>
              <dt>总额</dt>
              <dd>{formatNumber(snapshot.balanceSummary.total)}</dd>
            </div>
            <div>
              <dt>可用</dt>
              <dd>{formatNumber(snapshot.balanceSummary.free)}</dd>
            </div>
            <div>
              <dt>占用</dt>
              <dd>{formatNumber(snapshot.balanceSummary.used)}</dd>
            </div>
            <div>
              <dt>未实现 PnL</dt>
              <dd>{formatNumber(snapshot.balanceSummary.unrealizedProfit)}</dd>
            </div>
          </dl>
        </article>
        <article className="overview-panel">
          <h2>开放交易</h2>
          <dl className="compact-detail-list">
            <div>
              <dt>总 stake</dt>
              <dd>{formatNumber(snapshot.openTradesSummary.totalStakeAmount)}</dd>
            </div>
            <div>
              <dt>绝对收益</dt>
              <dd>{formatNumber(snapshot.openTradesSummary.totalProfitAbs)}</dd>
            </div>
            <div>
              <dt>收益率</dt>
              <dd>{formatNumber(snapshot.openTradesSummary.totalProfitPct)}</dd>
            </div>
            <div>
              <dt>交易对</dt>
              <dd>
                {snapshot.openTradesSummary.pairs.length
                  ? snapshot.openTradesSummary.pairs.join(", ")
                  : EMPTY_TEXT}
              </dd>
            </div>
          </dl>
        </article>
      </section>
      <section className="detail-section">
        <div className="section-header">
          <h2>Artifact Manifest</h2>
          <span>{displayStatus(manifest?.status)}</span>
        </div>
        <dl className="detail-list">
          <div>
            <dt>Manifest 路径</dt>
            <dd>{formatValue(manifest?.manifestPath ?? snapshot.artifactManifestPath)}</dd>
          </div>
          <div>
            <dt>Config 路径</dt>
            <dd>{formatValue(manifest?.configPath)}</dd>
          </div>
          <div>
            <dt>返回码</dt>
            <dd>{formatValue(manifest?.returnCode)}</dd>
          </div>
          <div>
            <dt>命令形状</dt>
            <dd>{manifest ? `${manifest.commandArgs.length} 个参数，展示前已脱敏` : EMPTY_TEXT}</dd>
          </div>
          <div>
            <dt>原因</dt>
            <dd>{reason}</dd>
          </div>
        </dl>
      </section>
      <section className="detail-section">
        <div className="section-header">
          <h2>FreqUI 链接</h2>
          <span>{freqUiLink.accessMode}</span>
        </div>
        <div className="detail-list frequi-panel">
          <div>
            <dt>环境</dt>
            <dd>{environmentLabel}</dd>
          </div>
          <div>
            <dt>URL</dt>
            <dd>
              <code>{frequiUrl ?? EMPTY_TEXT}</code>
            </dd>
          </div>
          <div>
            <dt>状态</dt>
            <dd>{linkEnabled ? "已启用" : freqUiLink.blockedReason ?? "已停用"}</dd>
          </div>
        </div>
        {linkEnabled ? (
          <a className="primary-link frequi-action" href={frequiUrl} target="_blank" rel="noreferrer">
            打开 FreqUI
          </a>
        ) : (
          <span className="primary-link disabled-link frequi-action">不可用</span>
        )}
      </section>
      <section className="detail-section">
        <div className="section-header">
          <h2>最近事件</h2>
          <span>{snapshot.recentEvents.length}</span>
        </div>
        {snapshot.recentEvents.length ? (
          <ol className="event-list">
            {snapshot.recentEvents.map((event) => (
              <li key={`${event.timestamp}:${event.eventType}`}>
                <div className="event-heading">
                  <span className={`run-status ${statusClassName(event.severity)}`}>
                    {displayStatus(event.severity)}
                  </span>
                  <strong>{event.eventType}</strong>
                  <span>{formatValue(event.timestamp)}</span>
                </div>
                <p>{summarizeText(event.message)}</p>
                <span>{event.source}</span>
              </li>
            ))}
          </ol>
        ) : (
          <div className="empty-state">暂无 Dry-run 事件。</div>
        )}
      </section>
      <section className="detail-section">
        <div className="section-header">
          <h2>安全状态</h2>
          <span>只读</span>
        </div>
        <dl className="detail-list">
          <div>
            <dt>执行控制</dt>
            <dd>不可用</dd>
          </div>
          <div>
            <dt>密钥值</dt>
            <dd>不渲染</dd>
          </div>
          <div>
            <dt>Fallback 来源</dt>
            <dd>{displayDataOrigin(source)}</dd>
          </div>
        </dl>
      </section>
    </section>
  );
}
