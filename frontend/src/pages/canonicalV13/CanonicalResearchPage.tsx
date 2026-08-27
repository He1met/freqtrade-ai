import { useSearchParams } from "react-router-dom";

import {
  fetchCanonicalConfigurations,
  fetchCanonicalPhase9Readiness,
  fetchCanonicalResearchChain,
  fetchCanonicalResearchGates,
  fetchCanonicalResearchPlans,
  fetchCanonicalResearchReadiness,
  fetchCanonicalResearchResults,
  fetchCanonicalRuntimeReadiness,
  fetchCanonicalStrategies,
  fetchCanonicalStrategy,
} from "../../api/canonicalV13Client";
import type { GateListProjection, Phase9AcceptanceStage, Phase9ReadinessProjection, ReadinessProjection, ResearchChainProjection, ResearchResultsProjection } from "../../api/canonicalV13Types";
import { CopyableValue, PageHeader } from "../../components/DisplayPrimitives";
import { CanonicalResearchStepper } from "./CanonicalResearchStepper";
import { CanonicalResearchResults } from "./CanonicalResearchResults";
import { CanonicalResearchHistory } from "./CanonicalResearchHistory";
import { CanonicalSearchSelect, type CanonicalSelectorAvailability } from "./CanonicalSearchSelect";
import { CanonicalInlineReason, CanonicalQueryError, CanonicalStatePanel, CanonicalStatus, useCanonicalQuery } from "./CanonicalStatePanel";
import { canonicalStatusesKnown, canonicalStatusPresentation, parseCanonicalUrlState, serializeCanonicalUrlState } from "./canonicalV13Model";
import {
  canonicalSelectionState,
  configurationContextOptions,
  researchPlanSelectorOptions,
  researchTargetSelectorOptions,
  strategySelectorOptions,
} from "./canonicalV13Selectors";

function ReadinessCard({ dependencyKey, loader, title }: {
  dependencyKey: string;
  loader: (signal: AbortSignal) => Promise<ReadinessProjection>;
  title: string;
}) {
  const query = useCanonicalQuery(loader, [dependencyKey]);
  if (query.loading) return <CanonicalStatePanel description={`正在读取 ${title}。`} kind="loading" title={`加载${title}`} />;
  if (query.error) return <CanonicalQueryError error={query.error} title={`${title}状态未知`} />;
  const readiness = query.data as ReadinessProjection;
  const presentation = canonicalStatusPresentation(readiness.status);
  if (!presentation.known) {
    return <CanonicalStatePanel description={`API 返回未知状态 ${readiness.status}；所有动作保持禁用。`} kind="unknown" reasonCodes={["UNKNOWN_CONTRACT_VALUE"]} title={`${title}合同漂移`} />;
  }
  return (
    <section className="canonical-v13-panel" data-readiness={title.toLowerCase()}>
      <div className="canonical-v13-heading-row"><h2>{title}</h2><CanonicalStatus status={readiness.status} /></div>
      {readiness.status === "BLOCKED" ? <CanonicalStatePanel description="阻塞原因来自 API projection；UI 未进行二次计算。" kind="blocked" reasonCodes={readiness.reason_codes} title={`${title}已阻断`} /> : null}
      {readiness.status === "PENDING_FIRST_BACKTEST" ? <CanonicalStatePanel description="Bundle 已冻结，但尚无获授权的首次真实回测事实；UI 不将其提升为 READY。" kind="pending" reasonCodes={readiness.reason_codes} title="等待首次回测" /> : null}
      <dl className="canonical-v13-definition-list">
        <div><dt>Scope</dt><dd>{readiness.scope_key ?? "未设置"}</dd></div>
        <div><dt>Workflow</dt><dd>{readiness.workflow_key ?? "未设置"}</dd></div>
        <div><dt>目标数量</dt><dd>{readiness.target_count ?? "未设置"}</dd></div>
        <div><dt>候选数量</dt><dd>{readiness.total_candidate_count ?? "未设置"}</dd></div>
      </dl>
      <details className="canonical-v13-advanced-evidence"><summary>高级运行标识</summary><dl className="canonical-v13-definition-list">
        <div><dt>Bundle</dt><dd>{readiness.configuration_bundle_id ? <CopyableValue value={readiness.configuration_bundle_id} /> : "未设置"}</dd></div>
        <div><dt>Runtime instance</dt><dd>{readiness.runtime_instance_id ? <CopyableValue value={readiness.runtime_instance_id} /> : "未设置"}</dd></div>
      </dl></details>
    </section>
  );
}

