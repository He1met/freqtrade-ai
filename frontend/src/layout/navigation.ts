export type NavigationItem = {
  to: string;
  label: string;
};

export type NavigationSection = {
  label: string;
  items: NavigationItem[];
};

export const navigationSections: NavigationSection[] = [
  {
    label: "工作台",
    items: [
      { to: "/", label: "总览" },
      { to: "/strategies", label: "策略" },
      { to: "/generation-runs", label: "生成批次" },
      { to: "/local-strategy-lab", label: "Local Strategy Lab" },
    ],
  },
  {
    label: "研究与验证",
    items: [
      { to: "/backtest-runs", label: "回测批次" },
      { to: "/backtest-tasks", label: "回测任务" },
      { to: "/hyperopt-runs", label: "Hyperopt 参数优化" },
      { to: "/ranking", label: "策略排行榜" },
    ],
  },
  {
    label: "治理与运行",
    items: [
      { to: "/live-governance", label: "实盘候选治理" },
      { to: "/operator-dashboard", label: "运维面板" },
      { to: "/freq-ui", label: "Dry-run / FreqUI" },
    ],
  },
];

export const navigationItems = navigationSections.flatMap((section) => section.items);

export function isNavigationItemActive(pathname: string, item: NavigationItem): boolean {
  return item.to === "/"
    ? pathname === "/"
    : pathname === item.to || pathname.startsWith(`${item.to}/`);
}

export function navigationLabelForPath(pathname: string): string {
  return navigationItems.find((item) => isNavigationItemActive(pathname, item))?.label ?? "页面未找到";
}
