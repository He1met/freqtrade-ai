import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";

import {
  fetchOkxDemoObservability,
  type OkxDemoObservability,
  type OkxDemoOrder,
} from "../api/okxDemoApi";
import { useFormalReadModels } from "../api/useFormalReadModels";
import {
  CompactText,
  CopyableValue,
  FormalLoadingState,
  PageHeader,
  StatusBadge,
} from "../components/DisplayPrimitives";
import "../styles/okx-demo.css";
import { okxDemoAcceptanceIsTruthful, orderCanDisplayComplete } from "./okxDemoDisplay";
import { displayDateTime, displayStatus, displayValue } from "./uiCopy";

function displayDecimal(
  value: string | null | undefined,
  maximumFractionDigits = 8,
): string {
  if (value === null || value === undefined || value === "") return "暂无";
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return value;
  return new Intl.NumberFormat("zh-CN", {
    maximumFractionDigits,
    minimumFractionDigits: 0,
    useGrouping: true,
  }).format(parsed);
}

function DetailValue({
  label,
  value,
}: {
  label: string;
  value: string | number | boolean | null | undefined;
}) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{displayValue(value)}</dd>
    </div>
  );
}

function OrderDetail({
  data,
  order,
}: {
  data: OkxDemoObservability;
  order: OkxDemoOrder;
}) {
  const intent = data.intents.find((item) => item.databaseId === order.tradeIntentDatabaseId);
  const lineage = data.lineage.find(
    (item) => item.tradeIntentDatabaseId === order.tradeIntentDatabaseId,
  );
  const complete = orderCanDisplayComplete(order, data);
  return (
    <aside className="okx-detail" aria-label="订单详情" id="okx-order-detail">
      <header>
        <div>
          <span className="okx-kicker">订单详情</span>
          <h2>{order.instrumentId ?? "合约未知"}</h2>
        </div>
        <StatusBadge status={complete ? "COMPLETE" : "INCOMPLETE"} showRaw />
      </header>
      <p className={complete ? "okx-conclusion okx-conclusion-ready" : "okx-conclusion"}>
        {order.completionReason}
      </p>

      <section>
        <h3>订单结论</h3>
        <dl className="okx-detail-grid">
          <DetailValue label="状态" value={order.status} />
          <DetailValue label="权威状态" value={order.authoritativeStatus} />
          <DetailValue label="方向" value={order.side} />
          <DetailValue label="类型" value={order.orderType} />
          <DetailValue label="数量" value={displayDecimal(order.quantity)} />
          <DetailValue label="已成交" value={displayDecimal(order.filledQuantity)} />
          <DetailValue label="更新时间" value={displayDateTime(order.updatedAt)} />
        </dl>
      </section>

      <section>
        <h3>风控结论</h3>
        <dl className="okx-detail-grid">
          <DetailValue label="意图状态" value={intent?.status} />
          <DetailValue label="风控决定" value={order.riskDecision?.decision} />
          <DetailValue label="策略版本" value={order.riskDecision?.policyVersion} />
        </dl>
        {order.riskDecision?.reason ? (
          <p className="okx-action-reason">{order.riskDecision.reason}</p>
        ) : null}
      </section>

      <section>
        <h3>成交</h3>
        {order.fills.length ? (
          <div className="okx-fill-list">
            {order.fills.map((fill) => (
              <article key={fill.databaseId}>
                <strong>{displayDecimal(fill.quantity)} @ {displayDecimal(fill.price)}</strong>
                <span>手续费 {displayDecimal(fill.fee)}</span>
                <CompactText label="成交 ID" value={fill.exchangeFillId} />
              </article>
            ))}
          </div>
        ) : (
          <p className="okx-muted">尚无成交数据库记录；不能把订单提交视为成交。</p>
        )}
      </section>

      <details className="formal-disclosure okx-technical-evidence">
        <summary>查看技术证据与完整链路</summary>
        <section>
          <h3>标识与数据库证据</h3>
          <dl className="okx-detail-grid">
            <DetailValue label="订单数据库 ID" value={order.databaseId} />
            <DetailValue label="完整链 DB ID" value={order.fullChainDatabaseId} />
            <DetailValue label="权威快照 DB ID" value={order.authoritativeSnapshotDatabaseId} />
            <DetailValue label="权威事件 DB ID" value={order.authoritativeEventDatabaseId} />
            <DetailValue label="TradeIntent DB ID" value={intent?.databaseId} />
            <DetailValue label="策略版本 ID" value={intent?.strategyVersionId} />
            <DetailValue label="风控 DB ID" value={order.riskDecision?.databaseId} />
          </dl>
          <CopyableValue label="交易所订单 ID" value={order.exchangeOrderId ?? "未提供"} />
          <CopyableValue label="客户端订单 ID" value={order.clientOrderId} />
          <CopyableValue label="TradeIntent ID" value={intent?.intentId ?? "未提供"} />
          <h3>证据链</h3>
          <div className="okx-lineage">
          <span>Chain #{lineage?.fullChainDatabaseId ?? "—"}</span>
          <span aria-hidden="true">→</span>
          <span>Strategy #{lineage?.strategyVersionDatabaseId ?? "—"}</span>
          <span aria-hidden="true">→</span>
          <span>Backtest #{lineage?.backtestResultDatabaseId ?? "—"}</span>
          <span aria-hidden="true">→</span>
          <span>Score #{lineage?.strategyScoreDatabaseId ?? "—"}</span>
          <span aria-hidden="true">→</span>
          <span>Candidate #{lineage?.candidateApprovalDatabaseId ?? "—"}</span>
          <span aria-hidden="true">→</span>
          <span>Signal #{lineage?.signalSnapshotDatabaseId ?? "—"}</span>
          <span aria-hidden="true">→</span>
          <span>Intent #{lineage?.tradeIntentDatabaseId ?? "—"}</span>
          <span aria-hidden="true">→</span>
          <span>Risk #{lineage?.riskDecisionDatabaseId ?? "—"}</span>
          <span aria-hidden="true">→</span>
          <span>Order #{lineage?.orderDatabaseId ?? "—"}</span>
          <span aria-hidden="true">→</span>
          <span>Fill #{lineage?.fillDatabaseId ?? "—"}</span>
          <span aria-hidden="true">→</span>
          <span>Snapshot #{lineage?.authoritativeOrderSnapshotDatabaseId ?? "—"}</span>
          <span aria-hidden="true">→</span>
          <span>Reconcile #{lineage?.reconciliationDatabaseId ?? "—"}</span>
          </div>
        </section>
      </details>
    </aside>
  );
}

