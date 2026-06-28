import { NavLink, Outlet } from "react-router-dom";

const navItems = [
  { to: "/", label: "Dashboard" },
  { to: "/strategies", label: "Strategies" },
  { to: "/generation-runs", label: "Generation Runs" },
  { to: "/backtest-runs", label: "Backtest Runs" },
  { to: "/backtest-tasks", label: "Backtest Tasks" },
  { to: "/ranking", label: "Ranking" },
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
        <nav className="nav-list" aria-label="Primary">
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
