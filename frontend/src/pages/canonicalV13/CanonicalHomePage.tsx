import { Link } from "react-router-dom";

import {
  fetchCanonicalConfigurations,
  fetchCanonicalMarketInventory,
  fetchCanonicalOptimizations,
  fetchCanonicalResearchReadiness,
  fetchCanonicalRuntimeReadiness,
  fetchCanonicalStrategies,
} from "../../api/canonicalV13Client";
import { PageHeader } from "../../components/DisplayPrimitives";
import { CanonicalStatePanel, CanonicalStatus, useCanonicalQuery } from "./CanonicalStatePanel";
import { canonicalErrorText, canonicalHomeDecision } from "./canonicalV13Model";

export function CanonicalHomePage() {
  const strategies = useCanonicalQuery(fetchCanonicalStrategies, []);
  const configurations = useCanonicalQuery(fetchCanonicalConfigurations, []);
  const market = useCanonicalQuery(fetchCanonicalMarketInventory, []);
  const research = useCanonicalQuery((signal) => fetchCanonicalResearchReadiness(null, null, signal), []);
  const runtime = useCanonicalQuery(fetchCanonicalRuntimeReadiness, []);
  const optimization = useCanonicalQuery(fetchCanonicalOptimizations, []);
  const queries = [strategies, configurations, market, research, runtime, optimization];
  const loading = queries.some((query) => query.loading);
  const decision = loading ? null : canonicalHomeDecision({
    configurations: configurations.data,
    market: market.data,
    optimization: optimization.data,
    research: research.data,
    runtime: runtime.data,
    strategies: strategies.data,
  });
  const errors = [
    ["策略目录", strategies.error],
    ["配置", configurations.error],
    ["行情", market.error],
    ["研究", research.error],
    ["Runtime", runtime.error],
    ["优化", optimization.error],
  ].filter((entry) => entry[1]);
  const cards = [
    {
      description: strategies.data ? `${strategies.data.items.length} 个 API 策略记录` : "Projection 暂不可用",
      label: "策略目录",
      status: strategies.data?.status ?? "UNKNOWN",
      to: "/v13/strategies",
    },
    {
      description: configurations.data ? `${configurations.data.unset_kinds.length} 类配置未设置` : "Projection 暂不可用",
      label: "配置准备",
      status: configurations.data?.status ?? "UNKNOWN",
      to: "/v13/configuration",
    },
    {
      description: market.data ? `${market.data.snapshots.length} 个 frozen snapshot` : "Projection 暂不可用",
      label: "行情证据",
      status: market.data?.status ?? "UNKNOWN",
      to: "/v13/market-data",
    },
    {
      description: research.data?.reason_codes.length ? research.data.reason_codes.join(" · ") : "无 API reason code",
      label: "研究状态",
      status: research.data?.status ?? "UNKNOWN",
      to: "/v13/research",
    },
    {
      description: runtime.data?.reason_codes.length ? runtime.data.reason_codes.join(" · ") : "无 API reason code",
      label: "Runtime 状态",
      status: runtime.data?.status ?? "UNKNOWN",
      to: "/v13/research",
    },
    {
      description: optimization.data ? `${optimization.data.items.length} 个 optimization run` : "Projection 暂不可用",
      label: "优化",
      status: optimization.data?.status ?? "UNKNOWN",
      to: "/v13/optimization",
    },
  ];

  return (
    <div className="page canonical-v13-page canonical-v13-home">
      <PageHeader
        description="从 Canonical API 读取策略、配置、行情、研究、Runtime 与优化状态；不读取 Legacy，不推断资格或运行成功。"
        eyebrow="V1.3 canonical-only"
        title="V1.3 用户工作台"
      />
      {loading ? <CanonicalStatePanel description="正在读取必需的 Canonical API projections。" kind="loading" title="加载项目状态" /> : null}
      {decision ? (
        <section className="canonical-v13-home-hero" data-state={decision.kind} aria-labelledby="canonical-home-state-title">
          <div className="canonical-v13-home-copy">
            <span className="canonical-v13-home-kicker">当前项目状态</span>
            <div className="canonical-v13-heading-row">
              <h2 id="canonical-home-state-title">{decision.title}</h2>
              <CanonicalStatus status={decision.rawStatus} />
            </div>
            <p>{decision.summary}</p>
            {decision.reasonCodes.length ? (
              <ul aria-label="当前主要阻断">
                {decision.reasonCodes.map((code) => <li key={code}><code>{code}</code></li>)}
              </ul>
            ) : null}
          </div>
          <Link className="formal-primary-button canonical-v13-home-action" to={decision.nextAction.to}>{decision.nextAction.label}</Link>
        </section>
      ) : null}
      <section className="canonical-v13-home-section" aria-labelledby="canonical-home-overview-title">
        <div className="canonical-v13-heading-row">
          <div><span className="canonical-v13-home-kicker">API 状态总览</span><h2 id="canonical-home-overview-title">从任务进入，而不是从内部 ID 开始</h2></div>
          <span className="canonical-v13-home-source">唯一来源：/api/canonical-v13</span>
        </div>
        <div className="canonical-v13-home-grid">
          {cards.map((card) => (
            <Link className="canonical-v13-home-card" key={card.label} to={card.to}>
              <div className="canonical-v13-heading-row"><strong>{card.label}</strong><CanonicalStatus status={card.status} /></div>
              <span>{card.description}</span>
              <b>查看详情 <span aria-hidden="true">→</span></b>
            </Link>
          ))}
        </div>
      </section>
      {errors.length ? (
        <details className="canonical-v13-home-diagnostics">
          <summary>查看 API 错误诊断</summary>
          <ul>{errors.map(([label, error]) => <li key={String(label)}><strong>{String(label)}</strong><code>{canonicalErrorText(error)}</code></li>)}</ul>
        </details>
      ) : null}
    </div>
  );
}
