import type { SubmissionState } from "./EvidencePanels";

type StatusSummary = {
  className: string;
  label: string;
};

type Props = {
  submission: SubmissionState;
  status: StatusSummary;
  message: string;
  rows: Array<[string, string]>;
};

export function SubmissionStatusPanel({ message, rows, status, submission }: Props) {
  return (
    <section className="lab-status-panel" aria-live="polite">
      <div className="lab-status-heading">
        <span className={`run-status ${status.className}`}>{status.label}</span>
        <strong>{message}</strong>
      </div>
      <dl className="compact-detail-list">
        {rows.map(([label, value]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
        {submission.kind === "failed" || submission.kind === "unauthorized" ||
        (submission.kind === "blocked" && submission.statusCode) ? (
          <div>
            <dt>HTTP</dt>
            <dd>
              {submission.statusCode ?? "-"} {submission.statusText ?? ""}
            </dd>
          </div>
        ) : null}
      </dl>
    </section>
  );
}
