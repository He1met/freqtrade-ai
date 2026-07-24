import type {
  DryRunArtifactManifest,
  DryRunEventSummary,
  DryRunStatusSnapshot,
  RuntimeSafetyBoundary,
} from "../api/types";
import { combineDataSources } from "../api/sourceState";
import { useMvpData } from "../api/useMvpData";
import {
  CompactText,
  CopyableValue,
  EmptyState,
  ExpandableText,
  PageHeader,
  StatusBadge,
} from "../components/DisplayPrimitives";
import "../styles/freq-ui.css";
import { FallbackNotice } from "./FallbackNotice";
import {
  dryRunDisplayConclusion,
  redactCommandArgs,
  redactFreqUiText,
  safeFreqUiLink,
  safetyBoundarySummary,
} from "./freqUiDisplay";
import {
  EMPTY_TEXT,
  displayDataOrigin,
  displayLoadState,
  displayNumber,
  displayValue,
} from "./uiCopy";

function metricValue(value: number | null, suffix = ""): string {
  return value === null ? EMPTY_TEXT : `${displayNumber(value, { maximumFractionDigits: 4 })}${suffix}`;
}

function SnapshotDetails({ snapshot }: { snapshot: DryRunStatusSnapshot }) {
  const fields = [
    ["Profile", snapshot.profileName],
    ["Strategy", snapshot.strategyName],
    ["交易所", snapshot.exchange],
    ["Pair", snapshot.pair],
    ["Timeframe", snapshot.timeframe],
    ["最后更新", snapshot.lastUpdated],
  ] as const;
  return (
    <section className="freq-ui-panel">
      <h2>运行快照</h2>
      <dl className="freq-ui-facts">
        {fields.map(([label, value]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd><CopyableValue label={label} value={redactFreqUiText(value)} /></dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

function BalanceDetails({
  snapshot,
  verified,
}: {
  snapshot: DryRunStatusSnapshot;
  verified: boolean;
}) {
  const balance = snapshot.balanceSummary;
  return (
    <section className="freq-ui-panel">
      <h2>余额摘要</h2>
      {verified ? (
        <>
          <div className="freq-ui-balance-total">
            <span>{displayValue(balance.currency)}</span>
            <strong>{metricValue(balance.total)}</strong>
          </div>
          <dl className="freq-ui-facts compact">
            <div><dt>可用</dt><dd>{metricValue(balance.free)}</dd></div>
            <div><dt>占用</dt><dd>{metricValue(balance.used)}</dd></div>
            <div><dt>已实现 PnL</dt><dd>{metricValue(balance.realizedProfit)}</dd></div>
            <div><dt>未实现 PnL</dt><dd>{metricValue(balance.unrealizedProfit)}</dd></div>
          </dl>
        </>
      ) : (
        <EmptyState description="仅在真实 API 快照正常加载后展示余额。" title="余额尚不可确认" />
      )}
    </section>
  );
}

function TradeDetails({
  snapshot,
  verified,
}: {
  snapshot: DryRunStatusSnapshot;
  verified: boolean;
}) {
  const trades = snapshot.openTradesSummary;
  return (
    <section className="freq-ui-panel">
      <div className="freq-ui-panel-heading">
        <h2>开放交易摘要</h2>
        {verified ? (
          <StatusBadge
            label={trades.totalOpenTrades === 0 ? "真实空交易" : `${trades.totalOpenTrades} 笔`}
            status={trades.totalOpenTrades === 0 ? "EMPTY" : "RUNNING"}
          />
        ) : (
          <StatusBadge label="尚不可确认" status="UNAVAILABLE" />
        )}
      </div>
      {verified ? (
        <>
          <dl className="freq-ui-facts compact">
            <div><dt>交易对数</dt><dd>{trades.pairCount}</dd></div>
            <div><dt>总 Stake</dt><dd>{metricValue(trades.totalStakeAmount)}</dd></div>
            <div><dt>绝对收益</dt><dd>{metricValue(trades.totalProfitAbs)}</dd></div>
            <div><dt>收益率</dt><dd>{metricValue(trades.totalProfitPct, "%")}</dd></div>
          </dl>
          {trades.pairs.length > 0 ? (
            <CopyableValue label="开放交易对" value={trades.pairs.map(redactFreqUiText).join(", ")} />
          ) : (
            <span className="freq-ui-muted">真实快照中没有开放交易对。</span>
          )}
        </>
      ) : (
        <EmptyState description="未加载、失败或非真实来源不能解释为 0 笔交易。" title="交易数量尚不可确认" />
      )}
    </section>
  );
}

function ArtifactDetails({ manifest, snapshot }: {
  manifest: DryRunArtifactManifest | null;
  snapshot: DryRunStatusSnapshot;
}) {
  const commandArgs = redactCommandArgs(manifest?.commandArgs ?? []);
  const reason = redactFreqUiText(
    snapshot.blockedReason ??
      manifest?.blockedReason ??
      snapshot.failedReason ??
      manifest?.failedReason ??
      snapshot.skippedReason ??
      manifest?.skippedReason,
  );
  return (
    <details className="freq-ui-detail-group">
      <summary>
        <span><strong>Artifact 与命令参数</strong><small>所有内容展示前再次脱敏</small></span>
        <StatusBadge showRaw status={manifest?.status ?? "UNAVAILABLE"} />
      </summary>
      <div className="freq-ui-detail-content">
        <dl className="freq-ui-facts">
          <div>
            <dt>Manifest 路径</dt>
            <dd><CopyableValue label="Manifest 路径" value={redactFreqUiText(manifest?.manifestPath ?? snapshot.artifactManifestPath)} /></dd>
          </div>
          <div>
            <dt>Config 路径</dt>
            <dd><CopyableValue label="Config 路径" value={redactFreqUiText(manifest?.configPath)} /></dd>
          </div>
          <div><dt>返回码</dt><dd>{displayValue(manifest?.returnCode)}</dd></div>
          <div><dt>命令参数数</dt><dd>{commandArgs.length}</dd></div>
        </dl>
        <section className="freq-ui-expand-block">
          <h3>脱敏命令参数</h3>
          <CopyableValue label="脱敏命令参数" value={commandArgs.join("\n") || EMPTY_TEXT} />
          <ExpandableText mono summary="展开全部参数" value={commandArgs.join("\n")} />
        </section>
        {reason ? <ExpandableText summary="展开运行原因" value={reason} /> : null}
      </div>
    </details>
  );
}

function EventsPanel({ events }: { events: DryRunEventSummary[] }) {
  return (
    <details className="freq-ui-detail-group">
      <summary>
        <span><strong>最近事件</strong><small>事件正文和来源均已脱敏</small></span>
        <span>{events.length} 项</span>
      </summary>
      <div className="freq-ui-detail-content">
        {events.length === 0 ? (
          <EmptyState description="已加载的快照没有最近事件记录。" title="暂无 Dry-run 事件" />
        ) : (
          <ol className="freq-ui-event-list">
            {events.map((event, index) => {
              const message = redactFreqUiText(event.message);
              const source = redactFreqUiText(event.source);
              return (
                <li key={`${event.timestamp}:${event.eventType}:${index}`}>
                  <div className="freq-ui-event-heading">
                    <StatusBadge showRaw status={event.severity} />
                    <strong><CompactText label="事件类型" value={redactFreqUiText(event.eventType)} /></strong>
                    <span>{displayValue(event.timestamp)}</span>
                  </div>
                  <ExpandableText summary="展开事件正文" value={message} />
                  <div className="freq-ui-event-copy">
                    <CopyableValue label="事件正文" value={message} />
                    <CopyableValue label="事件来源" value={source} />
                  </div>
                </li>
              );
            })}
          </ol>
        )}
      </div>
    </details>
  );
}

function SafetyDetails({ safety }: { safety: RuntimeSafetyBoundary }) {
  const boundary = safetyBoundarySummary(safety);
  return (
    <details className="freq-ui-detail-group">
      <summary>
        <span><strong>安全边界明细</strong><small>复用 Operator Runtime Contract</small></span>
        <StatusBadge
          label={boundary.status === "READY" ? "只读边界正常" : "安全边界异常"}
          status={boundary.status}
        />
      </summary>
      <div className="freq-ui-detail-content">
        <dl className="freq-ui-safety-grid">
          <div><dt>只读</dt><dd>{safety.readOnly ? "是" : "否"}</dd></div>
          <div><dt>Live trading</dt><dd>{boundary.liveLabel}</dd></div>
          <div><dt>真实订单</dt><dd>{boundary.realOrdersLabel}</dd></div>
          <div><dt>交易所连接</dt><dd>{safety.allowExchangeConnection ? "允许" : "禁止"}</dd></div>
          <div><dt>部署控制</dt><dd>{safety.allowDeployControl ? "允许" : "禁止"}</dd></div>
          <div><dt>启动 / 停止 Bot</dt><dd>{safety.canStartStopBot ? "允许" : "禁止"}</dd></div>
        </dl>
        {boundary.violations.length > 0 ? (
          <div className="freq-ui-alert" role="alert">{boundary.violations.join("；")}</div>
        ) : null}
        <ExpandableText summary="展开完整安全边界" value={redactFreqUiText(safety.boundary)} />
      </div>
    </details>
  );
}

export function FreqUILink() {
  const { data, sources, isLoading, error } = useMvpData();
  const source = combineDataSources(sources, ["dryRun", "operatorDashboard"]);
  const dryRunSource = sources.dryRun;
  const { manifest, snapshot, freqUiLink } = data.dryRun;
  const runtimeSafety = data.operatorDashboard.runtimeContract.safety;
  const conclusion = dryRunDisplayConclusion({
    error,
    isLoading,
    manifest,
    snapshot,
    source: dryRunSource,
  });
  const boundary = safetyBoundarySummary(runtimeSafety);
  const link = safeFreqUiLink(freqUiLink, runtimeSafety, source);
  const verifiedSnapshot = conclusion.state === "REAL_EMPTY" || conclusion.state === "NORMAL";

  return (
    <section className="page freq-ui-page">
      <PageHeader
        description="查看真实 Dry-run 快照、余额、开放交易与 Backend 管理的只读 FreqUI 入口。"
        eyebrow="本地运行观察"
        status={<span className="status-pill">{displayLoadState(isLoading, source)}</span>}
        title="Dry-run / FreqUI"
      />
      <FallbackNotice
        context="Dry-run / FreqUI 只读运行快照、manifest、余额和 Operator 安全边界。"
        error={error}
        isLoading={isLoading}
        source={source}
      />

      <section className={`freq-ui-conclusion state-${conclusion.state.toLowerCase()}`}>
        <div>
          <span>当前结论</span>
          <h2>{conclusion.title}</h2>
          <p>{conclusion.reason}</p>
        </div>
        <StatusBadge showRaw status={conclusion.status} />
      </section>

      <section className="freq-ui-summary-grid" aria-label="Dry-run 与 FreqUI 首屏摘要">
        <article>
          <span>Dry-run</span>
          <strong>{snapshot.dryRun === true ? "已确认" : "未确认"}</strong>
          <StatusBadge status={snapshot.dryRun === true ? "READY" : "BLOCKED"} />
        </article>
        <article className={runtimeSafety.allowLiveTrading ? "is-problem" : undefined}>
          <span>Live trading</span>
          <strong>{boundary.liveLabel}</strong>
          <StatusBadge status={runtimeSafety.allowLiveTrading ? "FAILED" : "READY"} />
        </article>
        <article className={runtimeSafety.allowRealOrders ? "is-problem" : undefined}>
          <span>真实订单</span>
          <strong>{boundary.realOrdersLabel}</strong>
          <StatusBadge status={runtimeSafety.allowRealOrders ? "FAILED" : "READY"} />
        </article>
        <article className={!link.enabled ? "is-problem" : undefined}>
          <span>FreqUI 可用性</span>
          <strong>{link.enabled ? "可打开" : "不可用"}</strong>
          <StatusBadge showRaw status={link.status} />
        </article>
        <article>
          <span>余额</span>
          <strong>{verifiedSnapshot ? metricValue(snapshot.balanceSummary.total) : EMPTY_TEXT}</strong>
          <CompactText
            label="余额币种"
            value={verifiedSnapshot ? snapshot.balanceSummary.currency : "尚不可确认"}
          />
        </article>
        <article>
          <span>开放交易</span>
          <strong>{verifiedSnapshot ? snapshot.openTradesSummary.totalOpenTrades : EMPTY_TEXT}</strong>
          <span className="freq-ui-muted">
            {verifiedSnapshot ? `${snapshot.openTradesSummary.pairCount} 个交易对` : "尚不可确认"}
          </span>
        </article>
      </section>

      <section className={`freq-ui-boundary ${boundary.status === "FAILED" ? "is-problem" : ""}`}>
        <div>
          <span>Operator Runtime Contract 安全边界</span>
          <strong>{boundary.dryRunLabel}</strong>
          <p>{boundary.violations.length > 0 ? boundary.violations.join("；") : runtimeSafety.boundary}</p>
        </div>
        <span>页面来源：{displayDataOrigin(source)}</span>
      </section>

      <section className="freq-ui-main-grid">
        <SnapshotDetails snapshot={snapshot} />
        <BalanceDetails snapshot={snapshot} verified={verifiedSnapshot} />
        <TradeDetails snapshot={snapshot} verified={verifiedSnapshot} />
      </section>

      <section className="freq-ui-panel freq-ui-link-panel">
        <div>
          <div className="freq-ui-panel-heading">
            <h2>FreqUI 入口</h2>
            <StatusBadge showRaw status={link.status} />
          </div>
          <p>{redactFreqUiText(link.reason)}</p>
          <dl className="freq-ui-facts">
            <div><dt>环境</dt><dd>{redactFreqUiText(freqUiLink.environmentLabel)}</dd></div>
            <div><dt>访问模式</dt><dd>{freqUiLink.accessMode}</dd></div>
          </dl>
          <CopyableValue label="脱敏 FreqUI URL" value={link.displayUrl} />
          <ExpandableText mono summary="展开脱敏 URL" value={link.displayUrl} />
        </div>
        {link.enabled && link.href ? (
          <a className="primary-link freq-ui-open-link" href={link.href} rel="noreferrer" target="_blank">
            打开 FreqUI
          </a>
        ) : (
          <span aria-disabled="true" className="primary-link disabled-link freq-ui-open-link">不可用</span>
        )}
      </section>

      <ArtifactDetails manifest={manifest} snapshot={snapshot} />
      <EventsPanel events={snapshot.recentEvents} />
      <SafetyDetails safety={runtimeSafety} />
    </section>
  );
}
