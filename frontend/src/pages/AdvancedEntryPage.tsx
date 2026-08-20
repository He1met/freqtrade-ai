import { Link } from "react-router-dom";

import { advancedNavigationSections } from "../layout/navigation";
import "../styles/advanced-entry.css";

export function AdvancedEntryPage() {
  return (
    <section className="formal-page advanced-entry-page">
      <header className="advanced-entry-header">
        <div><span className="formal-kicker">Advanced and historical</span><h1>高级入口</h1></div>
        <p>这里仅保留开发实验、Legacy 兼容查询与历史审计入口。它们不会向 V1.3 工作台提供资格、readiness、Runtime 或运行状态。</p>
        <Link className="primary-link" to="/v13">返回 V1.3 工作台</Link>
      </header>
      <aside className="advanced-entry-boundary" role="note">
        <strong>事实边界</strong>
        <span>V1.3 页面只读取 <code>/api/canonical-v13</code>。本页不会汇总、转换或回填以下页面的数据。</span>
      </aside>
      {advancedNavigationSections.map((section) => (
        <section aria-labelledby={`advanced-${section.kind}`} className="advanced-entry-section" key={section.kind}>
          <div className="advanced-entry-section-heading">
            <div><span className="formal-kicker">{section.kind === "legacy" ? "Legacy boundary" : "Development boundary"}</span><h2 id={`advanced-${section.kind}`}>{section.label}</h2></div>
            <p>{section.description}</p>
          </div>
          <div className="advanced-entry-grid">
            {section.items.map((item) => (
              <Link className="advanced-entry-card" key={item.to} to={item.to}>
                <strong>{item.label}</strong>
                <span>{item.purpose}</span>
                <dl><div><dt>数据来源</dt><dd>{item.source}</dd></div><div><dt>边界</dt><dd>{section.kind === "legacy" ? "非 canonical production 权威" : "不进入正式候选生命周期"}</dd></div></dl>
                <b>打开保留页面 →</b>
              </Link>
            ))}
          </div>
        </section>
      ))}
    </section>
  );
}