function ResearchChainCard({ chain }: { chain: ResearchChainProjection }) {
  for (const status of [chain.plan_status, chain.attempt_status, chain.qualification_status]) {
    if (status && !canonicalStatusPresentation(status).known) {
      return <CanonicalStatePanel description={`API 返回未知状态 ${status}；UI 不推断 qualification。`} kind="unknown" reasonCodes={["UNKNOWN_CONTRACT_VALUE"]} title="研究链路合同漂移" />;
    }
  }
  return (
    <section className="canonical-v13-panel" data-readiness="research-chain">
      <div className="canonical-v13-heading-row"><h2>精确研究链路</h2><CanonicalStatus status={chain.plan_status} /></div>
      <div className="canonical-v13-status-grid">
        <div><span>验证 Attempt</span><CanonicalStatus status={chain.attempt_status ?? "UNSET"} /></div>
        <div><span>资格</span><CanonicalStatus status={chain.qualification_status ?? "UNSET"} /></div>
        <div><span>目标</span><strong>{chain.target_key}</strong></div>
        <div><span>Overall score</span><strong>{chain.overall_score ?? "未提供"}</strong></div>
      </div>
      {chain.qualification_reason_code ? <CanonicalInlineReason code={chain.qualification_reason_code} /> : null}
      <details className="canonical-v13-advanced-evidence"><summary>高级 Lineage 与回执</summary><dl className="canonical-v13-definition-list">
        <div><dt>Validation plan</dt><dd><CopyableValue value={chain.validation_plan_id} /></dd></div>
        <div><dt>Plan digest</dt><dd><CopyableValue value={chain.validation_plan_digest} /></dd></div>
        <div><dt>Target ID</dt><dd><CopyableValue value={chain.research_target_id} /></dd></div>
        <div><dt>Strategy version</dt><dd><CopyableValue value={chain.strategy_version_id} /></dd></div>
        <div><dt>Attempt receipt</dt><dd>{chain.attempt_receipt_digest ? <CopyableValue value={chain.attempt_receipt_digest} /> : "UNSET"}</dd></div>
        <div><dt>Score receipt</dt><dd>{chain.score_digest ? <CopyableValue value={chain.score_digest} /> : "UNSET"}</dd></div>
        <div><dt>Qualification receipt</dt><dd>{chain.qualification_decision_digest ? <CopyableValue value={chain.qualification_decision_digest} /> : "UNSET"}</dd></div>
      </dl></details>
    </section>
  );
}

const PHASE9_STAGES: readonly Phase9AcceptanceStage[] = [
  "QUALIFICATION_HANDOFF", "NO_ORDER_SOAK", "SIGNAL_RISK_SHADOW", "OKX_DEMO_CANARY", "RECOVERY_SOAK",
];

const PHASE9_STAGE_LABELS: Record<Phase9AcceptanceStage, string> = {
  QUALIFICATION_HANDOFF: "资格交接",
  NO_ORDER_SOAK: "A · 无订单运行",
  SIGNAL_RISK_SHADOW: "B · 信号与风险影子",
  OKX_DEMO_CANARY: "C · OKX Demo Canary",
  RECOVERY_SOAK: "D · 恢复与重放",
};

