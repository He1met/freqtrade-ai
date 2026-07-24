import {
  CopyableValue,
  ExpandableText,
  StatusBadge,
} from "../../components/DisplayPrimitives";
import type { SubmissionState } from "./EvidencePanels";
import { submissionDisplayModel } from "./submissionDisplay";

type Props = {
  submission: SubmissionState;
};

export function SubmissionStatusPanel({ submission }: Props) {
  const display = submissionDisplayModel(submission);
  const hasRequestDetails =
    Boolean(display.runId) ||
    display.statusCode !== null ||
    Boolean(display.statusText) ||
    Boolean(display.detail);

  return (
    <section className="lab-status-panel lab-submit-status" aria-live="polite" aria-label="提交状态">
      <div className="lab-submit-status__heading">
        <StatusBadge label={display.label} showRaw status={display.status} tone={display.tone} />
        <div>
          <strong>{display.title}</strong>
          <p>{display.summary}</p>
        </div>
      </div>

      <dl className="lab-submit-status__summary">
        <div>
          <dt>生成记录</dt>
          <dd>{display.status === "SUCCESS" ? "1 个已持久化" : "未确认成功"}</dd>
        </div>
        <div>
          <dt>策略 / 版本</dt>
          <dd>
            {display.strategyCount} / {display.versionCount}
          </dd>
        </div>
        <div>
          <dt>下一步</dt>
          <dd>{display.nextAction}</dd>
        </div>
      </dl>

      {hasRequestDetails ? (
        <details className="lab-submit-status__details">
          <summary>查看请求与错误详情</summary>
          <dl>
            {display.runId ? (
              <div>
                <dt>生成记录 ID</dt>
                <dd>
                  <CopyableValue label="生成记录 ID" value={display.runId} />
                </dd>
              </div>
            ) : null}
            {display.statusCode !== null || display.statusText ? (
              <div>
                <dt>HTTP 响应</dt>
                <dd>
                  {display.statusCode ?? "—"} {display.statusText ?? ""}
                </dd>
              </div>
            ) : null}
            {display.detail ? (
              <div>
                <dt>完整原因</dt>
                <dd>
                  <ExpandableText summary="展开完整原因" value={display.detail} />
                </dd>
              </div>
            ) : null}
          </dl>
        </details>
      ) : null}
    </section>
  );
}
