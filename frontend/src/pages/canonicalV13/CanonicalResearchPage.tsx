import { useEffect, useState, type FormEvent } from "react";
import { useSearchParams } from "react-router-dom";

import { fetchCanonicalResearchChain, fetchCanonicalResearchGates, fetchCanonicalResearchReadiness, fetchCanonicalRuntimeReadiness } from "../../api/canonicalV13Client";
import type { GateListProjection, ReadinessProjection, ResearchChainProjection } from "../../api/canonicalV13Types";
import { CopyableValue, PageHeader } from "../../components/DisplayPrimitives";
import { CanonicalInlineReason, CanonicalQueryError, CanonicalStatePanel, CanonicalStatus, useCanonicalQuery } from "./CanonicalStatePanel";
import { canonicalStatusesKnown, canonicalStatusPresentation, parseCanonicalUrlState, serializeCanonicalUrlState } from "./canonicalV13Model";

function ReadinessCard({
  dependencyKey,
  loader,
  title,
}: {
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
      {readiness.status === "BLOCKED" ? (
        <CanonicalStatePanel description="阻塞原因来自 API projection；UI 未进行二次计算。" kind="blocked" reasonCodes={readiness.reason_codes} title={`${title}已阻断`} />
      ) : null}
      {readiness.status === "PENDING_FIRST_BACKTEST" ? (
        <CanonicalStatePanel description="Bundle 已冻结，但尚无获授权的首次真实回测事实；UI 不将其提升为 READY。" kind="pending" reasonCodes={readiness.reason_codes} title="等待首次回测" />
      ) : null}
      <dl className="canonical-v13-definition-list">
        <div><dt>Scope</dt><dd>{readiness.scope_key ?? "未设置"}</dd></div>
        <div><dt>Workflow</dt><dd>{readiness.workflow_key ?? "未设置"}</dd></div>
        <div><dt>目标数量</dt><dd>{readiness.target_count ?? "未设置"}</dd></div>
        <div><dt>候选数量</dt><dd>{readiness.total_candidate_count ?? "未设置"}</dd></div>
        <div><dt>Bundle</dt><dd>{readiness.configuration_bundle_id ? <CopyableValue value={readiness.configuration_bundle_id} /> : "未设置"}</dd></div>
        <div><dt>Runtime instance</dt><dd>{readiness.runtime_instance_id ? <CopyableValue value={readiness.runtime_instance_id} /> : "未设置"}</dd></div>
      </dl>
    </section>
  );
}

function ResearchChainCard({ planId }: { planId: string }) {
  const query = useCanonicalQuery(
    (signal) => fetchCanonicalResearchChain(planId, signal),
    [planId],
  );
  if (query.loading) return <CanonicalStatePanel description="正在读取 exact validation plan。" kind="loading" title="加载研究链路" />;
  if (query.error) return <CanonicalQueryError error={query.error} title="研究链路状态未知" />;
  const chain = query.data as ResearchChainProjection;
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
      </div>
      <dl className="canonical-v13-definition-list">
        <div><dt>Validation plan</dt><dd><CopyableValue value={chain.validation_plan_id} /></dd></div>
        <div><dt>Plan digest</dt><dd><CopyableValue value={chain.validation_plan_digest} /></dd></div>
        <div><dt>Target</dt><dd>{chain.target_key} · <CopyableValue value={chain.research_target_id} /></dd></div>
        <div><dt>Strategy version</dt><dd><CopyableValue value={chain.strategy_version_id} /></dd></div>
        <div><dt>Attempt receipt</dt><dd>{chain.attempt_receipt_digest ? <CopyableValue value={chain.attempt_receipt_digest} /> : "UNSET"}</dd></div>
        <div><dt>Overall score</dt><dd>{chain.overall_score ?? "UNSET"}</dd></div>
        <div><dt>Score receipt</dt><dd>{chain.score_digest ? <CopyableValue value={chain.score_digest} /> : "UNSET"}</dd></div>
        <div><dt>Qualification 原因</dt><dd>{chain.qualification_reason_code ? <CanonicalInlineReason code={chain.qualification_reason_code} /> : "未设置"}</dd></div>
        <div><dt>Qualification receipt</dt><dd>{chain.qualification_decision_digest ? <CopyableValue value={chain.qualification_decision_digest} /> : "UNSET"}</dd></div>
      </dl>
    </section>
  );
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
  const known = canonicalStatusesKnown(
    projection.status,
    ...itemStatuses,
  );
  if (!known) return <CanonicalStatePanel description="Gate projection 返回未知状态；receipt 列表保持隐藏。" kind="unknown" reasonCodes={["UNKNOWN_CONTRACT_VALUE"]} title="Gate 回执合同漂移" />;
  if (!projection.items.length) return <CanonicalStatePanel description="尚无持久化 planless v3 gate receipt；UI 不使用历史评论或 Legacy 状态推断。" kind="pending" title="暂无 Gate 回执" />;
  return (
    <section className="canonical-v13-panel" data-readiness="gate-receipts">
      <div className="canonical-v13-heading-row"><h2>Canonical Static / Lookahead Gates</h2><CanonicalStatus status={projection.status} /></div>
      <div className="canonical-v13-table-wrap"><table><thead><tr><th>策略版本</th><th>Static</th><th>Lookahead</th><th>验证条件</th><th>原因 / 数量</th><th>冻结 Lineage</th></tr></thead><tbody>
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
      </tbody></table></div>
    </section>
  );
}

