type FallbackNoticeProps = {
  context: string;
  error?: string | null;
  isLoading: boolean;
  note?: string;
  source: string;
};

export function FallbackNotice({ context, error, isLoading, note, source }: FallbackNoticeProps) {
  if (isLoading) {
    return null;
  }

  const reason =
    error ??
    (source === "api"
      ? "Backend API 已连接；页面仅保留 source_type=database/api_aggregate、core_data=true 且包含 database_ids 的核心记录。"
      : source === "fixture"
      ? "已显式启用开发 fixture；这些记录不来自真实数据库。"
      : "Backend API 当前未返回完整数据；页面已 fail-closed，不展示业务记录。");

  return (
    <aside className="notice fallback-notice" data-testid="fallback-notice" role="status" aria-live="polite">
      <div className="fallback-notice-heading">
        <strong>{source === "api" ? "仅显示真实核心数据" : source === "fixture" ? "当前显示显式开发 fixture" : "真实数据加载失败"}</strong>
        <span>{source === "api" ? "空结果不代表运行成功" : source === "fixture" ? "不可用于验收" : "未展示 mock/fallback 业务记录"}</span>
      </div>
      <dl className="fallback-notice-details">
        <div>
          <dt>数据源</dt>
          <dd>{source === "api" ? "database / api_aggregate" : source === "fixture" ? "fixture（显式开发模式）" : "failed（空业务快照）"}</dd>
        </div>
        <div>
          <dt>可验收</dt>
          <dd>{source === "api" ? "仅当前显示记录可验收；空结果、被过滤记录和缺失 ID 均不可验收。" : "否；缺少 database/api_aggregate、core_data=true 和 database_ids。"}</dd>
        </div>
        <div>
          <dt>原因</dt>
          <dd>{reason}</dd>
        </div>
        <div>
          <dt>解除条件</dt>
          <dd>{source === "api" ? "若页面为空，先运行真实本地流程；若 API 记录被过滤，补齐 data_source 和 database_ids 后刷新。" : source === "fixture" ? "关闭 VITE_ENABLE_DEV_FIXTURES，并恢复 backend API。" : "恢复 backend API，运行真实本地流程，刷新后确认数据来源为 database/api_aggregate。"}</dd>
        </div>
        <div>
          <dt>影响范围</dt>
          <dd>{context}</dd>
        </div>
        {note ? (
          <div>
            <dt>契约备注</dt>
            <dd>{note}</dd>
          </div>
        ) : null}
      </dl>
    </aside>
  );
}
