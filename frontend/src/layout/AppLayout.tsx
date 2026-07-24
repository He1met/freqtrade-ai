import { NavLink, Outlet, useLocation } from "react-router-dom";

import "./../styles/dashboard-shell.css";
import {
  navigationItems,
  navigationLabelForPath,
  navigationSections,
} from "./navigation";

export function AppLayout() {
  const { pathname } = useLocation();
  const currentLabel = navigationLabelForPath(pathname);

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">FA</span>
          <span>Freqtrade AI</span>
        </div>
        <nav className="nav-list desktop-nav" aria-label="主导航">
          {navigationSections.map((section) => (
            <section className="desktop-nav-section" key={section.label}>
              <h2>{section.label}</h2>
              <div className="desktop-nav-links">
                {section.items.map((item) => (
                  <NavLink key={item.to} to={item.to} end={item.to === "/"}>
                    {item.label}
                  </NavLink>
                ))}
              </div>
            </section>
          ))}
        </nav>
        <details className="mobile-nav">
          <summary aria-label={`打开主导航，当前页面：${currentLabel}`}>
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
            {navigationItems.map((item) => (
              <NavLink key={item.to} to={item.to} end={item.to === "/"}>
                {item.label}
              </NavLink>
            ))}
          </nav>
        </details>
      </aside>
      <main className="main-panel">
        <Outlet />
      </main>
    </div>
  );
}
