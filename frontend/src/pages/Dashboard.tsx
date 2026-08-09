import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import {
  fetchOkxDemoObservability,
  type OkxDemoObservability,
} from "../api/okxDemoApi";
import { combineDataSources } from "../api/sourceState";
import {
  fetchFormalResearchRun,
  fetchStrategyResearchBatches,
  type FormalResearchRun,
  type StrategyResearchBatch,
} from "../api/strategyResearchApi";
import { useMvpData } from "../api/useMvpData";
import { EmptyState, PageHeader, StatusBadge } from "../components/DisplayPrimitives";
import { okxDemoAcceptanceIsTruthful } from "./okxDemoDisplay";
import { deploymentHandoffText, validatedCandidateCount } from "./strategyFactoryModel";
import { displayDateTime } from "./uiCopy";

type Loadable<T> = {
  data: T | null;
  error: string | null;
  loading: boolean;
};

const initialLoadable = <T,>(): Loadable<T> => ({ data: null, error: null, loading: true });

function metricValue(loading: boolean, error: string | null, value: number | null): string | number {
  if (loading) return "…";
  if (error || value === null) return "—";
  return value;
}

export function Dashboard() {
  const { data, sources, isLoading, error } = useMvpData();
  const coreSource = combineDataSources(sources, ["strategies", "strategyVersions", "ranking"]);
  const [research, setResearch] = useState<Loadable<{
    batches: StrategyResearchBatch[];
    run: FormalResearchRun;
  }>>(initialLoadable);
  const [demo, setDemo] = useState<Loadable<OkxDemoObservability>>(initialLoadable);

  useEffect(() => {
    const controller = new AbortController();
    Promise.all([
      fetchStrategyResearchBatches(controller.signal),
      fetchFormalResearchRun(controller.signal),
    ])
      .then(([batches, run]) => setResearch({ data: { batches, run }, error: null, loading: false }))
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) {
          setResearch({
            data: null,
            error: reason instanceof Error ? reason.message : String(reason),
            loading: false,
          });
        }
      });
    fetchOkxDemoObservability(controller.signal)
      .then((result) => setDemo({ data: result, error: null, loading: false }))
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) {
          setDemo({
            data: null,
            error: reason instanceof Error ? reason.message : String(reason),
            loading: false,
          });
        }
      });
    return () => controller.abort();
  }, []);

  const latestResearch = research.data?.batches[0] ?? null;
  const formalResearchRun = research.data?.run ?? null;
  const latestResearchRun = formalResearchRun?.run_id === latestResearch?.run_id
    ? formalResearchRun
    : null;
  const dataHasProblem = Boolean(error || research.error || demo.error);
  const pageLoading = isLoading || research.loading || demo.loading;
  const pageConclusion = pageLoading
    ? "正在核对正式研究与模拟盘证据"
    : dataHasProblem
      ? "部分状态无法确认，未把缺失数据计为 0"
      : "核心数据已读取，运行中策略与最近信号仍待只读接口";
  const pageStatus = pageLoading ? "RUNNING" : dataHasProblem ? "UNKNOWN" : "READY";
  const fills = demo.data?.orders.reduce((total, order) => total + order.fills.length, 0) ?? null;
  const activities = useMemo(() => {
    const items: Array<{ id: string; title: string; meta: string; status: string }> = [];
    if (latestResearch) {
      items.push({
        id: `research-${latestResearch.id}`,
        title: `研究批次 ${latestResearch.run_id}`,
        meta: `${displayDateTime(latestResearch.completed_at ?? latestResearch.created_at)} · ${latestResearch.persisted_count} 条入库`,
        status: latestResearch.status,
      });
    }
    for (const order of demo.data?.orders.slice(0, 4) ?? []) {
      items.push({
        id: `order-${order.databaseId}`,
        title: `${order.instrumentId ?? "合约未知"} · ${order.side ?? "方向未知"}`,
        meta: `${displayDateTime(order.updatedAt)} · 订单 DB #${order.databaseId}`,
        status: order.authoritativeStatus ?? order.status,
      });
    }
    return items.slice(0, 5);
  }, [demo.data, latestResearch]);

  return (
    <section className="page dashboard-page formal-page">
      <PageHeader
        actions={<span className="formal-target-chip">OKX_DEMO · Demo-only</span>}
        description="先看结论、研究进度与模拟盘证据；技术详情按需展开。"
        eyebrow="正式工作台"
        status={<StatusBadge label={pageLoading ? "读取中" : dataHasProblem ? "部分未知" : "已读取"} status={pageStatus} />}
        title="总览"
      />

      <section className="formal-conclusion" data-state={dataHasProblem ? "attention" : "neutral"}>
        <div>
          <span className="formal-kicker">当前结论</span>
          <h2>{pageConclusion}</h2>
          <p>
            {dataHasProblem
              ? "可用区块继续展示真实数据；读取失败的区块保持未知，不推断系统空闲或运行正常。"
              : "页面不会把目录 active、QUALIFIED 或订单记录替代为 ACTIVE 部署与 signal evaluation。"}
          </p>
        </div>
        <Link className="formal-primary-link" to="/strategies">查看策略工厂</Link>
      </section>

      <section aria-labelledby="dashboard-metrics-title">
        <div className="formal-section-heading">
          <div>
            <span className="formal-kicker">项目状态</span>
            <h2 id="dashboard-metrics-title">四个关键数字</h2>
          </div>
          <span className="formal-section-note">真实来源不可用时显示“—”</span>
        </div>
        <div className="formal-metric-grid">
          <article className={isLoading ? "formal-metric formal-skeleton" : "formal-metric"}>
            <span>正式策略</span>
            <strong>{metricValue(isLoading, error, error ? null : data.strategies.length)}</strong>
            <small>{error ? "策略 API 读取失败" : coreSource === "api" ? "正式策略目录" : "来源未确认"}</small>
          </article>
          <article className={research.loading ? "formal-metric formal-skeleton" : "formal-metric"}>
            <span>本批次候选</span>
            <strong>{metricValue(research.loading, research.error, latestResearch?.persisted_count ?? null)}</strong>
            <small>{research.error ? "研究 API 读取失败" : latestResearch ? "已持久化候选" : "尚无持久化批次"}</small>
          </article>
          <article className={research.loading ? "formal-metric formal-skeleton" : "formal-metric"}>
            <span>合格候选</span>
            <strong>{metricValue(research.loading, research.error, latestResearch?.qualified_count ?? null)}</strong>
            <small>{latestResearch?.qualified_count ? "待既有部署评审" : latestResearch ? "本批次无合格" : "尚无批次"}</small>
          </article>
          <article className="formal-metric" data-state="unknown">
            <span>运行中策略</span>
            <strong>—</strong>
            <small>ACTIVE deployment 只读接口待补</small>
          </article>
        </div>
      </section>

      <section className="formal-panel" aria-labelledby="dashboard-research-title">
        <div className="formal-section-heading">
          <div>
            <span className="formal-kicker">最新正式研究</span>
            <h2 id="dashboard-research-title">生成 → 验证 → 入库 → 评审</h2>
          </div>
          <Link className="formal-text-link" to="/strategies">查看全部</Link>
        </div>
        {research.loading ? (
          <div className="formal-lifecycle formal-skeleton" aria-label="正在读取研究进度" />
        ) : research.error ? (
          <EmptyState title="研究状态未知" description="正式研究 API 读取失败，不代表尚未生成或全部被拒绝。" />
        ) : latestResearch ? (
          <>
            <div className="formal-lifecycle">
              {[
                ["请求", latestResearch.requested_count],
                ["生成", latestResearch.generated_count],
                ["验证", validatedCandidateCount(latestResearch)],
                ["入库", latestResearch.persisted_count],
                ["合格", latestResearch.qualified_count],
                ["拒绝", latestResearch.rejected_count],
              ].map(([label, value], index) => (
                <div className="formal-lifecycle-step" key={String(label)}>
                  <span>{index + 1}</span>
                  <div><small>{label}</small><strong>{value}</strong></div>
                </div>
              ))}
            </div>
            <div className="formal-panel-footer">
              <span>{deploymentHandoffText(latestResearchRun)}</span>
              <time dateTime={latestResearch.completed_at ?? latestResearch.created_at}>
                {displayDateTime(latestResearch.completed_at ?? latestResearch.created_at)}
              </time>
            </div>
          </>
        ) : (
          <EmptyState title="尚无持久化研究批次" description="这表示尚未完成生成与入库，不代表 10 条候选被拒绝。" />
        )}
      </section>

      <div className="formal-two-column">
        <section className="formal-panel" aria-labelledby="dashboard-deployment-title">
          <div className="formal-section-heading compact">
            <div><span className="formal-kicker">部署</span><h2 id="dashboard-deployment-title">评审与运行</h2></div>
            <StatusBadge label="只读" status="UNKNOWN" />
          </div>
          <dl className="formal-summary-list">
            <div><dt>部署交接</dt><dd>{deploymentHandoffText(latestResearchRun)}</dd></div>
            <div><dt>ACTIVE 运行策略</dt><dd>暂不可用</dd></div>
          </dl>
          <p className="formal-muted">QUALIFIED 只代表可进入评审，不代表已批准、已部署或正在运行。</p>
        </section>

        <section className="formal-panel" aria-labelledby="dashboard-demo-title">
          <div className="formal-section-heading compact">
            <div><span className="formal-kicker">模拟盘</span><h2 id="dashboard-demo-title">最近执行证据</h2></div>
            <Link className="formal-text-link" to="/okx-demo">查看模拟盘</Link>
          </div>
          {demo.loading ? <div className="formal-compact-skeleton formal-skeleton" /> : demo.error ? (
            <p className="formal-problem">模拟盘证据读取失败，当前状态未知。</p>
          ) : demo.data ? (
            <dl className="formal-summary-list">
              <div><dt>严格验收</dt><dd>{okxDemoAcceptanceIsTruthful(demo.data) ? "证据可验收" : "当前不可验收"}</dd></div>
              <div><dt>订单 / 成交</dt><dd>{demo.data.orders.length} / {fills ?? 0}</dd></div>
              <div><dt>最近对账</dt><dd>{demo.data.latestReconciliation?.status ?? "无记录"}</dd></div>
            </dl>
          ) : null}
        </section>
      </div>

      <section className="formal-panel" aria-labelledby="dashboard-activity-title">
        <div className="formal-section-heading compact">
          <div><span className="formal-kicker">最近活动</span><h2 id="dashboard-activity-title">研究与订单证据</h2></div>
          <span className="formal-section-note">最多 5 条</span>
        </div>
        {activities.length ? (
          <ol className="formal-activity-list">
            {activities.map((activity) => (
              <li key={activity.id}>
                <StatusBadge status={activity.status} />
                <div><strong>{activity.title}</strong><span>{activity.meta}</span></div>
              </li>
            ))}
          </ol>
        ) : pageLoading ? (
          <div className="formal-compact-skeleton formal-skeleton" />
        ) : (
          <EmptyState title="暂无可确认的最近活动" description="无记录与读取失败已分开处理；页面不会制造研究或订单数据。" />
        )}
      </section>
    </section>
  );
}