export function CanonicalResearchPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const url = parseCanonicalUrlState("research", searchParams);
  const [scope, setScope] = useState(url.values.scope ?? "");
  const [workflow, setWorkflow] = useState(url.values.workflow ?? "");
  const [target, setTarget] = useState(url.values.target ?? "");
  const [strategy, setStrategy] = useState(url.values.strategy ?? "");
  const [plan, setPlan] = useState(url.values.plan ?? "");
  const [selectionProblem, setSelectionProblem] = useState<string | null>(null);
  useEffect(() => {
    setScope(url.values.scope ?? "");
    setWorkflow(url.values.workflow ?? "");
    setTarget(url.values.target ?? "");
    setStrategy(url.values.strategy ?? "");
    setPlan(url.values.plan ?? "");
    setSelectionProblem(null);
  }, [searchParams]);

  function applySelection(event: FormEvent) {
    event.preventDefault();
    try {
      setSelectionProblem(null);
      setSearchParams(serializeCanonicalUrlState("research", {
        scope: scope || null,
        strategy: strategy || null,
        target: target || null,
        workflow: workflow || null,
        plan: plan || null,
      }));
    } catch (reason) {
      setSelectionProblem(reason instanceof Error ? reason.message : "INVALID_URL_STATE");
    }
  }

  return (
    <div className="canonical-v13-page">
      <PageHeader description="Research 与 Runtime readiness 独立读取；任一失败不会覆盖另一方事实。" eyebrow="V1.3 canonical-only" title="研究与 Runtime 状态" />
      <form className="canonical-v13-filter canonical-v13-filter-wide" onSubmit={applySelection}>
        <label>Scope<input value={scope} onChange={(event) => setScope(event.target.value)} /></label>
        <label>Workflow<input value={workflow} onChange={(event) => setWorkflow(event.target.value)} /></label>
        <label>Target<input value={target} onChange={(event) => setTarget(event.target.value)} /></label>
        <label>Strategy UUID<input value={strategy} onChange={(event) => setStrategy(event.target.value)} /></label>
        <label>Validation plan UUID<input value={plan} onChange={(event) => setPlan(event.target.value)} /></label>
        <button className="formal-primary-button" type="submit">写入 URL</button>
      </form>
      {selectionProblem ? <CanonicalStatePanel description="Scope/workflow 必须成对，strategy/plan 必须是 UUID；未提交新的读取。" kind="unknown" reasonCodes={[selectionProblem]} title="研究选择无效" /> : null}
      {!url.valid ? <CanonicalStatePanel description="scope/workflow 必须成对，strategy/plan 必须是 UUID；research 请求未发送。" kind="unknown" reasonCodes={url.problems} title="页面地址无效" /> : null}
      {url.valid ? <div className="canonical-v13-readiness-grid">
        <>
          <ReadinessCard
            dependencyKey={`${url.values.scope ?? ""}:${url.values.workflow ?? ""}`}
            loader={(signal) => fetchCanonicalResearchReadiness(url.values.scope, url.values.workflow, signal)}
            title="研究准备"
          />
        </>
        <ReadinessCard dependencyKey="runtime" loader={fetchCanonicalRuntimeReadiness} title="Runtime 准备" />
      </div> : null}
      {url.valid && url.values.plan ? <ResearchChainCard planId={url.values.plan} /> : null}
      {url.valid ? <GateReceiptsCard /> : null}
      {(url.values.target || url.values.strategy) ? (
        <CanonicalStatePanel
          description="Target/strategy 仅保存 committed URL selection；当前 #719 readiness DTO 未提供按这两项过滤的事实，UI 不据此重算 readiness。"
          kind="pending"
          title="选择上下文"
        />
      ) : null}
    </div>
  );
}