function Phase9StageCard({ projection, stage }: { projection: ResearchResultsProjection; stage: Phase9AcceptanceStage }) {
  const qualificationId = projection.qualification?.qualification_decision_id ?? "";
  const query = useCanonicalQuery(
    (signal) => fetchCanonicalPhase9Readiness({
      qualification_decision_id: qualificationId,
      strategy_version_id: projection.strategy_version_id,
      configuration_bundle_id: projection.configuration_bundle_id,
      market_snapshot_id: projection.market_snapshot_id,
    }, stage, signal),
    [qualificationId, projection.strategy_version_id, projection.configuration_bundle_id, projection.market_snapshot_id, stage],
    Boolean(qualificationId && projection.qualification?.status === "QUALIFIED"),
  );
  const title = PHASE9_STAGE_LABELS[stage];
  if (query.loading) return <CanonicalStatePanel description={`正在读取 ${title} 的持久化证据。`} kind="loading" title={`加载${title}`} />;
  if (query.error) return <CanonicalQueryError error={query.error} title={`${title}状态未知`} />;
  if (!query.data) return null;
  const readiness = query.data as Phase9ReadinessProjection;
  const acceptanceTriggerCount = readiness.execution_domain_counts.acceptance_signal_triggers ?? 0;
  if (!canonicalStatusesKnown(readiness.status) || readiness.stage !== stage) {
    return <CanonicalStatePanel description="Phase 9 projection 含未知状态或 stage 漂移；UI 不进行推断。" kind="unknown" reasonCodes={["UNKNOWN_CONTRACT_VALUE"]} title={`${title}合同漂移`} />;
  }
  return <section className="canonical-v13-panel" data-phase9-stage={stage}>
    <div className="canonical-v13-heading-row"><h3>{title}</h3><CanonicalStatus status={readiness.status} /></div>
    {acceptanceTriggerCount > 0 ? <CanonicalStatePanel description="这是 control-plane 隔离的一次性验收测试信号，只验证 signal 后半链；不代表策略自然盈利，也不进入研究、评分或资格指标。" kind="pending" reasonCodes={["ACCEPTANCE_SCHEDULED_TEST"]} title={`验收测试信号 · ${acceptanceTriggerCount} 条`} /> : null}
    {readiness.status === "BLOCKED" ? <CanonicalStatePanel description="此阶段保持 fail closed；原因码与计数均来自 canonical API。" kind="blocked" reasonCodes={readiness.reason_codes} title={`${title}未获准`} /> : null}
    <dl className="canonical-v13-definition-list">
      <div><dt>订单</dt><dd>{readiness.execution_domain_counts.orders ?? "未知"}</dd></div>
      <div><dt>验收测试触发器</dt><dd>{readiness.execution_domain_counts.acceptance_signal_triggers ?? 0}</dd></div>
      <div><dt>Fill</dt><dd>{readiness.execution_domain_counts.fills ?? "未知"}</dd></div>
      <div><dt>Ledger</dt><dd>{readiness.execution_domain_counts.ledger_entries ?? "未知"}</dd></div>
      <div><dt>Reconciliation</dt><dd>{readiness.execution_domain_counts.reconciliation_runs ?? "未知"}</dd></div>
    </dl>
    <details className="canonical-v13-advanced-evidence"><summary>高级 Phase 9 回执</summary><dl className="canonical-v13-definition-list">
      <div><dt>Qualification</dt><dd>{readiness.handoff ? <CopyableValue value={readiness.handoff.qualification_decision_id} /> : "未提供"}</dd></div>
      <div><dt>Topology digest</dt><dd><CopyableValue value={readiness.topology_digest} /></dd></div>
      <div><dt>Readiness receipt</dt><dd><CopyableValue value={readiness.receipt_digest} /></dd></div>
    </dl></details>
  </section>;
}

function Phase9ReadinessBoard({ projection }: { projection: ResearchResultsProjection }) {
  if (projection.qualification?.status !== "QUALIFIED") {
    return <CanonicalStatePanel description="仅 API 明确返回 QUALIFIED 的 exact research result 才会读取 Phase 9 readiness。" kind="blocked" reasonCodes={["EXACT_QUALIFICATION_HANDOFF_REQUIRED"]} title="Phase 9 未获得资格交接" />;
  }
  return <section className="canonical-v13-panel" aria-label="Phase 9 分段验收">
    <div className="canonical-v13-heading-row"><h2>Phase 9 分段验收</h2><CanonicalStatus status="QUALIFIED" /></div>
    <p>只读投影 A–D 的持久化证据；本页面不提供 deployment、runtime 或 order 写入控制。</p>
    <div className="canonical-v13-readiness-grid">
      {PHASE9_STAGES.map((stage) => <Phase9StageCard key={stage} projection={projection} stage={stage} />)}
    </div>
  </section>;
}

