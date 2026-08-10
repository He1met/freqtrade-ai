export type NavigationItem = {
  to: string;
  label: string;
};

export type NavigationSection = {
  label: string;
  items: NavigationItem[];
  collapsible?: boolean;
  description?: string;
};

export const navigationSections: NavigationSection[] = [
  {
    label: "正式工作台",
    items: [
      { to: "/", label: "总览" },
      { to: "/strategies", label: "策略工厂" },
      { to: "/research-queue", label: "研究队列" },
      { to: "/okx-demo", label: "模拟盘" },
    ],
  },
  {
    label: "开发实验",
    collapsible: true,
    description: "非正式候选",
    items: [
      { to: "/local-strategy-lab", label: "Local Strategy Lab" },
      { to: "/operator-dashboard", label: "技术运行证据" },
    ],
  },
  {
    label: "高级与历史",
    collapsible: true,
    description: "兼容旧入口",
    items: [
      { to: "/generation-runs", label: "生成批次" },
      { to: "/backtest-runs", label: "回测批次" },
      { to: "/backtest-tasks", label: "回测任务" },
      { to: "/hyperopt-runs", label: "Hyperopt 参数优化" },
      { to: "/live-governance", label: "候选治理证据" },
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
