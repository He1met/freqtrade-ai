import { type CSSProperties, type ReactNode, useId, useState } from "react";

import {
  EMPTY_TEXT,
  displayStatus,
  displayStatusWithRaw,
  displayValue,
  statusTone,
  type StatusTone,
} from "../pages/uiCopy";

type OptionalClassName = {
  className?: string;
};

function classNames(...values: Array<string | false | null | undefined>): string {
  return values.filter(Boolean).join(" ");
}

export interface PageHeaderProps extends OptionalClassName {
  title: ReactNode;
  description?: ReactNode;
  eyebrow?: ReactNode;
  status?: ReactNode;
  actions?: ReactNode;
}

export function PageHeader({
  title,
  description,
  eyebrow,
  status,
  actions,
  className,
}: PageHeaderProps) {
  return (
    <header className={classNames("page-header", "display-page-header", className)}>
      <div className="display-page-heading">
        {eyebrow ? <span className="display-page-eyebrow">{eyebrow}</span> : null}
        <h1>{title}</h1>
        {description ? <p>{description}</p> : null}
      </div>
      {status || actions ? (
        <div className="display-page-actions">
          {status}
          {actions}
        </div>
      ) : null}
    </header>
  );
}

export interface StatusBadgeProps extends OptionalClassName {
  status?: string | null;
  label?: string;
  tone?: StatusTone;
  showRaw?: boolean;
}

export function StatusBadge({
  status,
  label,
  tone,
  showRaw = false,
  className,
}: StatusBadgeProps) {
  const visibleLabel = label ?? displayStatus(status);
  const accessibleLabel = showRaw ? displayStatusWithRaw(status) : visibleLabel;
  const rawStatus = status?.trim();

  return (
    <span
      aria-label={`状态：${accessibleLabel}`}
      className={classNames("display-status-badge", `display-status-${tone ?? statusTone(status)}`, className)}
      data-raw-status={rawStatus || undefined}
    >
      <span aria-hidden="true" className="display-status-dot" />
      <span>{visibleLabel}</span>
      {showRaw && rawStatus && visibleLabel !== rawStatus ? (
        <code className="display-status-raw">{rawStatus}</code>
      ) : null}
    </span>
  );
}

function textValue(value: string | number | null | undefined): string {
  return displayValue(value);
}

export interface CompactTextProps extends OptionalClassName {
  value: string | number | null | undefined;
  label?: string;
  maxWidth?: number | string;
  mono?: boolean;
}

export function CompactText({
  value,
  label = "完整内容",
  maxWidth,
  mono = false,
  className,
}: CompactTextProps) {
  const tooltipId = useId();
  const text = textValue(value);
  const style = maxWidth ? ({ "--compact-text-width": typeof maxWidth === "number" ? `${maxWidth}px` : maxWidth } as CSSProperties) : undefined;

  return (
    <span
      aria-describedby={tooltipId}
      aria-label={`${label}：${text}`}
      className={classNames("compact-text", mono && "display-mono", className)}
      style={style}
      tabIndex={0}
    >
      <span className="compact-text-value">{text}</span>
      <span className="compact-text-tooltip" id={tooltipId} role="tooltip">
        {text}
      </span>
    </span>
  );
}

export interface ExpandableTextProps extends OptionalClassName {
  value: string | number | null | undefined;
  summary?: string;
  mono?: boolean;
  emptyText?: string;
}

export function ExpandableText({
  value,
  summary = "查看完整内容",
  mono = false,
  emptyText = EMPTY_TEXT,
  className,
}: ExpandableTextProps) {
  const text = textValue(value);
  if (text === EMPTY_TEXT) {
    return <span className={classNames("display-empty-value", className)}>{emptyText}</span>;
  }

  return (
    <details className={classNames("expandable-text", mono && "display-mono", className)}>
      <summary>{summary}</summary>
      <div className="expandable-text-content">{text}</div>
    </details>
  );
}

export interface CopyableValueProps extends OptionalClassName {
  value: string | number | null | undefined;
  label?: string;
  mono?: boolean;
}

export function CopyableValue({
  value,
  label = "值",
  mono = true,
  className,
}: CopyableValueProps) {
  const text = textValue(value);
  const canCopy = text !== EMPTY_TEXT;
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");

  async function copyValue() {
    try {
      if (!canCopy || !navigator.clipboard) {
        throw new Error("Clipboard API unavailable");
      }
      await navigator.clipboard.writeText(text);
      setCopyState("copied");
    } catch {
      setCopyState("failed");
    }
  }

  const actionLabel =
    copyState === "copied" ? "已复制" : copyState === "failed" ? "复制失败" : `复制${label}`;

  return (
    <span className={classNames("copyable-value", mono && "display-mono", className)}>
      <CompactText label={label} value={text} />
      <button
        aria-label={actionLabel}
        className="copyable-value-button"
        disabled={!canCopy}
        onClick={copyValue}
        type="button"
      >
        {copyState === "copied" ? "已复制" : "复制"}
      </button>
      <span aria-live="polite" className="sr-only">
        {copyState === "copied" ? `${label}已复制到剪贴板` : copyState === "failed" ? `${label}复制失败` : ""}
      </span>
    </span>
  );
}

export interface EmptyStateProps extends OptionalClassName {
  title?: string;
  description?: ReactNode;
  action?: ReactNode;
}

export function EmptyState({
  title = "暂无数据",
  description = "当前没有可显示的记录。",
  action,
  className,
}: EmptyStateProps) {
  return (
    <div className={classNames("empty-state", "display-empty-state", className)} role="status">
      <span aria-hidden="true" className="display-empty-icon">
        —
      </span>
      <div>
        <strong>{title}</strong>
        {description ? <p>{description}</p> : null}
      </div>
      {action ? <div className="display-state-action">{action}</div> : null}
    </div>
  );
}

export interface ErrorNoticeProps extends OptionalClassName {
  title?: string;
  message: ReactNode;
  details?: string | number;
  action?: ReactNode;
}

export function ErrorNotice({
  title = "加载失败",
  message,
  details,
  action,
  className,
}: ErrorNoticeProps) {
  return (
    <aside
      aria-live="assertive"
      className={classNames("notice", "display-error-notice", className)}
      role="alert"
    >
      <span aria-hidden="true" className="display-error-icon">
        !
      </span>
      <div className="display-error-body">
        <strong>{title}</strong>
        <p>{message}</p>
        {details !== null && details !== undefined && details !== "" ? (
          <ExpandableText summary="查看错误详情" value={details} />
        ) : null}
      </div>
      {action ? <div className="display-state-action">{action}</div> : null}
    </aside>
  );
}
