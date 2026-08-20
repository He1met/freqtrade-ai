export type NavigationItem = {
  to: string;
  label: string;
  end?: boolean;
};

export type AdvancedNavigationItem = NavigationItem & {
  purpose: string;
  source: string;
};

export type NavigationSection = {
  label: string;
  items: NavigationItem[];
  collapsible?: boolean;
  description?: string;
};

export const navigationSections: NavigationSection[] = [
  {
    label: "主要任务",
    items: [
      { to: "/v13", label: "工作台首页", end: true },
      { to: "/v13/strategies", label: "策略目录" },
      { to: "/v13/research", label: "研究与运行" },
    ],
  },
  {
    label: "配置与数据",
    items: [
      { to: "/v13/submission", label: "受控策略入库" },
      { to: "/v13/configuration", label: "配置中心" },
      { to: "/v13/market-data", label: "行情证据" },
      { to: "/v13/optimization", label: "优化" },
    ],
  },
  { label: "更多", items: [{ to: "/advanced", label: "高级入口" }] },
];

export const advancedNavigationSections: Array<{ label: string; description: string; items: AdvancedNavigationItem[]; kind: "development" | "legacy" }> = [
  {
    label: "开发实验",
    description: "本地实验与技术诊断，不进入 V1.3 正式候选或 production 状态。",
    kind: "development",
    items: [
      { to: "/local-strategy-lab", label: "Local Strategy Lab", purpose: "本地策略实验与证据浏览", source: "实验 API 与浏览器本地操作记录" },
      { to: "/operator-dashboard", label: "技术运行证据", purpose: "服务、环境与只读运行诊断", source: "Operator 与只读 Runtime API" },
    ],
  },
  {
    label: "Legacy 与历史证据",
    description: "保留兼容查询、旧书签和审计证据；不是 canonical V1.3 production 权威，也不作为 fallback。",
    kind: "legacy",
    items: [
      { to: "/legacy/dashboard", label: "Legacy 总览", purpose: "查看旧工作台汇总与历史诊断", source: "Legacy 聚合 API" },
      { to: "/strategies", label: "Legacy 策略工厂", purpose: "核对旧策略目录、版本与评分证据", source: "Legacy strategy/ranking API" },
      { to: "/configuration", label: "Legacy 配置中心", purpose: "查看旧配置兼容界面", source: "Legacy configuration API" },
      { to: "/research-queue", label: "Legacy 研究队列", purpose: "查看旧研究批次与串行验证证据", source: "Legacy research workspace API" },
      { to: "/okx-demo", label: "Legacy 模拟盘", purpose: "查看旧 OKX Demo 只读观测界面", source: "Legacy OKX Demo observability API" },
      { to: "/generation-runs", label: "生成批次", purpose: "审计历史策略生成批次", source: "Legacy generation-runs API" },
      { to: "/backtest-runs", label: "回测批次", purpose: "审计历史回测批次", source: "Legacy backtest-runs API" },
      { to: "/backtest-tasks", label: "回测任务", purpose: "审计历史回测任务与结果关联", source: "Legacy backtest-tasks API" },
      { to: "/hyperopt-runs", label: "Hyperopt 参数优化", purpose: "审计历史参数优化任务", source: "Legacy hyperopt-runs API" },
      { to: "/ranking", label: "策略排行榜", purpose: "审计历史评分与排名证据", source: "Legacy ranking API" },
      { to: "/live-governance", label: "候选治理证据", purpose: "查看旧候选治理与只读能力边界", source: "Legacy governance API" },
    ],
  },
];

export const navigationItems = navigationSections.flatMap((section) => section.items);
export const advancedNavigationItems = advancedNavigationSections.flatMap((section) => section.items);

export function isNavigationItemActive(pathname: string, item: NavigationItem): boolean {
  return item.end || item.to === "/"
    ? pathname === item.to
    : pathname === item.to || pathname.startsWith(`${item.to}/`);
}

export function navigationLabelForPath(pathname: string): string {
  return [...navigationItems, ...advancedNavigationItems].find((item) => isNavigationItemActive(pathname, item))?.label ?? "页面未找到";
}
