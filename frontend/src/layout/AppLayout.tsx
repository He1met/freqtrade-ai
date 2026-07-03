import { NavLink, Outlet } from "react-router-dom";

const navItems = [
  { to: "/", label: "总览" },
  { to: "/strategies", label: "策略" },
  { to: "/generation-runs", label: "生成批次" },
  { to: "/backtest-runs", label: "回测批次" },
  { to: "/backtest-tasks", label: "回测任务" },
  { to: "/ranking", label: "排行榜" },
  { to: "/freq-ui", label: "FreqUI" },
];

export function AppLayout() {
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">FA</span>
          <span>Freqtrade AI</span>
        </div>
        <nav className="nav-list" aria-label="主导航">
          {navItems.map((item) => (
            <NavLink key={item.to} to={item.to} end={item.to === "/"}>
              {item.label}
            </NavLink>
          ))}
        </nav>
      </aside>
      <main className="main-panel">
        <Outlet />
      </main>
    </div>
  );
}
