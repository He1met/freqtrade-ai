import { Link, useLocation } from "react-router-dom";

export function NotFound() {
  const { pathname } = useLocation();

  return (
    <section className="page">
      <div className="not-found-panel" role="status" aria-live="polite">
        <span className="status-pill">404</span>
        <h1>Not Found</h1>
        <p>
          路径 <code>{pathname}</code> 未注册，可能是链接失效或地址输入错误。
        </p>
        <Link className="primary-link" to="/">
          返回 Dashboard
        </Link>
      </div>
    </section>
  );
}
