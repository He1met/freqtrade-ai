import type {
  DataSource,
  DryRunArtifactManifest,
  DryRunStatusSnapshot,
  FreqUILinkMetadata,
  RuntimeSafetyBoundary,
} from "../api/types";

const SENSITIVE_NAME = /(api[_-]?key|api[_-]?secret|secret|password|passphrase|token|credential|private[_-]?key)/i;
const FAILED_STATUSES = new Set(["FAILED", "FAILURE", "ERROR", "CRITICAL"]);
const NORMAL_STATUSES = new Set(["SUCCESS", "RUNNING", "READY", "OK"]);

function normalizeStatus(status: string | null | undefined): string {
  return status?.trim().toUpperCase() || "UNKNOWN";
}

export function redactFreqUiText(value: string | null | undefined): string {
  if (!value) {
    return "";
  }
  return value
    .replace(
      /\b(api[_-]?key|api[_-]?secret|secret|password|passphrase|token|credential|private[_-]?key)(\s*[:=]\s*)([^\s,;&]+)/gi,
      "$1$2[REDACTED]",
    )
    .replace(/(https?:\/\/)([^/\s:@]+):([^/\s@]+)@/gi, "$1[REDACTED]@");
}

export function redactFreqUiUrl(value: string | null | undefined): string | null {
  if (!value?.trim()) {
    return null;
  }
  try {
    const parsed = new URL(value);
    if (parsed.username || parsed.password) {
      parsed.username = "[REDACTED]";
      parsed.password = "";
    }
    for (const [name] of parsed.searchParams) {
      parsed.searchParams.set(name, "[REDACTED]");
    }
    return redactFreqUiText(parsed.toString());
  } catch {
    return redactFreqUiText(value);
  }
}

export function redactCommandArgs(commandArgs: string[]): string[] {
  let redactNext = false;
  return commandArgs.map((argument) => {
    if (redactNext) {
      redactNext = false;
      return "[REDACTED]";
    }
    const optionName = argument.split("=", 1)[0];
    if (optionName.startsWith("-") && SENSITIVE_NAME.test(optionName)) {
      if (!argument.includes("=")) {
        redactNext = true;
        return argument;
      }
      return `${optionName}=[REDACTED]`;
    }
    return redactFreqUiText(argument);
  });
}

export type SafeFreqUiLink = {
  enabled: boolean;
  href: string | null;
  displayUrl: string | null;
  status: "READY" | "BLOCKED" | "UNAVAILABLE";
  reason: string;
};

export function safeFreqUiLink(
  metadata: FreqUILinkMetadata,
  safety?: RuntimeSafetyBoundary,
  source?: DataSource,
): SafeFreqUiLink {
  const displayUrl = redactFreqUiUrl(metadata.baseUrl);
  if (!metadata.enabled) {
    return {
      enabled: false,
      href: null,
      displayUrl,
      status: "BLOCKED",
      reason: metadata.blockedReason?.trim() || "Backend 已停用 FreqUI 链接。",
    };
  }
  if (!metadata.baseUrl?.trim()) {
    return {
      enabled: false,
      href: null,
      displayUrl: null,
      status: "UNAVAILABLE",
      reason: "Backend 已启用 FreqUI，但未提供 URL。",
    };
  }
  if (source && source !== "api") {
    return {
      enabled: false,
      href: null,
      displayUrl,
      status: "UNAVAILABLE",
      reason: "FreqUI 元数据不是来自完整 Backend API，已禁止打开。",
    };
  }
  if (metadata.accessMode !== "read-only-link") {
    return {
      enabled: false,
      href: null,
      displayUrl,
      status: "BLOCKED",
      reason: "FreqUI 访问模式不是 read-only-link，已禁止打开。",
    };
  }
  if (safety && safetyBoundarySummary(safety).status !== "READY") {
    return {
      enabled: false,
      href: null,
      displayUrl,
      status: "BLOCKED",
      reason: "Operator Runtime Contract 安全边界异常，已禁止打开 FreqUI。",
    };
  }
  try {
    const parsed = new URL(metadata.baseUrl);
    if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password || parsed.search) {
      return {
        enabled: false,
        href: null,
        displayUrl,
        status: "BLOCKED",
        reason: "FreqUI URL 包含不安全协议、凭据或查询参数，已禁止打开。",
      };
    }
  } catch {
    return {
      enabled: false,
      href: null,
      displayUrl,
      status: "BLOCKED",
      reason: "Backend 提供的 FreqUI URL 无效。",
    };
  }
  return {
    enabled: true,
    href: metadata.baseUrl,
    displayUrl,
    status: "READY",
    reason: "Backend 已启用只读 FreqUI 链接。",
  };
}

