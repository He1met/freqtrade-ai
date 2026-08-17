import { Link } from "react-router-dom";

import type { ResearchChainProjection, StrategyProjection } from "../../api/canonicalV13Types";
import { CanonicalInlineReason, CanonicalStatus } from "./CanonicalStatePanel";
import { canonicalResearchWorkflow, type CanonicalResearchStepState } from "./canonicalV13Workflow";

const STATE_LABEL: Readonly<Record<CanonicalResearchStepState, string>> = {
  blocked: "已阻断",
  complete: "已完成",
  current: "当前步骤",
  "not-started": "未开始",
  unknown: "未知",
};

export function CanonicalResearchStepper({
  chain,
  researchHref,
  selection,
  strategy,
  strategyHref,
}: {
  chain: ResearchChainProjection | null;
  researchHref: string;
  selection: { planId: string | null; strategyId: string; targetId: string | null };
  strategy: StrategyProjection;
  strategyHref: string;
}) {
  const workflow = canonicalResearchWorkflow({
    chain,
    links: { researchHref, strategyHref },
    selection,
    strategy,
  });
  const titleId = `canonical-research-workflow-${strategy.strategy_id}`;
  return (
    <section className="canonical-v13-workflow" aria-labelledby={titleId}>
      <header className="canonical-v13-workflow-header">
        <div>
          <span className="canonical-v13-home-kicker">API 投影的连续流程</span>
          <h2 id={titleId}>{strategy.display_name} 的研究流程</h2>
          <p>步骤只来自所选策略与 exact validation plan；页面访问、点击和本地缓存不会改变阶段。</p>
        </div>
        <Link className="canonical-v13-workflow-research-link" to={workflow.researchLink.to}>{workflow.researchLink.label}</Link>
      </header>
      <nav aria-label={`${strategy.display_name} 的策略研究流程`}>
        <ol className="canonical-v13-workflow-steps">
          {workflow.steps.map((item, index) => (
            <li
              aria-current={workflow.currentStepId === item.id ? "step" : undefined}
              data-step-state={item.state}
              key={item.id}
            >
              <span aria-hidden="true" className="canonical-v13-workflow-marker">{index + 1}</span>
              <div className="canonical-v13-workflow-step-copy">
                <span className="canonical-v13-workflow-state">{STATE_LABEL[item.state]}</span>
                <h3>{item.label}</h3>
                <p>{item.summary}</p>
                <div className="canonical-v13-workflow-evidence">
                  {item.apiStatus ? <CanonicalStatus status={item.apiStatus} /> : <span>API 状态：未提供</span>}
                  {item.reasonCodes.map((code) => <CanonicalInlineReason code={code} key={code} />)}
                </div>
                <details className="canonical-v13-workflow-diagnostics">
                  <summary>高级诊断</summary>
                  <dl>
                    <div><dt>步骤</dt><dd><code>{item.id}</code></dd></div>
                    <div><dt>原始 API 状态</dt><dd><code>{item.apiStatus ?? "NULL"}</code></dd></div>
                    <div><dt>策略版本</dt><dd><code>{strategy.current_version_id}</code></dd></div>
                    <div><dt>Validation plan</dt><dd><code>{chain?.validation_plan_id ?? "UNSELECTED"}</code></dd></div>
                  </dl>
                </details>
              </div>
            </li>
          ))}
        </ol>
      </nav>
      <footer className="canonical-v13-workflow-next">
        <span>建议下一步</span>
        <Link to={workflow.nextAction.to}>{workflow.nextAction.label}<span aria-hidden="true"> →</span></Link>
      </footer>
    </section>
  );
}
