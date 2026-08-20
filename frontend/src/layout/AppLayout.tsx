import { useEffect, useRef, useState } from "react";
import { Link, NavLink, Outlet, useLocation } from "react-router-dom";

import "./../styles/dashboard-shell.css";
import {
  isNavigationItemActive,
  navigationItems,
  navigationLabelForPath,
  navigationSections,
} from "./navigation";

function NavigationLinks({ items }: { items: typeof navigationItems }) {
  return (
    <div className="desktop-nav-links">
      {items.map((item) => (
        <NavLink key={item.to} to={item.to} end={item.end ?? item.to === "/"}>
          {item.label}
        </NavLink>
      ))}
    </div>
  );
}

function CollapsibleNavigationSection({
  label,
  description,
  items,
  pathname,
}: {
  label: string;
  description?: string;
  items: typeof navigationItems;
  pathname: string;
}) {
  const containsActiveItem = items.some((item) => isNavigationItemActive(pathname, item));
  const [open, setOpen] = useState(containsActiveItem);

  useEffect(() => {
    if (containsActiveItem) setOpen(true);
  }, [containsActiveItem]);

  return (
    <details
      className="desktop-nav-section desktop-nav-disclosure"
      onToggle={(event) => setOpen(event.currentTarget.open)}
      open={open}
    >
      <summary>
        <span>{label}</span>
        {description ? <small>{description}</small> : null}
      </summary>
      <NavigationLinks items={items} />
    </details>
  );
}

export function AppLayout() {
  const { pathname } = useLocation();
  const currentLabel = navigationLabelForPath(pathname);
  const [mobileOpen, setMobileOpen] = useState(false);
  const mainRef = useRef<HTMLElement>(null);
  const developmentRoute = pathname.startsWith("/operator-dashboard") || pathname.startsWith("/local-strategy-lab");
  const developmentReturn = pathname.startsWith("/local-strategy-lab")
    ? { label: "返回策略目录", to: "/v13/strategies" }
    : { label: "返回研究与运行", to: "/v13/research" };
  const historicalRoute = ["/legacy/dashboard", "/generation-runs", "/backtest-runs", "/backtest-tasks", "/hyperopt-runs", "/ranking", "/live-governance"]
    .some((prefix) => pathname.startsWith(prefix));

  useEffect(() => {
    setMobileOpen(false);
    mainRef.current?.focus();
    window.scrollTo({ top: 0 });
  }, [pathname]);

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">跳到主要内容</a>
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">FA</span>
          <span>Freqtrade AI</span>
        </div>
        <nav className="nav-list desktop-nav" aria-label="主导航">
          {navigationSections.map((section) => (
            section.collapsible ? (
              <CollapsibleNavigationSection
                description={section.description}
                items={section.items}
                key={section.label}
                label={section.label}
                pathname={pathname}
              />
            ) : (
              <section className="desktop-nav-section" key={section.label}>
                <h2>{section.label}</h2>
                <NavigationLinks items={section.items} />
              </section>
            )
          ))}
        </nav>
        <details
          className="mobile-nav"
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.preventDefault();
              event.currentTarget.open = false;
              setMobileOpen(false);
            }
          }}
          onToggle={(event) => setMobileOpen(event.currentTarget.open)}
          open={mobileOpen}
        >
          <summary aria-label={`${mobileOpen ? "关闭" : "打开"}主导航，当前页面：${currentLabel}`}>
            <span className="mobile-nav-current">
              <span>当前页面</span>
              <strong>{currentLabel}</strong>
            </span>
            <span className="mobile-nav-icon" aria-hidden="true">
              <span />
              <span />
              <span />
            </span>
          </summary>
          <nav className="mobile-nav-list" aria-label="移动端主导航">
            {navigationSections.map((section) => section.collapsible ? (
              <details className="mobile-nav-section mobile-nav-disclosure" key={section.label} open={section.items.some((item) => isNavigationItemActive(pathname, item)) || undefined}>
                <summary><strong>{section.label}</strong><small>{section.description}</small></summary>
                {section.items.map((item) => <NavLink end={item.end} key={item.to} to={item.to}>{item.label}</NavLink>)}
              </details>
            ) : (
              <section className="mobile-nav-section" key={section.label}>
                <h2>{section.label}</h2>
                {section.items.map((item) => <NavLink key={item.to} to={item.to} end={item.end ?? item.to === "/"}>{item.label}</NavLink>)}
              </section>
            ))}
          </nav>
        </details>
      </aside>
      <main className="main-panel" id="main-content" ref={mainRef} tabIndex={-1}>
        {developmentRoute ? (
          <aside
            className="formal-context-banner"
            data-context={pathname.startsWith("/local-strategy-lab") ? "local-lab" : "operator"}
            data-kind="development"
          >
            <div><strong>开发实验</strong><span>本页结果不进入正式候选生命周期，也不计入正式工作台数字。</span></div>
            <Link to={developmentReturn.to}>{developmentReturn.label}</Link>
          </aside>
        ) : historicalRoute ? (
          <aside className="formal-context-banner" data-kind="historical">
            <div><strong>高级与历史证据</strong><span>本页保留 legacy 兼容查询，但不是 V1.3 canonical production 权威，也不会作为 canonical fallback。</span></div>
            <Link to="/v13">返回 V1.3 工作台</Link>
          </aside>
        ) : null}
        <Outlet />
      </main>
    </div>
  );
}
