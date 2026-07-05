type FallbackNoticeProps = {
  context: string;
  error?: string | null;
  isLoading: boolean;
  note?: string;
  source: string;
};

export function FallbackNotice({ context, error, isLoading, note, source }: FallbackNoticeProps) {
  if (isLoading || (source !== "fallback" && !error)) {
    return null;
  }

  const reason =
    error ??
    "Backend API 当前没有返回完整 MVP 数据，页面使用前端受控 fixture 保持只读检查。";

  return (
    <aside className="notice fallback-notice" data-testid="fallback-notice" role="status" aria-live="polite">
      <div className="fallback-notice-heading">
        <strong>当前显示受控 fallback 数据</strong>
        <span>不是实时 backend API 结果</span>
      </div>
      <dl className="fallback-notice-details">
        <div>
          <dt>数据源</dt>
          <dd>fallback / fixture / mockMvpData</dd>
        </div>
        <div>
          <dt>可验收</dt>
          <dd>否；缺少 database/api_aggregate、core_data=true 和 database_ids。</dd>
        </div>
        <div>
          <dt>fallback 原因</dt>
          <dd>{reason}</dd>
        </div>
        <div>
          <dt>解除条件</dt>
          <dd>恢复 backend API，运行真实本地流程，刷新后确认相关 SourceMarker 显示核心数据来源。</dd>
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
