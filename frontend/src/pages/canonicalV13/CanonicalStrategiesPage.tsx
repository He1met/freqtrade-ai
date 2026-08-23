import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { fetchCanonicalResearchPlans, fetchCanonicalStrategies, fetchCanonicalStrategy } from "../../api/canonicalV13Client";
import type { StrategyProjection } from "../../api/canonicalV13Types";
import { CopyableValue, PageHeader } from "../../components/DisplayPrimitives";
import { CanonicalResearchStepper } from "./CanonicalResearchStepper";
import { CanonicalQueryError, CanonicalStatePanel, CanonicalStatus, useCanonicalQuery } from "./CanonicalStatePanel";
import { canonicalStatusesKnown, parseCanonicalUrlState, serializeCanonicalUrlState, withCanonicalUrlValue } from "./canonicalV13Model";
import { filterCanonicalSelectorOptions, strategySelectorOptions } from "./canonicalV13Selectors";

function StrategyDetail({ strategyId }: { strategyId: string }) {
  const query = useCanonicalQuery((signal) => fetchCanonicalStrategy(strategyId, signal), [strategyId]);
  const plans = useCanonicalQuery(fetchCanonicalResearchPlans, [strategyId]);
  if (query.loading) return <CanonicalStatePanel description="正在读取所选 canonical strategy。" kind="loading" title="加载策略详情" />;
  if (query.error) return <CanonicalQueryError error={query.error} title="所选策略无法读取" />;
  const strategy = query.data as StrategyProjection;
  if (!canonicalStatusesKnown(strategy.catalog_status, strategy.intake_status, strategy.validation_status, strategy.qualification_status)) {
    return <CanonicalStatePanel description="策略详情返回未知 enum；详情与成功状态保持隐藏。" kind="unknown" reasonCodes={["UNKNOWN_CONTRACT_VALUE"]} title="策略详情合同漂移" />;
  }
  const researchQuery = serializeCanonicalUrlState("research", { strategy: strategy.strategy_id });
  const planStatusesKnown = plans.data
    ? canonicalStatusesKnown(plans.data.status, ...plans.data.items.flatMap((plan) => [plan.plan_status, plan.attempt_status, plan.qualification_status].filter((status) => status !== null)))
    : true;
  const relatedPlans = plans.data?.items.filter((plan) => plan.strategy_version_id === strategy.current_version_id) ?? [];
  return (
    <>
      <section className="canonical-v13-panel" aria-label="Canonical strategy detail">
        <div className="canonical-v13-heading-row"><h2>{strategy.display_name}</h2><CanonicalStatus status={strategy.catalog_status} /></div>
        <div className="canonical-v13-status-grid">
          <div><span>受控入库</span><CanonicalStatus status={strategy.intake_status} /></div>
          <div><span>入库代码验证</span><CanonicalStatus status={strategy.validation_status} /></div>
          <div><span>OOS 研究资格</span><CanonicalStatus status={strategy.qualification_status} /></div>
          <div><span>执行授权</span><strong>{strategy.execution_authorized ? "是" : "否"}</strong></div>
        </div>
        <p>当前版本 {strategy.version_number}</p>
        <details className="canonical-v13-advanced-evidence"><summary>高级标识与摘要</summary><dl className="canonical-v13-definition-list">
          <div><dt>Strategy ID</dt><dd><CopyableValue value={strategy.strategy_id} /></dd></div>
          <div><dt>Version ID</dt><dd><CopyableValue value={strategy.current_version_id} /></dd></div>
          <div><dt>Artifact digest</dt><dd><CopyableValue value={strategy.artifact_digest} /></dd></div>
        </dl></details>
      </section>
      <CanonicalResearchStepper
        chain={null}
        researchHref={`/v13/research?${researchQuery}`}
        selection={{ planId: null, strategyId, targetId: null }}
        strategy={strategy}
        strategyHref={`/v13/strategies?strategy=${encodeURIComponent(strategy.strategy_id)}`}
      />
      {plans.loading ? <CanonicalStatePanel description="正在读取当前策略版本的研究计划与结果摘要。" kind="loading" title="加载相关回测结果" /> : null}
      {plans.error ? <CanonicalQueryError error={plans.error} title="相关回测结果未知" /> : null}
      {plans.data && !planStatusesKnown ? <CanonicalStatePanel description="相关 plan projection 含未知状态；结果入口保持隐藏。" kind="unknown" reasonCodes={["UNKNOWN_CONTRACT_VALUE"]} title="研究结果合同漂移" /> : null}
      {plans.data && planStatusesKnown ? (
        <section className="canonical-v13-panel" aria-labelledby="strategy-related-results">
          <div className="canonical-v13-heading-row"><h2 id="strategy-related-results">当前版本的研究与回测结果</h2><span>{relatedPlans.length} 个 API plan</span></div>
          {relatedPlans.length ? <div className="canonical-v13-card-list">{relatedPlans.map((plan) => {
            const href = serializeCanonicalUrlState("research", {
              plan: plan.validation_plan_id,
              strategy: strategy.strategy_id,
              target: plan.research_target_id,
            });
            return <Link className="canonical-v13-data-card canonical-v13-result-link" key={plan.validation_plan_id} to={`/v13/research?${href}`}>
              <strong>{plan.target_key}</strong>
              <span>Plan：<CanonicalStatus status={plan.plan_status} /></span>
              <span>资格：<CanonicalStatus status={plan.qualification_status ?? "UNSET"} /></span>
              <b>{plan.overall_score === null ? "尚无 API 分数" : `API 分数 ${plan.overall_score}`}</b>
              <em>查看 exact 回测证据 →</em>
            </Link>;
          })}</div> : <CanonicalStatePanel description="Canonical API 未返回当前策略版本的 validation plan；不从历史页面补充回测结果。" kind="empty" title="尚无相关研究结果" />}
        </section>
      ) : null}
    </>
  );
}

