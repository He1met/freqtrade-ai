export type SourceNoticeKind = "hidden" | "healthy" | "fixture" | "failed";

export type SourceNoticeState = {
  kind: SourceNoticeKind;
  title: string;
  summary: string;
  sourceLabel: string;
  acceptance: string;
  reason?: string;
  nextAction?: string;
  context: string;
  note?: string;
};

export type SourceNoticeInput = {
  context: string;
  error?: string | null;
  isLoading: boolean;
  note?: string;
  source: string;
};

function trimmed(value: string | null | undefined): string | undefined {
  const result = value?.trim();
  return result ? result : undefined;
}

export function buildSourceNoticeState({
  context,
  error,
  isLoading,
  note,
  source,
}: SourceNoticeInput): SourceNoticeState {
  const normalizedError = trimmed(error);
  const normalizedNote = trimmed(note);

  if (isLoading) {
    return {
      kind: "hidden",
      title: "",
      summary: "",
      sourceLabel: "",
      acceptance: "",
      context,
    };
  }

  if (source === "api" && !normalizedError) {
    return {
      kind: "healthy",
      title: "真实数据来源",
      summary: "Backend API 已连接",
      sourceLabel: "database / api_aggregate",
      acceptance:
        "API 已连接仅表示请求成功；当前页面只展示通过现有真实性筛选的记录。暂无记录时仅表示暂无真实记录，不代表运行成功。",
      context,
      note: normalizedNote,
    };
  }

  if (source === "fixture" && !normalizedError) {
    return {
      kind: "fixture",
      title: "当前为开发 fixture",
      summary: "页面正在显示显式开发数据，不能作为真实运行或验收依据。",
      sourceLabel: "fixture（显式开发模式）",
      acceptance: "不可验收；fixture 不代表真实数据库记录或真实流程成功。",
      reason: "开发 fixture 已显式启用。",
      nextAction: "关闭 VITE_ENABLE_DEV_FIXTURES，并恢复 Backend API 后重新检查数据来源。",
      context,
      note: normalizedNote,
    };
  }

  return {
    kind: "failed",
    title: "真实数据加载失败",
    summary:
      normalizedError ??
      "Backend API 当前未返回可用数据；页面保持 fail-closed，不展示 mock/fallback 业务记录。",
    sourceLabel: "failed（空业务快照）",
    acceptance: "不可验收；当前没有可核验的真实业务数据。",
    reason: normalizedError ?? "当前只能确认 API 数据不可用，现有页面状态无法诊断更具体原因。",
    nextAction: "恢复 Backend API 并刷新页面；确认来源恢复为 database / api_aggregate 后再验收。",
    context,
    note: normalizedNote,
  };
}

export function sourceNoticeDetails(notice: SourceNoticeState): string {
  const rows = [
    `数据源：${notice.sourceLabel}`,
    `验收状态：${notice.acceptance}`,
    notice.reason ? `原因：${notice.reason}` : undefined,
    notice.nextAction ? `处理建议：${notice.nextAction}` : undefined,
    `影响范围：${notice.context}`,
    notice.note ? `备注：${notice.note}` : undefined,
  ];
  return rows.filter((row): row is string => Boolean(row)).join("\n");
}