function GateReceiptsCard() {
  const query = useCanonicalQuery(fetchCanonicalResearchGates, ["canonical-gates-v3"]);
  if (query.loading) return <CanonicalStatePanel description="正在读取 canonical v3 gate receipts。" kind="loading" title="加载 static/lookahead receipts" />;
  if (query.error) return <CanonicalQueryError error={query.error} title="Gate receipt 状态未知" />;
  const projection = query.data as GateListProjection;
  const itemStatuses: string[] = [];
  for (const item of projection.items) {
    itemStatuses.push(item.status);
    if (item.static_status) itemStatuses.push(item.static_status);
    if (item.lookahead_status) itemStatuses.push(item.lookahead_status);
  }
  if (!canonicalStatusesKnown(projection.status, ...itemStatuses)) return <CanonicalStatePanel description="Gate projection 返回未知状态；receipt 列表保持隐藏。" kind="unknown" reasonCodes={["UNKNOWN_CONTRACT_VALUE"]} title="Gate 回执合同漂移" />;
  if (!projection.items.length) return <CanonicalStatePanel description="尚无持久化 planless v3 gate receipt；UI 不使用历史评论或 Legacy 状态推断。" kind="pending" title="暂无 Gate 回执" />;
  return (
    <section className="canonical-v13-panel" data-readiness="gate-receipts">
      <div className="canonical-v13-heading-row"><h2>Canonical Static / Lookahead Gates</h2><CanonicalStatus status={projection.status} /></div>
      <details className="canonical-v13-advanced-evidence"><summary>高级 Gate Lineage 与回执</summary><div className="canonical-v13-table-wrap"><table><thead><tr><th>策略版本</th><th>Static</th><th>Lookahead</th><th>验证条件</th><th>原因 / 数量</th><th>冻结 Lineage</th></tr></thead><tbody>
        {projection.items.map((item) => <tr key={item.gate_attempt_id}>
          <td><CopyableValue value={item.strategy_version_id} /></td>
          <td><CanonicalStatus status={item.static_status ?? "UNSET"} /></td>
          <td><CanonicalStatus status={item.lookahead_status ?? "UNSET"} /></td>
          <td>{item.validation_eligible ? "符合验证条件" : "不符合验证条件"}</td>
          <td>{item.lookahead_reason_code ?? item.static_reason_code ?? item.terminal_reason_code
            ? <CanonicalInlineReason code={(item.lookahead_reason_code ?? item.static_reason_code ?? item.terminal_reason_code) as string} />
            : "无原因码"}{item.required_trade_count !== null ? ` · ${item.observed_trade_count ?? "未设置"}/${item.required_trade_count}` : ""}</td>
          <td><CopyableValue value={item.configuration_bundle_id} /> / <CopyableValue value={item.market_snapshot_id} /></td>
        </tr>)}
      </tbody></table></div></details>
    </section>
  );
}

