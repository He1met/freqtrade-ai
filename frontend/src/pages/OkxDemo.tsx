import { useEffect, useMemo, useState } from "react";

import {
  fetchOkxDemoObservability,
  type OkxDemoObservability,
  type OkxDemoOrder,
} from "../api/okxDemoApi";
import { CompactText, CopyableValue, StatusBadge } from "../components/DisplayPrimitives";
import "../styles/okx-demo.css";
import { okxDemoAcceptanceIsTruthful, orderCanDisplayComplete } from "./okxDemoDisplay";
import { displayDateTime, displayValue } from "./uiCopy";

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
    <aside className="okx-detail" aria-label="订单详情">
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
        <h3>订单</h3>
        <dl className="okx-detail-grid">
          <DetailValue label="数据库 ID" value={order.databaseId} />
          <DetailValue label="完整链 DB ID" value={order.fullChainDatabaseId} />
          <DetailValue label="状态" value={order.status} />
          <DetailValue label="权威状态" value={order.authoritativeStatus} />
          <DetailValue label="方向" value={order.side} />
          <DetailValue label="类型" value={order.orderType} />
          <DetailValue label="数量" value={displayDecimal(order.quantity)} />
          <DetailValue label="已成交" value={displayDecimal(order.filledQuantity)} />
          <DetailValue label="权威快照 DB ID" value={order.authoritativeSnapshotDatabaseId} />
          <DetailValue label="权威事件 DB ID" value={order.authoritativeEventDatabaseId} />
          <DetailValue label="更新时间" value={displayDateTime(order.updatedAt)} />
        </dl>
        <CopyableValue label="交易所订单 ID" value={order.exchangeOrderId ?? "未提供"} />
        <CopyableValue label="客户端订单 ID" value={order.clientOrderId} />
      </section>

      <section>
        <h3>TradeIntent 与风控</h3>
        <dl className="okx-detail-grid">
          <DetailValue label="TradeIntent DB ID" value={intent?.databaseId} />
          <DetailValue label="策略版本 ID" value={intent?.strategyVersionId} />
          <DetailValue label="意图状态" value={intent?.status} />
          <DetailValue label="风控决定" value={order.riskDecision?.decision} />
          <DetailValue label="风控 DB ID" value={order.riskDecision?.databaseId} />
          <DetailValue label="策略版本" value={order.riskDecision?.policyVersion} />
        </dl>
        {order.riskDecision?.reason ? (
          <p className="okx-action-reason">{order.riskDecision.reason}</p>
        ) : null}
        <CopyableValue label="TradeIntent ID" value={intent?.intentId ?? "未提供"} />
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

      <section>
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
    </aside>
  );
}

export function OkxDemo() {
  const [data, setData] = useState<OkxDemoObservability | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<number | null>(null);

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
  }, []);

  const selectedOrder = useMemo(
    () => data?.orders.find((order) => order.databaseId === selectedId) ?? data?.orders[0] ?? null,
    [data, selectedId],
  );

  if (error) {
    return (
      <div className="page okx-demo-page">
        <header className="page-header"><h1>OKX Demo 执行</h1></header>
        <section className="okx-load-state" role="alert">
          <strong>数据加载失败</strong>
          <p>{error}</p>
          <p>没有读取到数据库证据，页面不会显示运行成功。</p>
        </section>
      </div>
    );
  }
  if (!data) {
    return (
      <div className="page okx-demo-page">
        <header className="page-header"><h1>OKX Demo 执行</h1></header>
        <section className="okx-load-state" aria-live="polite">正在读取数据库证据…</section>
      </div>
    );
  }

  const acceptable = okxDemoAcceptanceIsTruthful(data);
  return (
    <div className="page okx-demo-page">
      <header className="okx-target-bar">
        <div>
          <span className="okx-kicker">当前唯一交易目标</span>
          <h1>{data.target.label}</h1>
          <p>OKX 永续合约 · isolated · 仅模拟资金</p>
        </div>
        <div className="okx-target-safety">
          <span>SIMULATED</span>
          <strong>不允许真实资金</strong>
        </div>
      </header>

      <section className="okx-readiness" aria-labelledby="okx-readiness-title">
        <div className="okx-section-heading">
          <div>
            <span className="okx-kicker">启动门禁</span>
            <h2 id="okx-readiness-title">运行准备度</h2>
          </div>
          <span>更新于 {displayDateTime(data.generatedAt)}</span>
        </div>
        <div className="okx-readiness-grid">
          {data.readiness.map((check) => (
            <article key={check.key}>
              <header>
                <strong>{check.label}</strong>
                <StatusBadge status={check.status} showRaw />
              </header>
              <p>{check.summary}</p>
              {check.action ? <small>{check.action}</small> : null}
            </article>
          ))}
        </div>
      </section>

      <section className="okx-acceptance">
        <div>
          <span className="okx-kicker">严格验收判定</span>
          <strong>{acceptable ? "当前证据可验收" : "当前证据不可验收"}</strong>
          <p>{data.acceptanceReason}</p>
        </div>
        <StatusBadge status={acceptable ? "ACCEPTABLE" : "NOT_ACCEPTABLE"} showRaw />
      </section>

      <section className="okx-workspace" aria-label="订单主从工作区">
        <div className="okx-master">
          <div className="okx-section-heading">
            <div>
              <span className="okx-kicker">数据库记录</span>
              <h2>订单</h2>
            </div>
            <span>{data.orders.length} 条</span>
          </div>
          {data.orders.length ? (
            <div className="okx-order-table-wrap">
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
                      onClick={() => setSelectedId(order.databaseId)}
                      tabIndex={0}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" || event.key === " ") {
                          event.preventDefault();
                          setSelectedId(order.databaseId);
                        }
                      }}
                    >
                      <td><strong>{order.instrumentId ?? "未提供"}</strong><small>DB #{order.databaseId}</small></td>
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
              <p>空结果不代表运行成功，也不能进入验收。</p>
            </div>
          )}
        </div>
        {selectedOrder ? <OrderDetail data={data} order={selectedOrder} /> : null}
      </section>

      <section className="okx-secondary-grid">
        <article>
          <div className="okx-section-heading">
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
          <div className="okx-section-heading"><h2>账户</h2><StatusBadge status={data.account.status} showRaw /></div>
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
          <div className="okx-section-heading"><h2>最近对账</h2>
            <StatusBadge status={data.latestReconciliation?.status ?? "UNKNOWN"} showRaw />
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
    </div>
  );
}
