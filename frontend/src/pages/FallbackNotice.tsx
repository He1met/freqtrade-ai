import { ExpandableText, StatusBadge } from "../components/DisplayPrimitives";
import "../styles/source-notice.css";
import { buildSourceNoticeState, sourceNoticeDetails } from "./sourceNoticeState";

type FallbackNoticeProps = {
  context: string;
  error?: string | null;
  isLoading: boolean;
  note?: string;
  source: string;
};

export function FallbackNotice({ context, error, isLoading, note, source }: FallbackNoticeProps) {
  const notice = buildSourceNoticeState({ context, error, isLoading, note, source });

  if (notice.kind === "hidden") {
    return null;
  }

  const details = sourceNoticeDetails(notice);

  if (notice.kind === "healthy") {
    return (
      <section
        aria-label="数据来源状态"
        aria-live="polite"
        className="source-notice source-notice--healthy"
        data-testid="source-notice"
        role="status"
      >
        <StatusBadge label="真实数据" status="api" tone="success" />
        <span className="source-notice__summary">{notice.summary}</span>
        <ExpandableText className="source-notice__details" summary="查看来源详情" value={details} />
      </section>
    );
  }

  return (
    <aside
      aria-live="assertive"
      className={`source-notice source-notice--alert source-notice--${notice.kind}`}
      data-testid="source-notice"
      role="alert"
    >
      <span aria-hidden="true" className="source-notice__icon">
        !
      </span>
      <div className="source-notice__body">
        <div className="source-notice__heading">
          <strong>{notice.title}</strong>
          <StatusBadge
            label={notice.kind === "fixture" ? "不可用于验收" : "数据不可用"}
            status={notice.kind}
            tone={notice.kind === "fixture" ? "warning" : "danger"}
          />
        </div>
        <p>{notice.summary}</p>
        <ExpandableText className="source-notice__details" summary="查看原因与处理建议" value={details} />
      </div>
    </aside>
  );
}
