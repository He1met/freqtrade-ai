import { Link, useLocation } from "react-router-dom";

import { CompactText, PageHeader } from "../components/DisplayPrimitives";

export function NotFound() {
  const { pathname } = useLocation();

  return (
    <section className="page not-found-page">
      <PageHeader
        description="当前地址没有对应的项目页面。"
        eyebrow="导航提示"
        title="页面未找到"
      />
      <div className="not-found-panel" role="status" aria-live="polite">
        <span className="not-found-code">404</span>
        <h2>无法打开这个页面</h2>
        <p>链接可能已经失效，或者地址输入有误。</p>
        <div className="not-found-path">
          <span>当前路径</span>
          <CompactText label="当前路径" mono value={pathname} />
        </div>
        <Link className="primary-link" to="/">
          返回总览
        </Link>
      </div>
    </section>
  );
}
