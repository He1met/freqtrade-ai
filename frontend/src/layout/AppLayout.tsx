import { NavLink, Outlet, useLocation } from "react-router-dom";

const navItems = [
  { to: "/", label: "总览" },
  { to: "/strategies", label: "策略" },
  { to: "/generation-runs", label: "生成批次" },
  { to: "/local-strategy-lab", label: "Local Strategy Lab" },
  { to: "/backtest-runs", label: "回测批次" },
  { to: "/backtest-tasks", label: "回测任务" },
  { to: "/hyperopt-runs", label: "Hyperopt 参数优化" },
  { to: "/live-governance", label: "实盘候选治理" },
  { to: "/operator-dashboard", label: "运维面板" },
  { to: "/ranking", label: "策略排行榜" },
  { to: "/freq-ui", label: "Dry-run / FreqUI" },
];

export function AppLayout() {
  const { pathname } = useLocation();
  const currentItem =
    navItems.find((item) =>
      item.to === "/" ? pathname === "/" : pathname === item.to || pathname.startsWith(`${item.to}/`),
    ) ?? { label: "未找到页面" };

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">FA</span>
          <span>Freqtrade AI</span>
        </div>
        <nav className="nav-list desktop-nav" aria-label="主导航">
          {navItems.map((item) => (
            <NavLink key={item.to} to={item.to} end={item.to === "/"}>
              {item.label}
            </NavLink>
          ))}
        </nav>
        <details className="mobile-nav">
          <summary aria-label={`打开主导航，当前页面：${currentItem.label}`}>
            <span className="mobile-nav-current">
              <span>当前页面</span>
              <strong>{currentItem.label}</strong>
            </span>
            <span className="mobile-nav-icon" aria-hidden="true">
              <span />
              <span />
              <span />
            </span>
          </summary>
          <nav className="mobile-nav-list" aria-label="移动端主导航">
            {navItems.map((item) => (
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
