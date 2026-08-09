import { useEffect, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";

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
        <NavLink key={item.to} to={item.to} end={item.to === "/"}>
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

  return (
    <div className="app-shell">
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
            {navigationSections.map((section) => (
              <section className="mobile-nav-section" key={section.label}>
                <h2>{section.label}</h2>
                {section.items.map((item) => (
                  <NavLink key={item.to} to={item.to} end={item.to === "/"}>
                    {item.label}
                  </NavLink>
                ))}
              </section>
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
