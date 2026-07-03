import type { DataSource } from "../api/types";

export const NONE_TEXT = "无";

const STATUS_LABELS: Record<string, string> = {
  active: "启用",
  api: "接口数据",
  archived: "归档",
  blocked: "阻塞",
  candidate: "候选",
  current: "当前",
  draft: "草稿",
  eliminated: "已淘汰",
  error: "错误",
  failed: "失败",
  fallback: "无运行数据",
  info: "信息",
  missing: "缺失",
  passed: "通过",
  pending: "等待中",
  ranked: "已入榜",
  running: "运行中",
  success: "成功",
  succeeded: "成功",
  unknown: "未知",
  warning: "警告",
};

const STRATEGY_SOURCE_LABELS: Record<string, string> = {
  ai_generated: "AI 生成",
  generated_local: "本地生成",
  imported: "导入",
  manual: "手工",
};

const STAGE_LABELS: Record<string, string> = {
  backtest_probe: "回测探测",
  generation: "策略生成",
  static_check: "静态审查",
  validation: "校验",
};

export function sourceLabel(source: DataSource, isLoading: boolean): string {
  if (isLoading) {
    return "加载中";
  }
  return source === "api" ? "本地运行数据" : "无运行数据";
}

export function statusLabel(status: string | null | undefined): string {
  if (!status) {
    return NONE_TEXT;
  }
  return STATUS_LABELS[status.toLowerCase()] ?? status;
}

export function optionalText(value: string | null | undefined): string {
  return value?.trim() ? value : NONE_TEXT;
}

export function strategySourceLabel(source: string): string {
  return STRATEGY_SOURCE_LABELS[source] ?? source;
}

export function stageLabel(stage: string): string {
  return STAGE_LABELS[stage] ?? stage;
}

export function diffStatusLabel(status: "has parent" | "no parent" | "missing"): string {
  if (status === "has parent") {
    return "有父版本";
  }
  if (status === "no parent") {
    return "无父版本";
  }
  return "缺失";
}
