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
    label: "V1.3 Canonical",
    items: [
      { to: "/v13/submission", label: "策略提交" },
      { to: "/v13/strategies", label: "策略目录" },
      { to: "/v13/configuration", label: "配置中心" },
      { to: "/v13/market-data", label: "行情证据" },
      { to: "/v13/research", label: "研究与 Runtime" },
      { to: "/v13/optimization", label: "优化" },
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
    label: "Legacy 与历史",
    collapsible: true,
    description: "非 canonical production 权威",
    items: [
      { to: "/", label: "Legacy 总览" },
      { to: "/strategies", label: "Legacy 策略工厂" },
      { to: "/configuration", label: "Legacy 配置中心" },
      { to: "/research-queue", label: "Legacy 研究队列" },
      { to: "/okx-demo", label: "Legacy 模拟盘" },
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