export function OkxDemo() {
  const [searchParams] = useSearchParams();
  const { runtimeActivity, refresh } = useFormalReadModels();
  const [data, setData] = useState<OkxDemoObservability | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    fetchOkxDemoObservability(controller.signal)
      .then((result) => {
        setData(result);
        setSelectedId((current) => current ?? result.orders[0]?.databaseId ?? null);
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) {
          setError(reason instanceof Error ? reason.message : String(reason));
        }
      });
    return () => controller.abort();
  }, [revision]);

  const selectedOrder = useMemo(
    () => data?.orders.find((order) => order.databaseId === selectedId) ?? data?.orders[0] ?? null,
    [data, selectedId],
  );

  if (error) {
    return (
      <div className="page formal-page okx-demo-page">
        <PageHeader
          eyebrow="正式工作台"
          title="模拟盘"
          description="查看 OKX_DEMO 的运行健康、意图、订单、成交与对账证据。"
          status={<span className="formal-target-chip">OKX_DEMO · Demo-only</span>}
        />
        <section className="formal-conclusion" data-state="blocked" role="alert">
          <div>
            <span className="formal-kicker">数据不可用</span>
            <h2>暂时无法确认模拟盘状态</h2>
            <p>{error}。没有读取到真实数据库证据，页面不会把未知状态显示为成功。</p>
          </div>
          <StatusBadge status="UNKNOWN" label="未知" />
        </section>
        <section className="formal-panel">
          <div className="formal-section-heading compact"><h2>运行策略与信号（独立只读来源）</h2><StatusBadge status={runtimeActivity.error ? "UNKNOWN" : "READY"} /></div>
          {runtimeActivity.loading ? <FormalLoadingState label="正在读取运行投影" />
            : runtimeActivity.error ? <p className="formal-problem">运行投影同样不可用，状态未知。</p>
              : <dl className="formal-summary-list">
                <div><dt>ACTIVE 运行策略</dt><dd>{runtimeActivity.data?.active_deployments.length ?? 0}</dd></div>
                <div><dt>最近信号</dt><dd>{runtimeActivity.data?.recent_signal_evaluations[0] ? displayStatus(runtimeActivity.data.recent_signal_evaluations[0].status) : "当前无信号"}</dd></div>
              </dl>}
        </section>
        <button className="formal-primary-button" onClick={() => { setError(null); setData(null); setRevision((value) => value + 1); refresh(); }} type="button">重新读取只读证据</button>
      </div>
    );
  }
  if (!data) {
    return (
      <div className="page formal-page okx-demo-page">
        <PageHeader eyebrow="正式工作台" title="模拟盘" />
        <FormalLoadingState className="formal-conclusion" label="正在读取模拟盘证据" />
      </div>
    );
  }

  const acceptable = okxDemoAcceptanceIsTruthful(data);
  const fillCount = data.orders.reduce((total, order) => total + order.fills.length, 0);
  const riskDecisionCount = data.orders.filter((order) => order.riskDecision).length;
  const readyCount = data.readiness.filter((check) => check.status === "READY").length;
  const readinessState = data.readiness.length > 0 && readyCount === data.readiness.length
    ? "READY"
    : "BLOCKED";
  return (
    <div className="page formal-page okx-demo-page">
      <PageHeader
        eyebrow="正式工作台"
        title="模拟盘"
        description="只读查看 OKX_DEMO 运行、订单与成交证据；页面不会启动交易。"
        status={<span className="formal-target-chip">OKX_DEMO · Demo-only · real_orders=false</span>}
      />

      {searchParams.get("from") === "freq-ui" ? (
        <aside className="formal-context-banner" data-kind="compatibility">
          <div><strong>旧 FreqUI 兼容入口</strong><span>该入口已归并到正式模拟盘页面；这里仅展示 Freqtrade AI 的只读证据。</span></div>
        </aside>
      ) : null}

      <section className="formal-conclusion" data-state={acceptable ? "ready" : "attention"}>
        <div>
          <span className="formal-kicker">当前结论</span>
          <h2>{acceptable ? "模拟盘证据完整，可继续观察" : "模拟盘证据尚未满足严格验收"}</h2>
          <p>{data.acceptanceReason}</p>
        </div>
        <StatusBadge status={acceptable ? "ACCEPTABLE" : "NOT_ACCEPTABLE"} />
      </section>

      <section className="formal-metric-grid" aria-label="模拟盘关键指标">
        <article className={runtimeActivity.loading ? "formal-metric formal-skeleton" : "formal-metric"} data-state={runtimeActivity.error ? "unknown" : undefined}>
          <span>运行中策略</span>
          <strong>{runtimeActivity.loading ? "…" : runtimeActivity.error ? "—" : runtimeActivity.data?.active_deployments.length ?? 0}</strong>
          <small>{runtimeActivity.error ? "部署投影读取失败，保持未知" : runtimeActivity.data?.active_deployments.length ? "OKX_DEMO ACTIVE deployment" : "尚未部署运行策略"}</small>
        </article>
        <article className={runtimeActivity.loading ? "formal-metric formal-skeleton" : "formal-metric"} data-state={runtimeActivity.error ? "unknown" : undefined}>
          <span>最近信号</span>
          <strong>{runtimeActivity.loading ? "…" : runtimeActivity.error ? "—" : runtimeActivity.data?.recent_signal_evaluations[0] ? displayStatus(runtimeActivity.data.recent_signal_evaluations[0].status) : "无"}</strong>
          <small>{runtimeActivity.error ? "信号投影读取失败，保持未知" : runtimeActivity.data?.recent_signal_evaluations.length ? displayDateTime(runtimeActivity.data.recent_signal_evaluations[0].closed_candle_at) : "当前没有 signal evaluation"}</small>
        </article>
        <article className="formal-metric">
          <span>订单记录</span><strong>{data.orders.length}</strong><small>最近聚合窗口内的数据库订单</small>
        </article>
        <article className="formal-metric">
          <span>成交记录</span><strong>{fillCount}</strong><small>订单附带的真实成交数据库记录</small>
        </article>
      </section>

      <section className="formal-panel" aria-labelledby="demo-flow-title">
        <div className="formal-section-heading">
          <div><span className="formal-kicker">执行链路</span><h2 id="demo-flow-title">最近证据覆盖</h2></div>
          <span className="formal-section-note">更新于 {displayDateTime(data.generatedAt)}</span>
        </div>
        <div className="formal-demo-flow">
          <div><span>信号</span><strong>{runtimeActivity.loading ? "读取中" : runtimeActivity.error ? "未知" : `${runtimeActivity.data?.recent_signal_evaluations.length ?? 0} 条`}</strong></div>
          <div><span>交易意图</span><strong>{data.intents.length} 条</strong></div>
          <div><span>风控决定</span><strong>{riskDecisionCount} 条</strong></div>
          <div><span>订单</span><strong>{data.orders.length} 条</strong></div>
          <div><span>成交</span><strong>{fillCount} 条</strong></div>
          <div><span>对账</span><strong>{data.latestReconciliation?.status ?? "暂无"}</strong></div>
        </div>
        <p className="formal-muted">订单提交不等于成交；缺少信号或部署读取 DTO 时保持“未知”，不从订单反推运行成功。</p>
      </section>

      <section className="formal-panel" aria-labelledby="demo-runtime-title">
        <div className="formal-section-heading">
          <div><span className="formal-kicker">运行读模型</span><h2 id="demo-runtime-title">ACTIVE 策略与最近信号</h2></div>
          <span className="formal-section-note">{runtimeActivity.data ? `更新于 ${displayDateTime(runtimeActivity.data.as_of)}` : "独立只读来源"}</span>
        </div>
        {runtimeActivity.loading ? <FormalLoadingState label="正在读取运行投影" />
          : runtimeActivity.error ? <p className="formal-problem">运行投影读取失败：{runtimeActivity.error}。订单证据仍可独立展示。</p>
            : runtimeActivity.data?.active_deployments.length ? (
              <div className="formal-demo-flow">
                {runtimeActivity.data.active_deployments.map((deployment) => (
                  <div key={deployment.deployment_id}>
                    <span>槽位 {deployment.active_slot} · {deployment.instrument_id}</span>
                    <strong>{deployment.strategy_name} · v{deployment.strategy_version_number}</strong>
                    <small>{deployment.timeframe} · Approval #{deployment.candidate_approval_id}</small>
                  </div>
                ))}
              </div>
            ) : <div className="okx-empty"><strong>尚未部署运行策略</strong><p>查询成功且没有 OKX_DEMO ACTIVE deployment；这不是 API 读取失败。</p></div>}
        {!runtimeActivity.loading && !runtimeActivity.error && runtimeActivity.data ? (
          runtimeActivity.data.recent_signal_evaluations.length ? (
            <ol className="formal-activity-list">
              {runtimeActivity.data.recent_signal_evaluations.slice(0, 5).map((evaluation) => (
                <li key={evaluation.evaluation_id}>
                  <StatusBadge status={evaluation.status} />
                  <div><strong>{evaluation.instrument_id} · {evaluation.timeframe}</strong><span>{displayDateTime(evaluation.closed_candle_at)}{evaluation.error_code ? ` · ${evaluation.error_code}` : ""}</span></div>
                </li>
              ))}
            </ol>
          ) : <div className="okx-empty"><strong>当前无信号评估记录</strong><p>可能尚无 ACTIVE 部署或尚未到闭合 candle；不会从订单反推信号。</p></div>
        ) : null}
      </section>

      <details className="formal-panel okx-readiness-disclosure">
        <summary>
          <span><strong>运行准备度</strong><small>{readyCount}/{data.readiness.length} 项就绪</small></span>
          <StatusBadge status={readinessState} />
        </summary>
        <div className="okx-readiness-grid">
          {data.readiness.map((check) => (
            <article key={check.key}>
              <header><strong>{check.label}</strong><StatusBadge status={check.status} /></header>
              <p>{check.summary}</p>
              {check.action ? <small>{check.action}</small> : null}
            </article>
          ))}
        </div>
      </details>

      <section className="okx-workspace" aria-label="订单主从工作区">
        <div className="okx-master">
          <div className="formal-section-heading">
            <div>
              <span className="formal-kicker">最近活动</span>
              <h2>订单与完成判定</h2>
            </div>
            <span className="formal-section-note">{data.orders.length} 条</span>
          </div>
          {data.orders.length ? (
            <div
              aria-label="订单表，可横向滚动查看全部列"
              className="okx-order-table-wrap"
              role="region"
              tabIndex={0}
            >
              <p className="okx-scroll-hint">横向滚动查看全部订单字段</p>
              <table className="okx-order-table">
                <thead>
                  <tr>
                    <th>合约</th>
                    <th>方向 / 类型</th>
                    <th>数量</th>
                    <th>订单状态</th>
                    <th>完成判定</th>
                    <th>交易所订单 ID</th>
                  </tr>
                </thead>
                <tbody>
                  {data.orders.map((order) => (
                    <tr
                      aria-selected={selectedOrder?.databaseId === order.databaseId}
                      key={order.databaseId}
                    >
                      <td>
                        <button
                          aria-controls="okx-order-detail"
                          aria-pressed={selectedOrder?.databaseId === order.databaseId}
                          className="okx-order-select"
                          onClick={() => setSelectedId(order.databaseId)}
                          type="button"
                        >
                          <strong>{order.instrumentId ?? "未提供"}</strong><small>DB #{order.databaseId}</small>
                        </button>
                      </td>
                      <td>{order.side ?? "—"} / {order.orderType ?? "—"}</td>
                      <td>{displayDecimal(order.quantity)}</td>
                      <td><StatusBadge status={order.authoritativeStatus ?? order.status} showRaw /></td>
                      <td><StatusBadge status={orderCanDisplayComplete(order, data) ? "COMPLETE" : "INCOMPLETE"} showRaw /></td>
                      <td><CompactText label="交易所订单 ID" value={order.exchangeOrderId ?? "未提供"} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="okx-empty">
              <strong>暂无订单数据库记录</strong>
              <p>等待模拟盘产生并持久化订单证据；空结果不代表运行成功，也不能进入验收。</p>
            </div>
          )}
        </div>
        {selectedOrder ? <OrderDetail data={data} order={selectedOrder} /> : null}
      </section>

      <section className="okx-secondary-grid">
        <article>
          <div className="formal-section-heading compact">
            <h2>仓位</h2><span>{data.positions.length} 条</span>
          </div>
          {data.positions.length ? (
            <dl className="okx-position-list">
              {data.positions.map((position) => (
                <div key={position.databaseId}>
                  <dt>{position.instrumentId} · {position.positionSide}</dt>
                  <dd>{displayDecimal(position.quantity)} @ {displayDecimal(position.averagePrice)}</dd>
                  <small>{displayDateTime(position.observedAt)}</small>
                </div>
              ))}
            </dl>
          ) : <p className="okx-muted">暂无仓位数据库记录。</p>}
        </article>
        <article>
          <div className="formal-section-heading compact"><h2>账户</h2><StatusBadge status={data.account.status} /></div>
          <p>{data.account.reason}</p>
          {data.account.databaseId ? (
            <dl className="okx-detail-grid">
              <DetailValue label="数据库 ID" value={data.account.databaseId} />
              <DetailValue label="事件 DB ID" value={data.account.eventDatabaseId} />
              <DetailValue label="权益" value={displayDecimal(data.account.equity, 2)} />
              <DetailValue label="可用余额" value={displayDecimal(data.account.availableBalance, 2)} />
              <DetailValue label="保证金余额" value={displayDecimal(data.account.marginBalance, 2)} />
              <DetailValue label="观测时间" value={displayDateTime(data.account.observedAt)} />
            </dl>
          ) : null}
        </article>
        <article>
          <div className="formal-section-heading compact"><h2>最近对账</h2>
            <StatusBadge status={data.latestReconciliation?.status ?? "UNKNOWN"} />
          </div>
          {data.latestReconciliation ? (
            <>
              <p>DB #{data.latestReconciliation.databaseId} · {displayDateTime(data.latestReconciliation.completedAt)}</p>
              <p>State #{data.latestReconciliation.stateDatabaseId} · 权威状态 {displayDateTime(data.latestReconciliation.authoritativeObservedAt)}</p>
              {data.latestReconciliation.reason ? <p className="okx-action-reason">{data.latestReconciliation.reason}</p> : null}
            </>
          ) : <p className="okx-muted">没有对账数据库记录，不能验收。</p>}
        </article>
      </section>

      <details className="formal-panel okx-live-readiness">
        <summary>
          <span><strong>未来 Live 受控上线证据</strong><small>只读规划状态，不提供真实资金操作</small></span>
          <StatusBadge status="UNKNOWN" label="Live 未配置" />
        </summary>
        <div className="okx-live-grid">
          <p>Demo 与 Live 必须使用严格隔离的 execution target、凭据、数据与审计域，不能在本页直接切换。</p>
          <p>持续表现、独立验证、运行健康、对账、风险限额、账户权限、人工审批和回滚演练等证据尚未由当前接口提供。</p>
          <p>本阶段没有、也不会新增“一键实盘”按钮；任何 Live 能力都需要独立审批与后续数据设计。</p>
        </div>
      </details>
    </div>
  );
}
