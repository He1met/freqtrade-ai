export const EMPTY_TEXT = "暂无";

export const UI_TERMS = {
  artifact: "Artifact",
  bestParams: "最佳参数",
  config: "配置",
  dataSource: "数据源",
  envPresence: "环境变量配置状态",
  notFound: "页面未找到",
  reason: "原因",
  result: "结果",
  rollbackPlan: "回滚方案",
  strategyIdea: "策略构想",
} as const;

const STATUS_LABELS: Readonly<Record<string, string>> = {
  accepted: "已接受",
  acceptable: "可验收",
  approve: "批准",
  approved_for_deployment_record: "已批准创建部署记录",
  approved_for_review: "已批准进入人工复核",
  api_gap: "API 数据缺口",
  blocked: "已阻塞",
  blocked_by_preflight: "预检阻塞",
  cancelled: "已取消",
  candidate: "候选",
  completed: "已完成",
  critical: "严重",
  disabled: "已停用",
  draft: "草稿",
  empty: "暂无数据",
  enabled: "已启用",
  error: "错误",
  expire: "使其过期",
  expired: "已过期",
  failed: "失败",
  failure: "失败",
  idle: "空闲",
  info: "提示",
  missing: "缺失",
  not_acceptable: "不可验收",
  not_run: "尚未运行",
  ok: "正常",
  pass: "通过",
  passed: "通过",
  pending: "等待中",
  planned: "已规划",
  present: "已配置",
  queued: "排队中",
  running: "运行中",
  ready: "就绪",
  reject: "拒绝",
  rejected: "已拒绝",
  revoke: "撤销",
  skipped: "已跳过",
  stale: "已过期",
  starting: "启动中",
  stopped: "已停止",
  stopping: "停止中",
  success: "成功",
  succeeded: "成功",
  unavailable: "不可用",
  unauthorized: "未授权",
  unknown: "未知",
  warning: "警告",
};

export type StatusTone = "success" | "danger" | "warning" | "info" | "neutral";

const STATUS_TONES: Readonly<Record<string, StatusTone>> = {
  accepted: "success",
  acceptable: "success",
  approve: "success",
  approved_for_deployment_record: "success",
  approved_for_review: "success",
  api_gap: "warning",
  blocked: "warning",
  blocked_by_preflight: "warning",
  cancelled: "neutral",
  candidate: "info",
  completed: "success",
  critical: "danger",
  disabled: "neutral",
  error: "danger",
  expired: "warning",
  failed: "danger",
  failure: "danger",
  missing: "danger",
  not_acceptable: "warning",
  not_run: "neutral",
  ok: "success",
  pass: "success",
  passed: "success",
  pending: "info",
  planned: "info",
  present: "success",
  queued: "info",
  ready: "success",
  reject: "danger",
  rejected: "danger",
  revoke: "warning",
  running: "info",
  stale: "warning",
  starting: "info",
  stopped: "neutral",
  stopping: "info",
  success: "success",
  succeeded: "success",
  unavailable: "neutral",
  unauthorized: "danger",
  warning: "warning",
};

function normalizeStatus(status: string): string {
  return status.trim().toLowerCase().replace(/[-\s]+/g, "_");
}

export function displayStatus(status: string | null | undefined): string {
  if (!status?.trim()) {
    return EMPTY_TEXT;
  }

  const normalized = normalizeStatus(status);
  return STATUS_LABELS[normalized] ?? status;
}

export function statusTone(status: string | null | undefined): StatusTone {
  if (!status?.trim()) {
    return "neutral";
  }
  return STATUS_TONES[normalizeStatus(status)] ?? "neutral";
}

export function displayStatusWithRaw(status: string | null | undefined): string {
  const label = displayStatus(status);
  if (!status?.trim() || label === status) {
    return label;
  }
  return `${label}（${status}）`;
}

export function displaySource(source: string | null | undefined): string {
  if (!source?.trim()) {
    return EMPTY_TEXT;
  }
  if (source === "failed") {
    return "真实数据加载失败";
  }
  if (source === "fixture") {
    return "开发 fixture";
  }
  if (source === "api") {
    return "backend API 数据";
  }
  if (source === "api_aggregate") {
    return "API 聚合数据";
  }
  if (source === "database") {
    return "数据库数据";
  }
  if (source === "fallback") {
    return "降级数据";
  }
  return source;
}

export function displayLoadState(isLoading: boolean, source: string | null | undefined): string {
  return isLoading ? "加载中" : displaySource(source);
}

export function displayBoolean(value: boolean | null | undefined): string {
  if (value === null || value === undefined) {
    return EMPTY_TEXT;
  }
  return value ? "是" : "否";
}

export function displayDataOrigin(source: string | null | undefined): string {
  if (!source?.trim()) return EMPTY_TEXT;
  if (source === "fixture") return "显式开发 fixture";
  if (source === "failed") return "真实数据不可用";
  if (source === "database") return "数据库";
  if (source === "api_aggregate") return "API 聚合";
  if (source === "fallback") return "降级数据";
  return "backend API";
}

export function displayValue(value: number | string | boolean | null | undefined): string {
  if (typeof value === "boolean") {
    return displayBoolean(value);
  }
  return value === null || value === undefined || value === "" ? EMPTY_TEXT : String(value);
}

export interface NumberDisplayOptions {
  maximumFractionDigits?: number;
  minimumFractionDigits?: number;
  useGrouping?: boolean;
}

export function displayNumber(
  value: number | null | undefined,
  options: NumberDisplayOptions = {},
): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return EMPTY_TEXT;
  }
  return new Intl.NumberFormat("zh-CN", {
    maximumFractionDigits: options.maximumFractionDigits ?? 2,
    minimumFractionDigits: options.minimumFractionDigits ?? 0,
    useGrouping: options.useGrouping ?? true,
  }).format(value);
}

export interface PercentDisplayOptions extends NumberDisplayOptions {
  /** `ratio` converts 0.12 to 12%; `percent` renders 12 as 12%. */
  input?: "ratio" | "percent";
}

export function displayPercent(
  value: number | null | undefined,
  options: PercentDisplayOptions = {},
): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return EMPTY_TEXT;
  }
  const percentValue = options.input === "percent" ? value : value * 100;
  return `${displayNumber(percentValue, {
    maximumFractionDigits: options.maximumFractionDigits ?? 2,
    minimumFractionDigits: options.minimumFractionDigits,
    useGrouping: options.useGrouping,
  })}%`;
}

export interface DateTimeDisplayOptions {
  includeSeconds?: boolean;
  timeZone?: string;
}

export function displayDateTime(
  value: string | number | Date | null | undefined,
  options: DateTimeDisplayOptions = {},
): string {
  if (value === null || value === undefined || value === "") {
    return EMPTY_TEXT;
  }
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) {
    return EMPTY_TEXT;
  }
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: options.includeSeconds ? "2-digit" : undefined,
    hour12: false,
    timeZone: options.timeZone,
  }).format(date);
}
