export type CanonicalPageKey =
  | "submission"
  | "strategies"
  | "configuration"
  | "market-data"
  | "research"
  | "optimization";

export const CANONICAL_URL_KEYS = {
  submission: [],
  strategies: ["strategy"],
  configuration: ["scope", "workflow", "profile", "version"],
  "market-data": ["profile", "snapshot", "target"],
  research: ["scope", "workflow", "target", "strategy"],
  optimization: ["strategy", "target"],
} as const satisfies Record<CanonicalPageKey, readonly string[]>;

export type CanonicalUrlState = Readonly<Record<string, string>>;

export type CanonicalUrlParseResult =
  | { valid: true; values: CanonicalUrlState; problems: readonly [] }
  | { valid: false; values: CanonicalUrlState; problems: readonly string[] };

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const CONTROL = /[\u0000-\u001f\u007f]/;
const UUID_KEYS = new Set(["profile", "strategy", "version", "snapshot"]);

function validValue(key: string, value: string): boolean {
  if (!value || value.trim() !== value || value.length > 200 || CONTROL.test(value)) return false;
  return !UUID_KEYS.has(key) || UUID.test(value);
}

export function parseCanonicalUrlState(
  page: CanonicalPageKey,
  source: string | URLSearchParams,
): CanonicalUrlParseResult {
  const params = typeof source === "string" ? new URLSearchParams(source) : source;
  const allowed = CANONICAL_URL_KEYS[page] as readonly string[];
  const values: Record<string, string> = {};
  const problems: string[] = [];
  for (const key of new Set(params.keys())) {
    const all = params.getAll(key);
    if (!allowed.includes(key)) {
      problems.push(`UNKNOWN_URL_KEY:${key}`);
      continue;
    }
    if (all.length !== 1) {
      problems.push(`DUPLICATE_URL_KEY:${key}`);
      continue;
    }
    if (!validValue(key, all[0])) {
      problems.push(`INVALID_URL_VALUE:${key}`);
      continue;
    }
    values[key] = all[0];
  }
  if ((page === "configuration" || page === "research") && Boolean(values.scope) !== Boolean(values.workflow)) {
    problems.push("INCOMPLETE_SCOPE_WORKFLOW");
  }
  return problems.length
    ? { valid: false, values, problems }
    : { valid: true, values, problems: [] };
}

export function serializeCanonicalUrlState(
  page: CanonicalPageKey,
  values: Readonly<Record<string, string | null | undefined>>,
): string {
  const params = new URLSearchParams();
  for (const key of CANONICAL_URL_KEYS[page]) {
    const value = values[key];
    if (value) params.set(key, value);
  }
  const parsed = parseCanonicalUrlState(page, params);
  if (!parsed.valid) throw new Error(`INVALID_URL_STATE: ${parsed.problems.join(",")}`);
  return params.toString();
}

export function withCanonicalUrlValue(
  page: CanonicalPageKey,
  current: CanonicalUrlState,
  key: string,
  value: string | null,
): string {
  if (!(CANONICAL_URL_KEYS[page] as readonly string[]).includes(key)) {
    throw new Error(`UNKNOWN_URL_KEY:${key}`);
  }
  return serializeCanonicalUrlState(page, { ...current, [key]: value });
}

export type CanonicalStatusTone = "success" | "danger" | "warning" | "info" | "neutral";

export type CanonicalStatusPresentation = {
  known: boolean;
  label: string;
  raw: string;
  tone: CanonicalStatusTone;
};

const STATUS: Readonly<Record<string, Omit<CanonicalStatusPresentation, "known" | "raw">>> = {
  ACCEPTED: { label: "已接受", tone: "success" },
  ACTIVE: { label: "已启用", tone: "info" },
  ARCHIVED: { label: "已归档", tone: "neutral" },
  AVAILABLE: { label: "可用", tone: "success" },
  BLOCKED: { label: "已阻塞", tone: "warning" },
  DRAFT: { label: "草稿", tone: "neutral" },
  EMPTY: { label: "空目录", tone: "neutral" },
  FAILED: { label: "失败", tone: "danger" },
  INTAKE_ACCEPTED: { label: "已安全入库", tone: "success" },
  MARKET_SNAPSHOT_UNSET: { label: "行情快照未设置", tone: "warning" },
  NOT_EVALUATED: { label: "尚未评价", tone: "neutral" },
  NOT_STARTED: { label: "尚未开始", tone: "neutral" },
  PENDING: { label: "等待中", tone: "info" },
  PENDING_BASELINE: { label: "等待基线", tone: "info" },
  PENDING_FIRST_BACKTEST: { label: "等待首次回测", tone: "warning" },
  QUALIFIED: { label: "已合格", tone: "success" },
  READY: { label: "就绪", tone: "success" },
  REJECTED: { label: "已拒绝", tone: "danger" },
  RETIRED: { label: "已退役", tone: "neutral" },
  RUNNING: { label: "运行中", tone: "info" },
  SUCCEEDED: { label: "成功", tone: "success" },
  TRADING_DISABLED: { label: "交易已禁用", tone: "warning" },
  UNSET: { label: "未设置", tone: "warning" },
  UNVALIDATED: { label: "尚未验证", tone: "warning" },
  VALIDATED: { label: "已验证", tone: "success" },
  VALIDATING: { label: "验证中", tone: "info" },
};

export function canonicalStatusPresentation(status: string): CanonicalStatusPresentation {
  const known = STATUS[status];
  return known
    ? { ...known, known: true, raw: status }
    : { known: false, label: "未知合同状态", raw: status, tone: "danger" };
}

export function canonicalStatusesKnown(...statuses: readonly string[]): boolean {
  return statuses.every((status) => canonicalStatusPresentation(status).known);
}

export function canonicalErrorText(error: unknown): string {
  return error instanceof Error ? error.message : "UNKNOWN_CANONICAL_ERROR";
}
