import { useSearchParams } from "react-router-dom";

import { fetchCanonicalOptimizations, fetchCanonicalResearchPlans, fetchCanonicalStrategies } from "../../api/canonicalV13Client";
import { CopyableValue, PageHeader } from "../../components/DisplayPrimitives";
import { CanonicalSearchSelect, type CanonicalSelectorAvailability } from "./CanonicalSearchSelect";
import { CanonicalQueryError, CanonicalReasonList, CanonicalStatePanel, CanonicalStatus, useCanonicalQuery } from "./CanonicalStatePanel";
import { canonicalStatusesKnown, parseCanonicalUrlState, serializeCanonicalUrlState } from "./canonicalV13Model";
import { canonicalSelectionState, researchTargetSelectorOptions, strategySelectorOptions } from "./canonicalV13Selectors";

export function CanonicalOptimizationPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const url = parseCanonicalUrlState("optimization", searchParams);
  const query = useCanonicalQuery(fetchCanonicalOptimizations, [], url.valid);
  const strategies = useCanonicalQuery((signal) => fetchCanonicalStrategies(signal, 200), [], url.valid);
  const plans = useCanonicalQuery(fetchCanonicalResearchPlans, [], url.valid);
  const contractKnown = query.data ? canonicalStatusesKnown(query.data.status, ...query.data.items.map((item) => item.status))
    && query.data.items.every((item) => {
      const terminal = ["SUCCEEDED", "FAILED", "BLOCKED"].includes(item.status);
      if (!terminal) return item.terminal_reason_codes === null && item.trial_count === null && item.result_count === null && item.submitted_strategy_count === null && item.result_digest === null;
      return Array.isArray(item.terminal_reason_codes)
        && (item.status !== "BLOCKED" || item.terminal_reason_codes.length > 0)
        && item.trial_count !== null
        && item.result_count !== null
        && item.submitted_strategy_count !== null
        && item.result_digest !== null;
    }) : true;
  const strategiesKnown = strategies.data
    ? canonicalStatusesKnown(strategies.data.status, ...strategies.data.items.flatMap((strategy) => [strategy.catalog_status, strategy.intake_status, strategy.validation_status, strategy.qualification_status]))
    : true;
  const plansKnown = plans.data
    ? canonicalStatusesKnown(plans.data.status, ...plans.data.items.flatMap((plan) => [plan.plan_status, plan.attempt_status, plan.qualification_status].filter((status) => status !== null)))
    : true;
  const strategyAvailability: CanonicalSelectorAvailability = !url.valid || strategies.error || !strategiesKnown ? "unavailable" : strategies.loading ? "loading" : strategies.data?.status === "AVAILABLE" ? "ready" : "empty";
  const targetAvailability: CanonicalSelectorAvailability = !url.valid || plans.error || !plansKnown ? "unavailable" : plans.loading ? "loading" : plans.data?.status === "AVAILABLE" ? "ready" : "empty";
  const strategyOptions = strategySelectorOptions(strategiesKnown ? strategies.data : null);
  const selectedStrategy = strategies.data?.items.find((strategy) => strategy.strategy_id === url.values.strategy) ?? null;
  const targetOptions = researchTargetSelectorOptions(plansKnown ? plans.data : null, selectedStrategy);
  const strategyStale = Boolean(url.values.strategy && strategyAvailability === "ready" && canonicalSelectionState(strategyOptions, url.values.strategy) === "stale");
  const targetStale = Boolean(url.values.target && selectedStrategy && targetAvailability === "ready" && canonicalSelectionState(targetOptions, url.values.target) === "stale");

  function selectStrategy(value: string | null) {
    setSearchParams(serializeCanonicalUrlState("optimization", { strategy: value, target: null }));
  }

  function selectTarget(value: string | null) {
    setSearchParams(serializeCanonicalUrlState("optimization", { ...url.values, target: value }));
  }

  return (
    <div className="canonical-v13-page">
      <PageHeader description="优化记录是首次真实 backtest 之后的事实；选择器只保存 API 返回的稳定上下文。" eyebrow="V1.3 canonical-only" title="优化与回测结果" />
      <section className="canonical-v13-selector-panel" aria-label="优化上下文选择器">
        <CanonicalSearchSelect availability={strategyAvailability} label="策略" onChange={selectStrategy} options={strategyOptions} value={url.values.strategy ?? ""} />
        <CanonicalSearchSelect availability={targetAvailability} disabled={!selectedStrategy} label="研究目标" onChange={selectTarget} options={targetOptions} value={url.values.target ?? ""} />
      </section>
      {!url.valid ? <CanonicalStatePanel description="Optimization URL state 无效；selector 请求未发送。" kind="unknown" reasonCodes={url.problems} title="页面地址无效" /> : null}
      {strategies.error ? <CanonicalQueryError error={strategies.error} title="优化策略选项暂不可用" /> : null}
      {plans.error ? <CanonicalQueryError error={plans.error} title="优化目标选项暂不可用" /> : null}
      {strategyStale ? <CanonicalStatePanel description="Committed strategy 不在最新 API 策略目录中。" kind="unknown" reasonCodes={["SELECTED_STRATEGY_NOT_FOUND"]} title="所选策略已失效" /> : null}
      {targetStale ? <CanonicalStatePanel description="Committed target 不属于所选策略当前版本的 API plan。" kind="unknown" reasonCodes={["SELECTED_TARGET_NOT_FOUND"]} title="所选研究目标已失效" /> : null}
      {query.loading ? <CanonicalStatePanel description="正在读取 optimization projection。" kind="loading" title="加载 optimization" /> : null}
      {query.error ? <CanonicalQueryError error={query.error} title="优化状态未知" /> : null}
      {query.data && !contractKnown ? <CanonicalStatePanel description="Optimization projection 含未知 enum；列表保持禁用。" kind="unknown" reasonCodes={["UNKNOWN_CONTRACT_VALUE"]} title="优化合同漂移" /> : null}
      {contractKnown && query.data?.status === "PENDING_FIRST_BACKTEST" ? <CanonicalStatePanel description="尚未获得首次真实 backtest 事实；没有 optimization run、score 或 qualification 可展示。" kind="pending" reasonCodes={["PENDING_FIRST_BACKTEST"]} title="等待首次回测" /> : null}
      {contractKnown && query.data?.status === "AVAILABLE" ? (
        <section className="canonical-v13-panel">
          <div className="canonical-v13-heading-row"><h2>优化记录</h2><CanonicalStatus status={query.data.status} /></div>
          <div className="canonical-v13-card-list">
            {query.data.items.map((run) => (
              <article className="canonical-v13-data-card" key={run.optimization_run_id}>
                <div className="canonical-v13-heading-row"><strong>创建于 {run.created_at}</strong><CanonicalStatus status={run.status} /></div>
                <span>完成时间：{run.completed_at ?? "尚未完成"}</span>
                {run.trial_count !== null ? <span>Trials {run.trial_count} · Results {run.result_count} · 已提交策略 {run.submitted_strategy_count}</span> : null}
                {run.terminal_reason_codes ? <CanonicalReasonList reasonCodes={run.terminal_reason_codes} /> : null}
                <details className="canonical-v13-advanced-evidence"><summary>高级优化标识</summary>
                  <CopyableValue label="Optimization run" value={run.optimization_run_id} />
                  <CopyableValue label="基线资格决策" value={run.baseline_qualification_decision_id} />
                  <CopyableValue label="Request digest" value={run.request_digest} />
                  {run.receipt_digest ? <CopyableValue label="Run receipt digest" value={run.receipt_digest} /> : null}
                  {run.result_digest ? <CopyableValue label="Terminal result digest" value={run.result_digest} /> : null}
                </details>
              </article>
            ))}
          </div>
        </section>
      ) : null}
      {(url.values.target || url.values.strategy) ? <CanonicalStatePanel description="当前 optimization DTO 不包含 target/strategy filter lineage；URL 仅保留 API selection，页面不伪造过滤结果。" kind="pending" title="选择上下文" /> : null}
    </div>
  );
}
