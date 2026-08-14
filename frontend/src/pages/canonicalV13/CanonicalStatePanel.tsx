import { useEffect, useState, type DependencyList, type ReactNode } from "react";

import { ErrorNotice, FormalLoadingState, StatusBadge } from "../../components/DisplayPrimitives";
import { canonicalErrorText, canonicalStatusPresentation } from "./canonicalV13Model";

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
  const presentation = canonicalStatusPresentation(status);
  return (
    <StatusBadge
      label={presentation.known ? presentation.label : `${presentation.label}：${presentation.raw}`}
      status={status}
      tone={presentation.tone}
    />
  );
}

export function CanonicalStatePanel({
  kind,
  title,
  description,
  reasonCodes = [],
  children,
}: {
  kind: "blocked" | "empty" | "error" | "loading" | "pending" | "unknown";
  title: string;
  description: ReactNode;
  reasonCodes?: readonly string[];
  children?: ReactNode;
}) {
  if (kind === "loading") {
    return <FormalLoadingState className="canonical-v13-state" label={title} />;
  }
  if (kind === "error") {
    return <ErrorNotice className="canonical-v13-state" message={description} title={title} />;
  }
  return (
    <section className="canonical-v13-state" data-state={kind} role="status">
      <div>
        <strong>{title}</strong>
        <p>{description}</p>
      </div>
      {reasonCodes.length ? (
        <ul aria-label="阻塞原因">
          {reasonCodes.map((code) => <li key={code}><code>{code}</code></li>)}
        </ul>
      ) : null}
      {children}
    </section>
  );
}

export function CanonicalQueryError({ error, title = "Canonical API 加载失败" }: { error: unknown; title?: string }) {
  return (
    <CanonicalStatePanel
      description={canonicalErrorText(error)}
      kind="error"
      title={title}
    />
  );
}