export type DryRunPageState =
  | "LOADING"
  | "FAILED"
  | "BLOCKED"
  | "NOT_LOADED"
  | "NON_CORE"
  | "REAL_EMPTY"
  | "NORMAL";

export type DryRunDisplayConclusion = {
  state: DryRunPageState;
  status: string;
  title: string;
  reason: string;
};

function snapshotReason(
  snapshot: DryRunStatusSnapshot,
  manifest: DryRunArtifactManifest | null,
): string | null {
  return (
    snapshot.blockedReason?.trim() ||
    manifest?.blockedReason?.trim() ||
    snapshot.failedReason?.trim() ||
    manifest?.failedReason?.trim() ||
    snapshot.skippedReason?.trim() ||
    manifest?.skippedReason?.trim() ||
    null
  );
}

export function dryRunDisplayConclusion({
  error,
  isLoading,
  manifest,
  snapshot,
  source,
}: {
  error: string | null;
  isLoading: boolean;
  manifest: DryRunArtifactManifest | null;
  snapshot: DryRunStatusSnapshot;
  source: DataSource;
}): DryRunDisplayConclusion {
  const status = normalizeStatus(snapshot.status);
  const reason = snapshotReason(snapshot, manifest);
  if (isLoading) {
    return {
      state: "LOADING",
      status: "LOADING",
      title: "正在加载 Dry-run 快照",
      reason: "数据尚未返回，当前不能判断余额或开放交易。",
    };
  }
  if (source === "failed" || error || FAILED_STATUSES.has(status)) {
    return {
      state: "FAILED",
      status: "FAILED",
      title: "Dry-run 快照加载失败",
      reason: redactFreqUiText(error || reason || "Backend 未返回可用的 Dry-run 快照。"),
    };
  }
  if (status === "BLOCKED") {
    return {
      state: "BLOCKED",
      status,
      title: "Dry-run 当前被阻断",
      reason: redactFreqUiText(reason || "报告未提供阻断原因。"),
    };
  }
  if (!snapshot.lastUpdated || ["UNKNOWN", "UNAVAILABLE", "SKIPPED"].includes(status)) {
    return {
      state: "NOT_LOADED",
      status: "NOT_RUN",
      title: "Dry-run 尚未加载",
      reason: redactFreqUiText(reason || "没有可确认时间的新鲜运行快照。"),
    };
  }
  if (source !== "api") {
    return {
      state: "NON_CORE",
      status: "FIXTURE",
      title: "当前为非真实来源",
      reason: "Fixture 数据不能证明真实余额、交易或 FreqUI 可用性。",
    };
  }
  if (snapshot.dryRun !== true) {
    return {
      state: "BLOCKED",
      status: "BLOCKED",
      title: "Dry-run 标记未确认",
      reason: "快照没有明确 dry_run=true，页面不会将其视为安全运行。",
    };
  }
  if (NORMAL_STATUSES.has(status) && snapshot.openTradesSummary.totalOpenTrades === 0) {
    return {
      state: "REAL_EMPTY",
      status: "SUCCESS",
      title: "当前无开放交易",
      reason: "真实 API 快照已加载且运行正常；开放交易数量确认为 0。",
    };
  }
  if (NORMAL_STATUSES.has(status)) {
    return {
      state: "NORMAL",
      status,
      title: "Dry-run 状态正常",
      reason: `真实 API 快照包含 ${snapshot.openTradesSummary.totalOpenTrades} 笔开放交易。`,
    };
  }
  return {
    state: "NOT_LOADED",
    status,
    title: "Dry-run 状态尚未确认",
    reason: redactFreqUiText(reason || `当前状态 ${status} 不能证明运行正常。`),
  };
}

export function safetyBoundarySummary(safety: RuntimeSafetyBoundary): {
  status: "READY" | "FAILED";
  dryRunLabel: string;
  liveLabel: string;
  realOrdersLabel: string;
  violations: string[];
} {
  const violations: string[] = [];
  if (!safety.readOnly) violations.push("运行契约未确认只读");
  if (safety.allowLiveTrading) violations.push("允许 Live trading");
  if (safety.allowRealOrders) violations.push("允许真实订单");
  if (safety.allowExchangeConnection) violations.push("允许交易所连接");
  if (safety.allowDeployControl) violations.push("允许部署控制");
  if (safety.canStartStopBot) violations.push("允许启动或停止机器人");
  return {
    status: violations.length === 0 ? "READY" : "FAILED",
    dryRunLabel: violations.length === 0 ? "只读 Dry-run" : "安全边界异常",
    liveLabel: safety.allowLiveTrading ? "允许" : "禁止",
    realOrdersLabel: safety.allowRealOrders ? "允许" : "禁止",
    violations,
  };
}