export function CanonicalStrategiesPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const url = parseCanonicalUrlState("strategies", searchParams);
  const catalog = useCanonicalQuery(fetchCanonicalStrategies, [], url.valid);
  const [search, setSearch] = useState("");
  const selected = url.values.strategy ?? null;
  const catalogContractKnown = catalog.data
    ? canonicalStatusesKnown(catalog.data.status, ...catalog.data.items.flatMap((item) => [item.catalog_status, item.intake_status, item.validation_status, item.qualification_status]))
    : true;
  const strategyOptions = strategySelectorOptions(catalogContractKnown ? catalog.data : null);
  const matchingStrategyIds = new Set(filterCanonicalSelectorOptions(strategyOptions, search).map((option) => option.value));

  function selectStrategy(strategyId: string | null) {
    setSearchParams(withCanonicalUrlValue("strategies", url.values, "strategy", strategyId));
  }

  return (
    <div className="canonical-v13-page">
      <PageHeader description="入库代码验证与 exact OOS 研究资格是两套独立事实；列表不会自动选择第一项。" eyebrow="V1.3 canonical-only" title="策略目录" />
      {!url.valid ? <CanonicalStatePanel description="URL selection 无效；未发送详情请求。" kind="unknown" reasonCodes={url.problems} title="策略选择无效" /> : null}
      {catalog.loading ? <CanonicalStatePanel description="正在读取 canonical catalog。" kind="loading" title="加载策略目录" /> : null}
      {catalog.error ? <CanonicalQueryError error={catalog.error} title="策略目录状态未知" /> : null}
      {catalog.data && !catalogContractKnown ? <CanonicalStatePanel description="目录包含未知 enum；selection 与详情请求保持禁用。" kind="unknown" reasonCodes={["UNKNOWN_CONTRACT_VALUE"]} title="策略目录合同漂移" /> : null}
      {catalogContractKnown && catalog.data?.status === "EMPTY" ? <CanonicalStatePanel description="API 明确返回空目录；这不是加载失败或 Legacy fallback。" kind="empty" title="Canonical 策略目录为空" /> : null}
      {catalogContractKnown && catalog.data?.status === "AVAILABLE" ? (
        <section className="canonical-v13-panel">
          <div className="canonical-v13-heading-row"><h2>Canonical 策略</h2><CanonicalStatus status={catalog.data.status} /></div>
          <label className="canonical-v13-card-search">搜索策略<input disabled={catalog.loading} onChange={(event) => setSearch(event.target.value)} placeholder="按名称或状态搜索" type="search" value={search} /></label>
          <div className="canonical-v13-card-list">
            {catalog.data.items.filter((strategy) => matchingStrategyIds.has(strategy.strategy_id)).map((strategy) => (
              <button
                aria-pressed={selected === strategy.strategy_id}
                className="canonical-v13-select-card"
                key={strategy.strategy_id}
                onClick={() => selectStrategy(strategy.strategy_id)}
                type="button"
              >
                <strong>{strategy.display_name}</strong>
                <span>版本 {strategy.version_number}</span>
                <span className="canonical-v13-card-statuses"><CanonicalStatus status={strategy.validation_status} /><CanonicalStatus status={strategy.qualification_status} /></span>
              </button>
            ))}
          </div>
          {search && !matchingStrategyIds.size ? <CanonicalStatePanel description="当前 API 策略选项中没有匹配名称或状态；未改写已提交 selection。" kind="empty" title="没有匹配策略" /> : null}
        </section>
      ) : null}
      {selected && url.valid && catalogContractKnown ? (
        <><button className="canonical-v13-text-button" onClick={() => selectStrategy(null)} type="button">清除所选策略</button><StrategyDetail strategyId={selected} /></>
      ) : (
        <CanonicalStatePanel description="请显式选择 strategy；UI 不会自动选择第一项。" kind="empty" title="尚未选择策略" />
      )}
    </div>
  );
}