export function CanonicalResearchPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const url = parseCanonicalUrlState("research", searchParams);
  const configurations = useCanonicalQuery(fetchCanonicalConfigurations, [], url.valid);
  const strategies = useCanonicalQuery((signal) => fetchCanonicalStrategies(signal, 200), [], url.valid);
  const plans = useCanonicalQuery(fetchCanonicalResearchPlans, [], url.valid);
  const selectedStrategyId = url.valid ? url.values.strategy ?? null : null;
  const selectedPlanId = url.valid ? url.values.plan ?? null : null;
  const selectedStrategy = useCanonicalQuery((signal) => fetchCanonicalStrategy(selectedStrategyId ?? "", signal), [selectedStrategyId], Boolean(selectedStrategyId));
  const selectedChain = useCanonicalQuery((signal) => fetchCanonicalResearchChain(selectedPlanId ?? "", signal), [selectedPlanId], Boolean(selectedPlanId));
  const selectedResults = useCanonicalQuery((signal) => fetchCanonicalResearchResults(selectedPlanId ?? "", signal), [selectedPlanId], Boolean(selectedPlanId));

  const configurationKnown = configurations.data
    ? canonicalStatusesKnown(configurations.data.status, ...configurations.data.items.flatMap((profile) => profile.versions.map((version) => version.lifecycle_status)))
    : true;
  const strategiesKnown = strategies.data
    ? canonicalStatusesKnown(strategies.data.status, ...strategies.data.items.flatMap((strategy) => [strategy.catalog_status, strategy.intake_status, strategy.validation_status, strategy.qualification_status]))
    : true;
  const plansKnown = plans.data
    ? canonicalStatusesKnown(plans.data.status, ...plans.data.items.flatMap((plan) => [plan.plan_status, plan.attempt_status, plan.qualification_status].filter((status) => status !== null)))
    : true;
  const configurationAvailability: CanonicalSelectorAvailability = !url.valid || configurations.error || !configurationKnown ? "unavailable" : configurations.loading ? "loading" : configurations.data?.status === "AVAILABLE" ? "ready" : "empty";
  const strategyAvailability: CanonicalSelectorAvailability = !url.valid || strategies.error || !strategiesKnown ? "unavailable" : strategies.loading ? "loading" : strategies.data?.status === "AVAILABLE" ? "ready" : "empty";
  const planAvailability: CanonicalSelectorAvailability = !url.valid || plans.error || !plansKnown ? "unavailable" : plans.loading ? "loading" : plans.data?.status === "AVAILABLE" ? "ready" : "empty";

  const contexts = configurationContextOptions(configurationKnown ? configurations.data : null);
  const contextValue = url.values.scope && url.values.workflow ? JSON.stringify([url.values.scope, url.values.workflow]) : "";
  const strategyOptions = strategySelectorOptions(strategiesKnown ? strategies.data : null);
  const selectedStrategySummary = strategies.data?.items.find((strategy) => strategy.strategy_id === selectedStrategyId) ?? null;
  const targetOptions = researchTargetSelectorOptions(plansKnown ? plans.data : null, selectedStrategySummary);
  const planOptions = researchPlanSelectorOptions(plansKnown ? plans.data : null, selectedStrategySummary, url.values.target ?? null);
  const strategyStale = Boolean(selectedStrategyId && strategyAvailability === "ready" && canonicalSelectionState(strategyOptions, selectedStrategyId) === "stale");
  const targetStale = Boolean(url.values.target && selectedStrategySummary && planAvailability === "ready" && canonicalSelectionState(targetOptions, url.values.target) === "stale");
  const planStale = Boolean(selectedPlanId && selectedStrategySummary && planAvailability === "ready" && canonicalSelectionState(planOptions, selectedPlanId) === "stale");
  const resultsKnown = selectedResults.data
    ? canonicalStatusesKnown(
      selectedResults.data.plan_status,
      ...(selectedResults.data.attempt ? [selectedResults.data.attempt.status] : []),
      ...(selectedResults.data.qualification ? [selectedResults.data.qualification.status] : []),
    )
    : true;
  const resultsLineageConflict = Boolean(selectedResults.data && (
    selectedResults.data.validation_plan_id !== selectedPlanId
    || (selectedStrategySummary && selectedResults.data.strategy_version_id !== selectedStrategySummary.current_version_id)
    || (url.values.target && selectedResults.data.research_target_id !== url.values.target)
  ));

  function commit(values: Readonly<Record<string, string | null | undefined>>) {
    setSearchParams(serializeCanonicalUrlState("research", values));
  }

  function selectContext(value: string | null) {
    const selected = contexts.find((option) => option.value === value) ?? null;
    commit({ ...url.values, scope: selected?.scopeKey ?? null, workflow: selected?.workflowKey ?? null });
  }

  function selectStrategy(value: string | null) {
    commit({ ...url.values, plan: null, strategy: value, target: null });
  }

  function selectTarget(value: string | null) {
    commit({ ...url.values, plan: null, target: value });
  }

  function selectPlan(value: string | null) {
    const selected = plans.data?.items.find((plan) => plan.validation_plan_id === value) ?? null;
    commit({ ...url.values, plan: value, target: selected?.research_target_id ?? url.values.target ?? null });
  }

  const committedResearchQuery = url.valid ? serializeCanonicalUrlState("research", url.values) : "";
  const committedResearchHref = `/v13/research${committedResearchQuery ? `?${committedResearchQuery}` : ""}`;
  const selectedStrategyHref = selectedStrategyId ? `/v13/strategies?${serializeCanonicalUrlState("strategies", { strategy: selectedStrategyId })}` : "/v13/strategies";

  return (
    <div className="canonical-v13-page">
      <PageHeader description="Research 与 Runtime readiness 独立读取；所有选择来自 Canonical API，稳定标识只写入 URL。" eyebrow="V1.3 canonical-only" title="研究与 Runtime 状态" />
      <CanonicalResearchHistory enabled={url.valid} />
      <section className="canonical-v13-selector-panel canonical-v13-selector-panel-wide" aria-label="研究上下文选择器">
        <CanonicalSearchSelect availability={configurationAvailability} label="Scope / Workflow 上下文" onChange={selectContext} options={contexts} value={contextValue} />
        <CanonicalSearchSelect availability={strategyAvailability} label="策略" onChange={selectStrategy} options={strategyOptions} value={selectedStrategyId ?? ""} />
        <CanonicalSearchSelect availability={planAvailability} disabled={!selectedStrategySummary} label="研究目标" onChange={selectTarget} options={targetOptions} value={url.values.target ?? ""} />
        <CanonicalSearchSelect availability={planAvailability} disabled={!selectedStrategySummary} label="Validation plan" onChange={selectPlan} options={planOptions} value={selectedPlanId ?? ""} />
      </section>
      {!url.valid ? <CanonicalStatePanel description="页面地址包含未知、重复或非法 selection；所有 selector 请求保持禁用。" kind="unknown" reasonCodes={url.problems} title="页面地址无效" /> : null}
      {configurations.error ? <CanonicalQueryError error={configurations.error} title="Scope / Workflow 选项暂不可用" /> : null}
      {strategies.error ? <CanonicalQueryError error={strategies.error} title="策略选项暂不可用" /> : null}
      {plans.error ? <CanonicalQueryError error={plans.error} title="研究目标与计划选项暂不可用" /> : null}
      {!configurationKnown || !strategiesKnown || !plansKnown ? <CanonicalStatePanel description="Selector projection 含未知状态；对应选项保持禁用。" kind="unknown" reasonCodes={["UNKNOWN_CONTRACT_VALUE"]} title="选择器合同漂移" /> : null}
      {contextValue && configurationAvailability === "ready" && canonicalSelectionState(contexts, contextValue) === "stale" ? <CanonicalStatePanel description="Committed Scope/Workflow 不在最新 API projection 中；未自动替换。" kind="unknown" reasonCodes={["SELECTED_CONTEXT_NOT_FOUND"]} title="研究范围已失效" /> : null}
      {strategyStale ? <CanonicalStatePanel description="Committed strategy 不在最新 API 策略目录中；未自动选择第一项。" kind="unknown" reasonCodes={["SELECTED_STRATEGY_NOT_FOUND"]} title="所选策略已失效" /> : null}
      {targetStale ? <CanonicalStatePanel description="Committed target 不属于所选策略当前版本的任何 API plan。" kind="unknown" reasonCodes={["SELECTED_TARGET_NOT_FOUND"]} title="所选研究目标已失效" /> : null}
      {planStale ? <CanonicalStatePanel description="Committed validation plan 与所选策略或目标上下文不一致。" kind="unknown" reasonCodes={["SELECTED_PLAN_NOT_FOUND"]} title="所选研究计划已失效" /> : null}
      {url.valid && !selectedStrategyId && !selectedPlanId ? <CanonicalStatePanel description="从 API 策略选项中选择目标策略后，页面才会投影连续研究步骤。" kind="empty" title="尚未选择研究策略" /> : null}
      {url.valid && selectedPlanId && !selectedStrategyId ? <CanonicalStatePanel description="当前 URL 只有 validation plan，无法核对它是否属于目标策略当前版本。" kind="unknown" reasonCodes={["RESEARCH_STRATEGY_CONTEXT_REQUIRED"]} title="研究策略上下文缺失" /> : null}
      {selectedStrategy.loading ? <CanonicalStatePanel description="正在读取所选策略的 canonical projection。" kind="loading" title="加载策略研究上下文" /> : null}
      {selectedStrategy.error ? <CanonicalQueryError error={selectedStrategy.error} title="所选策略研究上下文未知" /> : null}
      {selectedChain.loading ? <CanonicalStatePanel description="正在读取 exact validation plan。" kind="loading" title="加载研究链路" /> : null}
      {selectedChain.error ? <CanonicalQueryError error={selectedChain.error} title="研究链路状态未知" /> : null}
      {selectedResults.loading ? <CanonicalStatePanel description="正在读取 exact plan 的回测窗口、分数与资格 evidence。" kind="loading" title="加载回测结果" /> : null}
      {selectedResults.error ? <CanonicalQueryError error={selectedResults.error} title="回测结果状态未知" /> : null}
      {selectedResults.data && !resultsKnown ? <CanonicalStatePanel description="回测结果 projection 含未知状态；图表与摘要保持隐藏。" kind="unknown" reasonCodes={["UNKNOWN_CONTRACT_VALUE"]} title="回测结果合同漂移" /> : null}
      {resultsLineageConflict ? <CanonicalStatePanel description="Results projection 与 URL 中所选策略、目标或 plan 不一致；页面拒绝展示。" kind="unknown" reasonCodes={["RESEARCH_RESULT_LINEAGE_MISMATCH"]} title="回测结果 Lineage 不一致" /> : null}
      {selectedStrategyId && selectedStrategy.data && !strategyStale && !targetStale && !planStale && (!selectedPlanId || selectedChain.data) ? (
        <CanonicalResearchStepper chain={selectedChain.data} researchHref={committedResearchHref} selection={{ planId: selectedPlanId, strategyId: selectedStrategyId, targetId: url.values.target ?? null }} strategy={selectedStrategy.data} strategyHref={selectedStrategyHref} />
      ) : null}
      {url.valid ? <div className="canonical-v13-readiness-grid">
        <ReadinessCard dependencyKey={`${url.values.scope ?? ""}:${url.values.workflow ?? ""}`} loader={(signal) => fetchCanonicalResearchReadiness(url.values.scope, url.values.workflow, signal)} title="研究准备" />
        <ReadinessCard dependencyKey="runtime" loader={fetchCanonicalRuntimeReadiness} title="Runtime 准备" />
      </div> : null}
      {url.valid && selectedChain.data && !planStale ? <ResearchChainCard chain={selectedChain.data} /> : null}
      {url.valid && selectedResults.data && resultsKnown && !resultsLineageConflict && !strategyStale && !targetStale && !planStale ? <CanonicalResearchResults projection={selectedResults.data} /> : null}
      {url.valid && selectedResults.data && resultsKnown && !resultsLineageConflict && !strategyStale && !targetStale && !planStale ? <Phase9ReadinessBoard projection={selectedResults.data} /> : null}
      {url.valid ? <GateReceiptsCard /> : null}
      {(url.values.target || url.values.strategy) ? <CanonicalStatePanel description="Target/strategy 仅保存 committed URL selection；当前 readiness DTO 未提供按这两项过滤的事实，UI 不据此重算 readiness。" kind="pending" title="选择上下文" /> : null}
    </div>
  );
}
