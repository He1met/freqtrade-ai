import { useEffect, useState, type FormEvent } from "react";
import { useSearchParams } from "react-router-dom";

import { fetchCanonicalOptimizations } from "../../api/canonicalV13Client";
import { CopyableValue, PageHeader } from "../../components/DisplayPrimitives";
import { CanonicalQueryError, CanonicalStatePanel, CanonicalStatus, useCanonicalQuery } from "./CanonicalStatePanel";
import { canonicalStatusesKnown, parseCanonicalUrlState, serializeCanonicalUrlState } from "./canonicalV13Model";

export function CanonicalOptimizationPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const url = parseCanonicalUrlState("optimization", searchParams);
  const query = useCanonicalQuery(fetchCanonicalOptimizations, [], url.valid);
  const [strategy, setStrategy] = useState(url.values.strategy ?? "");
  const [target, setTarget] = useState(url.values.target ?? "");
  const [selectionProblem, setSelectionProblem] = useState<string | null>(null);
  useEffect(() => {
    setStrategy(url.values.strategy ?? "");
    setTarget(url.values.target ?? "");
    setSelectionProblem(null);
  }, [searchParams]);
  const contractKnown = query.data
    ? canonicalStatusesKnown(query.data.status, ...query.data.items.map((item) => item.status))
    : true;

  function applySelection(event: FormEvent) {
    event.preventDefault();
    try {
      setSelectionProblem(null);
      setSearchParams(serializeCanonicalUrlState("optimization", { strategy: strategy || null, target: target || null }));
    } catch (reason) {
      setSelectionProblem(reason instanceof Error ? reason.message : "INVALID_URL_STATE");
    }
  }

  return (
    <div className="canonical-v13-page">
      <PageHeader description="Optimization 是首次真实 backtest 之后的事实；空状态不生成 0 分或失败结论。" eyebrow="V1.3 canonical-only" title="Optimization" />
      <form className="canonical-v13-filter" onSubmit={applySelection}>
        <label>Strategy UUID<input value={strategy} onChange={(event) => setStrategy(event.target.value)} /></label>
        <label>Target<input value={target} onChange={(event) => setTarget(event.target.value)} /></label>
        <button className="formal-primary-button" type="submit">写入 URL</button>
      </form>
      {selectionProblem ? <CanonicalStatePanel description="Strategy 必须是 UUID，target 必须符合 URL 合同；未提交新的读取。" kind="unknown" reasonCodes={[selectionProblem]} title="INVALID_SELECTION" /> : null}
      {!url.valid ? <CanonicalStatePanel description="Optimization URL state 无效。" kind="unknown" reasonCodes={url.problems} title="INVALID_URL_STATE" /> : null}
      {query.loading ? <CanonicalStatePanel description="正在读取 optimization projection。" kind="loading" title="加载 optimization" /> : null}
      {query.error ? <CanonicalQueryError error={query.error} title="Optimization 状态未知" /> : null}
      {query.data && !contractKnown ? <CanonicalStatePanel description="Optimization projection 含未知 enum；列表保持禁用。" kind="unknown" reasonCodes={["UNKNOWN_CONTRACT_VALUE"]} title="Optimization 合同漂移" /> : null}
      {contractKnown && query.data?.status === "PENDING_FIRST_BACKTEST" ? (
        <CanonicalStatePanel description="尚未获得首次真实 backtest 事实；没有 optimization run、score 或 qualification 可展示。" kind="pending" reasonCodes={["PENDING_FIRST_BACKTEST"]} title="等待首次回测" />
      ) : null}
      {contractKnown && query.data?.status === "AVAILABLE" ? (
        <section className="canonical-v13-panel">
          <div className="canonical-v13-heading-row"><h2>Optimization runs</h2><CanonicalStatus status={query.data.status} /></div>
          <div className="canonical-v13-card-list">
            {query.data.items.map((run) => (
              <article className="canonical-v13-data-card" key={run.optimization_run_id}>
                <div className="canonical-v13-heading-row"><CopyableValue value={run.optimization_run_id} /><CanonicalStatus status={run.status} /></div>
                <span>Baseline qualification: {run.baseline_qualification_decision_id}</span>
                <span>Created: {run.created_at}</span>
              </article>
            ))}
          </div>
        </section>
      ) : null}
      {(url.values.target || url.values.strategy) ? <CanonicalStatePanel description="当前 #719 optimization DTO 不包含 target/strategy filter lineage；URL 仅保留 selection，页面不伪造过滤结果。" kind="pending" title="Selection context" /> : null}
    </div>
  );
}
