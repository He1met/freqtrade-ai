export const EMPTY_TEXT = "暂无";

const STATUS_LABELS: Record<string, string> = {
  accepted: "已接受",
  approve: "批准",
  approved_for_deployment_record: "已批准创建部署记录",
  approved_for_review: "已批准进入人工复核",
  blocked: "已阻塞",
  blocked_by_preflight: "预检阻塞",
  candidate: "候选",
  critical: "严重",
  disabled: "已停用",
  draft: "草稿",
  empty: "暂无数据",
  enabled: "已启用",
  failed: "失败",
  failure: "失败",
  missing: "缺失",
  ok: "正常",
  pass: "通过",
  passed: "通过",
  pending: "等待中",
  planned: "已规划",
  present: "已配置",
  ready: "就绪",
  rejected: "已拒绝",
  skipped: "已跳过",
  stale: "已过期",
  success: "成功",
  succeeded: "成功",
  unavailable: "不可用",
  unknown: "未知",
  warning: "警告",
};

export function displayStatus(status: string | null | undefined): string {
  if (!status) {
    return EMPTY_TEXT;
  }

  const normalized = status.toLowerCase();
  return STATUS_LABELS[normalized] ?? status;
}

export function displaySource(source: string): string {
  if (source === "failed") {
    return "真实数据加载失败";
  }
  if (source === "fixture") {
    return "开发 fixture";
  }
  if (source === "api") {
    return "backend API 数据";
  }
  return source;
}

export function displayLoadState(isLoading: boolean, source: string): string {
  return isLoading ? "加载中" : displaySource(source);
}

export function displayBoolean(value: boolean): string {
  return value ? "是" : "否";
}

export function displayDataOrigin(source: string): string {
  if (source === "fixture") return "显式开发 fixture";
  if (source === "failed") return "真实数据不可用";
  return "backend API";
}

export function displayValue(value: number | string | boolean | null | undefined): string {
  return value === null || value === undefined || value === "" ? EMPTY_TEXT : String(value);
}
