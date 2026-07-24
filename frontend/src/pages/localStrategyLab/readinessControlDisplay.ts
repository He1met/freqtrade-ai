import type {
  DryRunArtifactManifest,
  DryRunControlReport,
  DryRunStatusSnapshot,
} from "../../api/types";

export type ControlUiKind = "idle" | "starting" | "stopping" | "complete" | "failed";

const EXACT_RUNTIME_STATUSES = new Set([
  "STARTING",
  "RUNNING",
  "STOPPING",
  "STOPPED",
  "FAILED",
  "BLOCKED",
]);

function normalizedStatus(value: string | null | undefined): string | null {
  const status = value?.trim().toUpperCase();
  return status || null;
}

export function resolvedControlStatus({
  kind,
  report,
  persistedStatus,
}: {
  kind: ControlUiKind;
  report: DryRunControlReport | null;
  persistedStatus: string | null | undefined;
}): string {
  if (kind === "starting") return "STARTING";
  if (kind === "stopping") return "STOPPING";
  if (kind === "failed") return "FAILED";

  const reportStatus = normalizedStatus(report?.status);
  const snapshotStatus = normalizedStatus(report?.statusSnapshot.status);
  const storedStatus = normalizedStatus(persistedStatus);

  if (kind === "complete") {
    if (reportStatus === "FAILED" || reportStatus === "BLOCKED") return reportStatus;
    if (snapshotStatus && EXACT_RUNTIME_STATUSES.has(snapshotStatus)) return snapshotStatus;
    if (reportStatus === "STOPPED") return "STOPPED";
    return reportStatus ?? snapshotStatus ?? "FAILED";
  }

  return storedStatus ?? "BLOCKED";
}

export function readinessReason({
  status,
  summary,
  blockedReason,
  unavailableReason,
  staleReason,
}: {
  status: string;
  summary: string;
  blockedReason: string | null;
  unavailableReason: string | null;
  staleReason: string | null;
}): string {
  if (normalizedStatus(status) === "READY") {
    return summary || "Dry-run readiness 持久证据已通过。";
  }
  return blockedReason
    ?? unavailableReason
    ?? staleReason
    ?? summary
    ?? "Dry-run readiness 未提供可验收结论。";
}

export function readinessNextAction(status: string, reason: string): string {
  if (normalizedStatus(status) === "READY") {
    return "复核候选版本、profile、manifest 和 snapshot；仅在人工批准后启动受控 dry-run。";
  }
  return `先解决阻断并重新检查 readiness：${reason}`;
}

export function dryRunSafetyConclusion(snapshot: DryRunStatusSnapshot): {
  status: "PASS" | "BLOCKED";
  reason: string;
} {
  if (snapshot.dryRun === true) {
    return {
      status: "PASS",
      reason: "snapshot 已证明 dry_run=true；该结论不授予 live trading 或真实订单权限。",
    };
  }
  if (snapshot.dryRun === false) {
    return {
      status: "BLOCKED",
      reason: "snapshot 显示 dry_run=false；禁止启动或继续运行。",
    };
  }
  return {
    status: "BLOCKED",
    reason: "snapshot 未证明 dry_run=true；禁止启动或继续运行。",
  };
}

export function dryRunBlockers(
  manifest: DryRunArtifactManifest | null,
  snapshot: DryRunStatusSnapshot,
): string[] {
  return Array.from(new Set([
    manifest?.blockedReason,
    manifest?.failedReason,
    manifest?.skippedReason,
    snapshot.blockedReason,
    snapshot.failedReason,
    snapshot.skippedReason,
  ].filter((value): value is string => Boolean(value?.trim()))));
}

export function controlNextAction(status: string, blockers: string[]): string {
  switch (normalizedStatus(status)) {
    case "STARTING":
      return "等待启动报告与 status snapshot；不要重复提交启动请求。";
    case "RUNNING":
      return "持续核对 snapshot、dry_run=true 与事件；需要结束时执行停止。";
    case "STOPPING":
      return "等待停止报告，并以最新 status snapshot 确认 STOPPED。";
    case "STOPPED":
      return "核对最终 snapshot 和 artifact；需要再次运行时重新检查 readiness。";
    case "FAILED":
      return blockers[0] ? `修复失败原因后重新检查：${blockers[0]}` : "检查失败报告和服务日志后重新检查 readiness。";
    case "BLOCKED":
    default:
      return blockers[0] ? `先解决阻断：${blockers[0]}` : "先补齐 readiness、人工批准和 dry_run 安全证据。";
  }
}
