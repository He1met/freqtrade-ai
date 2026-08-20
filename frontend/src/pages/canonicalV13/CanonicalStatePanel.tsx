import { useEffect, useState, type DependencyList, type ReactNode } from "react";
import { Link } from "react-router-dom";

import { CanonicalV13ApiError, CanonicalV13ClientContractError } from "../../api/canonicalV13Client";
import { FormalLoadingState, StatusBadge } from "../../components/DisplayPrimitives";
import { canonicalErrorText, canonicalReasonGuidance, canonicalStatusGuidance } from "./canonicalV13Model";

export type CanonicalQueryState<T> = {
  data: T | null;
  error: unknown;
  loading: boolean;
};

export function useCanonicalQuery<T>(
  loader: (signal: AbortSignal) => Promise<T>,
  dependencies: DependencyList,
  enabled = true,
): CanonicalQueryState<T> {
  const [state, setState] = useState<CanonicalQueryState<T>>({ data: null, error: null, loading: enabled });
  useEffect(() => {
    if (!enabled) {
      setState({ data: null, error: null, loading: false });
      return undefined;
    }
    const controller = new AbortController();
    setState({ data: null, error: null, loading: true });
    loader(controller.signal).then(
      (data) => { if (!controller.signal.aborted) setState({ data, error: null, loading: false }); },
      (error: unknown) => { if (!controller.signal.aborted) setState({ data: null, error, loading: false }); },
    );
    return () => controller.abort();
  }, [...dependencies, enabled]);
  return state;
}

export function CanonicalStatus({ status }: { status: string }) {
  const presentation = canonicalStatusGuidance(status);
  return (
    <span
      className="canonical-v13-status-guidance"
      title={`${presentation.explanation} ${presentation.actionLabel}。原始状态码：${presentation.raw}`}
    >
      <StatusBadge
        label={presentation.label}
        status={status}
        tone={presentation.tone}
      />
    </span>
  );
}

export function CanonicalReasonList({
  reasonCodes,
  diagnosticDetails = {},
}: {
  reasonCodes: readonly string[];
  diagnosticDetails?: Readonly<Record<string, string>>;
}) {
  const uniqueCodes = [...new Set(reasonCodes)];
  if (!uniqueCodes.length) return null;
  return (
    <ul aria-label="阻塞原因与解决建议" className="canonical-v13-reason-list">
      {uniqueCodes.map((code) => {
        const item = canonicalReasonGuidance(code);
        return (
          <li data-known-reason={item.known ? "true" : "false"} key={code}>
            <div>
              <strong>{item.label}</strong>
              <p>{item.explanation}</p>
              <Link to={item.actionTo}>{item.actionLabel}</Link>
            </div>
            <details aria-label={`原始诊断：${item.raw}`}>
              <summary>查看原始诊断</summary>
              <code>{item.raw}</code>
              {diagnosticDetails[code] ? <p>{diagnosticDetails[code]}</p> : null}
            </details>
          </li>
        );
      })}
    </ul>
  );
}

export function CanonicalInlineReason({ code }: { code: string }) {
  const item = canonicalReasonGuidance(code);
  return (
    <span className="canonical-v13-inline-reason" data-known-reason={item.known ? "true" : "false"}>
      <span>{item.label}</span>
      <details aria-label={`原始诊断：${item.raw}`}>
        <summary>诊断码</summary>
        <code>{item.raw}</code>
      </details>
    </span>
  );
}

export function CanonicalStatePanel({
  kind,
  title,
  description,
  reasonCodes = [],
  diagnosticDetails = {},
  children,
}: {
  kind: "blocked" | "empty" | "error" | "loading" | "pending" | "unknown";
  title: string;
  description: ReactNode;
  reasonCodes?: readonly string[];
  diagnosticDetails?: Readonly<Record<string, string>>;
  children?: ReactNode;
}) {
  if (kind === "loading") {
    return <FormalLoadingState className="canonical-v13-state" label={title} />;
  }
  return (
    <section className="canonical-v13-state" data-state={kind} role={kind === "error" ? "alert" : "status"}>
      <div>
        <strong>{title}</strong>
        <p>{description}</p>
      </div>
      <CanonicalReasonList diagnosticDetails={diagnosticDetails} reasonCodes={reasonCodes} />
      {children}
    </section>
  );
}

export function canonicalErrorDiagnostic(error: unknown): { code: string; detail: string } {
  if (error instanceof CanonicalV13ApiError) {
    return { code: error.code, detail: error.detail };
  }
  if (error instanceof CanonicalV13ClientContractError) {
    return { code: error.code, detail: canonicalErrorText(error) };
  }
  if (error instanceof Error) {
    const match = /^([A-Z][A-Z0-9_]+):\s*(.*)$/s.exec(error.message);
    if (match) return { code: match[1], detail: match[2] };
  }
  return { code: "CANONICAL_API_UNAVAILABLE", detail: canonicalErrorText(error) };
}

export function CanonicalQueryError({ error, title = "Canonical API 加载失败" }: { error: unknown; title?: string }) {
  const diagnostic = canonicalErrorDiagnostic(error);
  return (
    <CanonicalStatePanel
      description="未取得可验证的 Canonical API 事实；当前区域保持未知。"
      diagnosticDetails={{ [diagnostic.code]: diagnostic.detail }}
      kind="error"
      reasonCodes={[diagnostic.code]}
      title={title}
    />
  );
}
