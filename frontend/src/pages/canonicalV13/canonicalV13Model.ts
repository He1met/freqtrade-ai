import { CANONICAL_CONFIGURATION_KINDS } from "../../api/canonicalV13Types.ts";
import type {
  ConfigurationCatalogProjection,
  MarketInventoryProjection,
  OptimizationListProjection,
  ReadinessProjection,
  StrategyCatalogProjection,
} from "../../api/canonicalV13Types";

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
  research: ["scope", "workflow", "target", "strategy", "plan"],
  optimization: ["strategy", "target"],
} as const satisfies Record<CanonicalPageKey, readonly string[]>;

export type CanonicalUrlState = Readonly<Record<string, string>>;

export type CanonicalUrlParseResult =
  | { valid: true; values: CanonicalUrlState; problems: readonly [] }
  | { valid: false; values: CanonicalUrlState; problems: readonly string[] };

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const CONTROL = /[\u0000-\u001f\u007f]/;
const UUID_KEYS = new Set(["profile", "strategy", "version", "snapshot", "plan"]);

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

export type CanonicalGuidance = CanonicalStatusPresentation & {
  actionLabel: string;
  actionTo: string;
  explanation: string;
};

type GuidanceDefinition = Omit<CanonicalGuidance, "known" | "raw">;

function guidance(
  label: string,
  explanation: string,
  actionLabel: string,
  actionTo: string,
  tone: CanonicalStatusTone,
): GuidanceDefinition {
  return { actionLabel, actionTo, explanation, label, tone };
}

const STATUS: Readonly<Record<string, GuidanceDefinition>> = {
  ACCEPTED: guidance("已接受", "API 已接受这份 canonical 证据；只代表当前记录状态。", "查看行情证据", "/v13/market-data", "success"),
  ACTIVE: guidance("已启用", "API 将该记录标记为启用；这本身不证明运行或交易已获授权。", "返回工作台核对事实", "/v13", "info"),
  ARCHIVED: guidance("已归档", "该记录已归档，不再作为当前 canonical 入口。", "查看策略目录", "/v13/strategies", "neutral"),
  AVAILABLE: guidance("可用", "API projection 当前可读取；可用不等于研究就绪或可执行。", "返回工作台核对事实", "/v13", "success"),
  BLOCKED: guidance("已阻塞", "API 明确返回阻断；必须按原因码处理，页面不会自行解除。", "查看研究诊断", "/v13/research", "warning"),
  COMPLETE: guidance("已完成", "API 记录的当前流程阶段已完成；不自动代表 qualification 通过。", "查看研究状态", "/v13/research", "success"),
  DECLARED: guidance("已声明", "验证计划已持久化声明，但尚未证明可运行。", "查看研究状态", "/v13/research", "info"),
  DRAFT: guidance("草稿", "记录仍是草稿，尚未形成已验证的冻结事实。", "查看配置或策略", "/v13/configuration", "neutral"),
  EMPTY: guidance("暂无记录", "API 明确返回空 projection；这不是加载成功后的隐含资格。", "提交或查看策略", "/v13/submission", "neutral"),
  FAILED: guidance("失败", "API 记录了失败终态；页面不会把失败结果提升为可用。", "查看研究诊断", "/v13/research", "danger"),
  INTAKE_ACCEPTED: guidance("已安全入库", "受控 intake 已接受；尚未验证、合格或获准执行。", "查看策略状态", "/v13/strategies", "success"),
  MARKET_SNAPSHOT_UNSET: guidance("行情快照未设置", "Canonical API 尚未提供冻结行情快照。", "查看行情证据", "/v13/market-data", "warning"),
  NOT_EVALUATED: guidance("尚未评价", "API 尚无 qualification 决策，不能推断为通过或拒绝。", "查看研究状态", "/v13/research", "neutral"),
  NOT_STARTED: guidance("尚未开始", "API 尚未记录该流程开始。", "查看研究状态", "/v13/research", "neutral"),
  PENDING: guidance("等待中", "API 记录仍在等待，不是完成或成功。", "查看研究状态", "/v13/research", "info"),
  PENDING_BASELINE: guidance("等待基线", "优化流程正在等待已持久化的基线 qualification。", "查看优化状态", "/v13/optimization", "info"),
  PENDING_FIRST_BACKTEST: guidance("等待首次回测", "API 尚无获授权且已持久化的首次回测事实。", "查看研究状态", "/v13/research", "warning"),
  PASSED: guidance("已通过", "API 记录该 gate 已通过；仅适用于该 gate 与冻结 lineage。", "查看研究证据", "/v13/research", "success"),
  QUALIFIED: guidance("已合格", "API 存在 qualification 通过决策；仍不等于 runtime 或交易获授权。", "查看研究证据", "/v13/research", "success"),
  READY: guidance("就绪", "当前 API projection 明确为就绪；只适用于该 projection。", "查看研究证据", "/v13/research", "success"),
  REJECTED: guidance("已拒绝", "API 已记录拒绝终态，不能作为合格或可执行事实。", "查看研究原因", "/v13/research", "danger"),
  RETIRED: guidance("已退役", "该版本已退役，不再是当前配置事实。", "查看配置版本", "/v13/configuration", "neutral"),
  RUNNING: guidance("进行中", "API 记录流程正在进行；页面不会据此推断最终结果。", "查看研究状态", "/v13/research", "info"),
  SUCCEEDED: guidance("已成功", "API 记录本次 attempt 成功；qualification 仍由独立决策提供。", "查看研究证据", "/v13/research", "success"),
  TRADING_DISABLED: guidance("交易已禁用", "API 明确声明交易能力处于禁用状态。", "查看 Runtime 诊断", "/v13/research", "warning"),
  UNSET: guidance("未设置", "API 明确表示必需事实尚未设置；页面不会填充默认值。", "查看配置缺口", "/v13/configuration", "warning"),
  UNVALIDATED: guidance("尚未验证", "版本已入库但尚无 canonical 验证事实。", "查看策略状态", "/v13/strategies", "warning"),
  VALIDATED: guidance("已验证", "API 记录该版本已验证；qualification 仍是独立事实。", "查看策略状态", "/v13/strategies", "success"),
  VALIDATING: guidance("验证中", "API 记录验证仍在进行，尚无终态。", "查看策略状态", "/v13/strategies", "info"),
};

export const CANONICAL_ENUMERATED_STATUS_CODES = Object.freeze(Object.keys(STATUS));

export function canonicalStatusPresentation(status: string): CanonicalStatusPresentation {
  const known = STATUS[status];
  return known
    ? { known: true, label: known.label, raw: status, tone: known.tone }
    : { known: false, label: "未知合同状态", raw: status, tone: "danger" };
}

export function canonicalStatusGuidance(status: string): CanonicalGuidance {
  const known = STATUS[status];
  return known
    ? { ...known, known: true, raw: status }
    : {
      actionLabel: "返回工作台核对 API",
      actionTo: "/v13",
      explanation: "API 返回了前端尚未识别的状态；页面保持阻断，不推断其业务含义。",
      known: false,
      label: "未知合同状态",
      raw: status,
      tone: "danger",
    };
}

const API_FAILURE = guidance("Canonical API 不可用", "未取得可验证的 canonical projection；页面保持未知并禁止推断。", "返回工作台核对服务", "/v13", "danger");
const CONTRACT_FAILURE = guidance("接口合同无法识别", "返回值不符合已发布的 Canonical API 合同；页面保持阻断。", "返回工作台核对 API", "/v13", "danger");
const CONFIGURATION_BLOCKER = guidance("研究配置尚未满足", "Canonical 配置或冻结 snapshot 缺失、不一致或尚未验证。", "查看配置缺口", "/v13/configuration", "warning");
const CONFIGURATION_DRIFT = guidance("研究配置证据不一致", "冻结配置的 identity、scope、workflow 或 digest 与当前 lineage 不一致。", "查看配置诊断", "/v13/configuration", "danger");
const MARKET_BLOCKER = guidance("行情证据尚未满足", "Canonical 行情 snapshot、receipt、覆盖范围或窗口条件尚未满足。", "查看行情证据", "/v13/market-data", "warning");
const MARKET_DRIFT = guidance("行情证据不一致", "Canonical 行情 lineage、digest 或 receipt 校验不一致；不能用于研究。", "查看行情诊断", "/v13/market-data", "danger");
const RESEARCH_BLOCKER = guidance("研究流程尚未就绪", "Canonical 研究 bundle、activation 或 lineage 尚未满足。", "查看研究诊断", "/v13/research", "warning");
const RESEARCH_DRIFT = guidance("研究证据不一致", "Canonical 研究 bundle 或 activation 的冻结证据发生不一致。", "查看研究诊断", "/v13/research", "danger");
const RUNTIME_BLOCKER = guidance("Runtime 尚未就绪", "Canonical API 明确返回 Runtime 前置条件或健康证据不足。", "查看 Runtime 诊断", "/v13/research", "warning");
const RUNTIME_DRIFT = guidance("Runtime 证据不一致", "部署、approval、qualification、capability 或 runtime receipt 的 lineage/digest 不一致。", "查看 Runtime 诊断", "/v13/research", "danger");
const GATE_BLOCKER = guidance("研究 Gate 未通过", "持久化 gate receipt 明确记录阻断或失败；不能推断 validation eligibility。", "查看 Gate 诊断", "/v13/research", "warning");
const SUBMISSION_BLOCKER = guidance("策略提交被阻断", "Canonical intake 拒绝了当前 source envelope 或安全校验未通过；未写入成功事实。", "检查提交内容", "/v13/submission", "danger");
const CONFIGURATION_API_BLOCKER = guidance("配置操作被阻断", "Canonical 配置写入、验证、依赖或 receipt 合同未满足。", "查看配置诊断", "/v13/configuration", "danger");
const RESEARCH_API_BLOCKER = guidance("研究操作被阻断", "Canonical bundle、authorization、lineage 或 research capability 合同未满足。", "查看研究诊断", "/v13/research", "danger");

function enumeratedGuidance(
  codes: readonly string[],
  definition: GuidanceDefinition,
): Readonly<Record<string, GuidanceDefinition>> {
  return Object.fromEntries(codes.map((code) => [code, definition]));
}

const SUBMISSION_ERROR_REASONS = enumeratedGuidance([
  "BLOCKED_AMBIGUOUS_LATEST_SOURCE",
  "BLOCKED_ARTIFACT_DIGEST_COLLISION",
  "BLOCKED_CURRENT_VERSION_OWNERSHIP",
  "BLOCKED_IDEMPOTENCY_KEY_REUSE",
  "BLOCKED_INTAKE_RECEIPT_DRIFT",
  "BLOCKED_INVALID_SOURCE_ENVELOPE",
  "BLOCKED_PATH_TRAVERSAL",
  "BLOCKED_SOURCE_ENTRY_DRIFT",
  "REJECTED_ARTIFACT_TOO_LARGE",
  "REJECTED_CONTROL_CHARACTER",
  "REJECTED_DANGEROUS_CALL",
  "REJECTED_DYNAMIC_STRATEGY_SHAPE",
  "REJECTED_EMPTY_ARTIFACT",
  "REJECTED_IMPORT_NOT_ALLOWED",
  "REJECTED_INVALID_PYTHON_AST",
  "REJECTED_INVALID_UTF8",
  "REJECTED_MODULE_LEVEL_EXECUTION",
  "REJECTED_SECRET_SHAPED_CONTENT",
  "REJECTED_STRATEGY_BASE",
  "REJECTED_STRATEGY_CLASS_MISMATCH",
  "REJECTED_STRATEGY_CLASS_SHAPE",
], SUBMISSION_BLOCKER);

const CONFIGURATION_ERROR_REASONS = enumeratedGuidance([
  "BLOCKED_ACTIVATION_AUTHORITY_UNSET",
  "BLOCKED_ADAPTER_MANIFEST_DRIFT",
  "BLOCKED_AGGREGATE_DEPENDENCIES",
  "BLOCKED_ALLOCATION_OR_CAP_UNSET",
  "BLOCKED_CONFIGURATION_AUDIT_DRIFT",
  "BLOCKED_CONFIGURATION_DIGEST_DRIFT",
  "BLOCKED_CONFIGURATION_KIND",
  "BLOCKED_CONFIGURATION_KIND_MISMATCH",
  "BLOCKED_CONFIGURATION_RECEIPT_DRIFT",
  "BLOCKED_CONFIGURATION_TRANSITION",
  "BLOCKED_CONFIGURATION_VALUE_UNSET",
  "BLOCKED_CONFIGURATION_VERSION_NOT_FOUND",
  "BLOCKED_DEPENDENCY_CYCLE",
  "BLOCKED_DEPENDENCY_NOT_FROZEN",
  "BLOCKED_DEPENDENCY_TYPE_MISMATCH",
  "BLOCKED_DERIVED_TOTAL_PERSISTENCE",
  "BLOCKED_DUPLICATE_DEPENDENCY",
  "BLOCKED_DUPLICATE_TARGET",
  "BLOCKED_GENERATION_TARGET_DEPENDENCY",
  "BLOCKED_INVALID_CONFIGURATION_AUTHORITY",
  "BLOCKED_INVALID_CONFIGURATION_ENVELOPE",
  "BLOCKED_INVALID_CONFIGURATION_PAYLOAD",
  "BLOCKED_INVALID_DIVERSITY_RULE",
  "BLOCKED_INVALID_QUALIFICATION_GATE",
  "BLOCKED_INVALID_QUALIFICATION_THRESHOLD",
  "BLOCKED_INVALID_SCORING_COMPONENT",
  "BLOCKED_INVALID_WINDOW_MEMBER",
  "BLOCKED_NON_CANONICAL_JSON",
  "BLOCKED_PROFILE_IDENTITY_DRIFT",
  "BLOCKED_SCORING_AGGREGATION_UNSET",
  "BLOCKED_SCORING_WEIGHT_TOTAL",
  "BLOCKED_SNAPSHOT_DIGEST_DRIFT",
  "BLOCKED_SNAPSHOT_LIFECYCLE_DRIFT",
  "BLOCKED_SNAPSHOT_MEMBER_DRIFT",
  "BLOCKED_TARGET_ALLOCATION_MISMATCH",
], CONFIGURATION_API_BLOCKER);

const RESEARCH_ERROR_REASONS = enumeratedGuidance([
  "BLOCKED_AUTHORIZATION_ENVIRONMENT",
  "BLOCKED_AUTHORIZATION_PLAN_NOT_READY",
  "BLOCKED_BUNDLE_ID_DRIFT",
  "BLOCKED_BUNDLE_SCOPE_UNSET",
  "BLOCKED_EVALUATION_CAPABILITY_OVERLAP",
  "BLOCKED_EXECUTION_AUTHORIZATION_LINEAGE",
  "BLOCKED_PREVIEW_DIGEST_DRIFT",
  "BLOCKED_PRODUCTION_EXECUTOR_CAPABILITY",
  "BLOCKED_RESEARCH_BATCH_INPUT",
  "BLOCKED_RESEARCH_BUNDLE_NOT_READY",
  "BLOCKED_RESEARCH_CANDIDATE_TARGET_AMBIGUOUS",
  "BLOCKED_RESEARCH_CAPABILITY_UNPROVISIONED",
  "BLOCKED_RESEARCH_CONNECTION_FACTORY",
  "BLOCKED_RESEARCH_TARGET_SET_MISMATCH",
  "BLOCKED_RESEARCH_TRANSACTION_OWNERSHIP",
  "BLOCKED_VALIDATION_PLAN_NOT_FOUND",
], RESEARCH_API_BLOCKER);

const GATE_ERROR_REASONS = enumeratedGuidance([
  "BLOCKED_GATE_ARTIFACT_UNAVAILABLE",
  "BLOCKED_GATE_ATTEMPT_NOT_CLAIMABLE",
  "BLOCKED_GATE_ATTEMPT_NOT_FOUND",
  "BLOCKED_GATE_BUNDLE_MEMBERS",
  "BLOCKED_GATE_BUNDLE_NOT_ACCEPTED",
  "BLOCKED_GATE_DIGEST",
  "BLOCKED_GATE_IDEMPOTENCY_CONFLICT",
  "BLOCKED_GATE_IDEMPOTENCY_KEY",
  "BLOCKED_GATE_LEASE_INVALID",
  "BLOCKED_GATE_MARKET_LINEAGE",
  "BLOCKED_GATE_MIXED_LINEAGE",
  "BLOCKED_GATE_RECEIPT_DIGEST_DRIFT",
  "BLOCKED_GATE_RECEIPT_UNAVAILABLE",
  "BLOCKED_GATE_RELEASE_COMMIT",
  "BLOCKED_GATE_STATIC_PREREQUISITE",
  "BLOCKED_GATE_TARGET_LINEAGE",
  "BLOCKED_GATE_TIMESTAMP",
], GATE_BLOCKER);

const MARKET_ERROR_REASONS = enumeratedGuidance([
  "BLOCKED_MARKET_ACQUISITION_NOT_CONFIGURED",
  "BLOCKED_MARKET_ACQUISITION_RECEIPT",
  "BLOCKED_MARKET_ACQUISITION_RECEIPT_DRIFT",
  "BLOCKED_MARKET_ARTIFACT_ROOT",
  "BLOCKED_MARKET_FRESHNESS_CONTRACT",
  "BLOCKED_MARKET_FRESHNESS_EXPIRED",
  "BLOCKED_MARKET_PLAN_DIGEST_DRIFT",
  "BLOCKED_MARKET_RECEIPT_DIGEST_DRIFT",
  "BLOCKED_MARKET_RECEIPT_NOT_ACCEPTED",
], MARKET_BLOCKER);

const CONFIGURATION_REASON_SUFFIXES = [
  "UNSET",
  "SNAPSHOT_UNSET",
  "SNAPSHOT_INVALID",
  "SNAPSHOT_DIGEST_DRIFT",
  "SCOPE_MISMATCH",
  "WORKFLOW_MISMATCH",
] as const;

const CONFIGURATION_KIND_REASONS: Readonly<Record<string, GuidanceDefinition>> = Object.fromEntries(
  CANONICAL_CONFIGURATION_KINDS.flatMap((kind) => CONFIGURATION_REASON_SUFFIXES.map((suffix) => [
    `${kind}_${suffix}`,
    suffix === "UNSET" || suffix === "SNAPSHOT_UNSET" || suffix === "SNAPSHOT_INVALID"
      ? CONFIGURATION_BLOCKER
      : CONFIGURATION_DRIFT,
  ])),
);

const REASON: Readonly<Record<string, GuidanceDefinition>> = {
  ...CONFIGURATION_KIND_REASONS,
  ...SUBMISSION_ERROR_REASONS,
  ...CONFIGURATION_ERROR_REASONS,
  ...RESEARCH_ERROR_REASONS,
  ...GATE_ERROR_REASONS,
  ...MARKET_ERROR_REASONS,
  ACTIVE_APPROVAL_REQUIRED: RUNTIME_BLOCKER,
  ACTIVE_BUNDLE_DIGEST_DRIFT: RESEARCH_DRIFT,
  ACTIVE_BUNDLE_LINEAGE_DRIFT: RESEARCH_DRIFT,
  ACTIVE_BUNDLE_MEMBER_SET_INVALID: RESEARCH_DRIFT,
  ACTIVE_DEPLOYMENT_AMBIGUOUS: RUNTIME_BLOCKER,
  ACTIVE_DEPLOYMENT_UNSET: guidance("尚无启用中的部署", "API 未找到唯一 ACTIVE deployment；这不会被解释为 Runtime 正常。", "查看 Runtime 诊断", "/v13/research", "warning"),
  AGGREGATE_SNAPSHOT_BINDING_MISMATCH: CONFIGURATION_DRIFT,
  ALL_REQUIRED_WINDOWS_AND_SCORE_PASSED: guidance("必需窗口与总分均已通过", "Qualification receipt 明确记录所有必需窗口和总分门槛通过；只适用于该冻结 lineage。", "查看研究证据", "/v13/research", "success"),
  APPROVAL_RECEIPT_DIGEST_DRIFT: RUNTIME_DRIFT,
  BLOCKED_CANONICAL_API_FAILURE: API_FAILURE,
  BLOCKED_CANONICAL_API_RESPONSE: API_FAILURE,
  BLOCKED_CANONICAL_CONNECTION_FACTORY: API_FAILURE,
  BLOCKED_CANONICAL_CONCURRENT_CONFLICT: guidance("Canonical 状态发生并发冲突", "写入期间状态已变化；必须重新读取后再决定是否重试。", "返回工作台重新读取", "/v13", "warning"),
  BLOCKED_CANONICAL_TRANSACTION_OWNERSHIP: API_FAILURE,
  BLOCKED_INVALID_COMMAND_DTO: guidance("提交内容不符合接口合同", "请求字段或格式未通过 Canonical API DTO 校验。", "检查提交内容", "/v13/submission", "danger"),
  BLOCKED_MARKET_SNAPSHOT_LINEAGE_MISSING: MARKET_DRIFT,
  BLOCKED_MARKET_SNAPSHOT_NOT_FOUND: guidance("所选行情快照不存在", "Canonical API 未找到该 snapshot identity；页面不会改用其他快照。", "重新选择行情快照", "/v13/market-data", "warning"),
  BLOCKED_STRATEGY_ARTIFACT_MISSING: guidance("策略 Artifact 缺失", "当前策略版本没有可验证的 canonical artifact。", "查看策略诊断", "/v13/strategies", "danger"),
  BLOCKED_STRATEGY_NOT_FOUND: guidance("所选策略不存在", "Canonical API 未找到该策略 identity；页面不会自动改选。", "重新选择策略", "/v13/strategies", "warning"),
  BLOCKED_STRATEGY_VERSION_MISSING: guidance("策略版本缺失", "当前 canonical 策略没有可读取的版本事实。", "查看策略诊断", "/v13/strategies", "danger"),
  BLOCKED_WRONG_CANONICAL_DATABASE: guidance("Canonical 数据库身份不匹配", "服务连接的数据库未通过 V1.3 canonical genesis 身份校验。", "返回工作台核对服务", "/v13", "danger"),
  CANONICAL_API_UNAVAILABLE: API_FAILURE,
  DEMO_ONLY_INVARIANT_FAILED: RUNTIME_DRIFT,
  DEPLOYMENT_CAPABILITY_DIGEST_DRIFT: RUNTIME_DRIFT,
  DEPLOYMENT_CAPABILITY_LINEAGE_INCOMPLETE: RUNTIME_DRIFT,
  DEPLOYMENT_QUALIFICATION_LINEAGE_DRIFT: RUNTIME_DRIFT,
  DUPLICATE_URL_KEY: CONTRACT_FAILURE,
  GATE_LEASE_EXPIRED: GATE_BLOCKER,
  HEALTHY_RUNTIME_AMBIGUOUS: RUNTIME_BLOCKER,
  INCOMPLETE_SCOPE_WORKFLOW: guidance("Scope 与 Workflow 选择不完整", "Scope 与 Workflow 必须成对来自同一 committed URL selection。", "重新选择研究范围", "/v13/configuration", "warning"),
  INVALID_LIMIT: CONTRACT_FAILURE,
  INVALID_PATH_IDENTITY: CONTRACT_FAILURE,
  INVALID_SUCCESS_DTO: CONTRACT_FAILURE,
  INVALID_URL_STATE: CONTRACT_FAILURE,
  INVALID_VERSION_NUMBER: guidance("版本号无效", "版本号必须是正整数；请求尚未提交。", "检查提交内容", "/v13/submission", "warning"),
  INVALID_URL_VALUE: CONTRACT_FAILURE,
  LOOKAHEAD_BIAS_DETECTED: GATE_BLOCKER,
  LOOKAHEAD_EVIDENCE_BLOCKED: GATE_BLOCKER,
  LOOKAHEAD_EXPORT_MISSING: GATE_BLOCKER,
  LOOKAHEAD_INSUFFICIENT_TRADES: GATE_BLOCKER,
  LOOKAHEAD_LOG_LIMIT_EXCEEDED: GATE_BLOCKER,
  LOOKAHEAD_OBSERVATIONS_UNSET: GATE_BLOCKER,
  LOOKAHEAD_PROCESS_FAILED: GATE_BLOCKER,
  LOOKAHEAD_RESULT_AMBIGUOUS: GATE_BLOCKER,
  LOOKAHEAD_WORKER_BLOCKED: GATE_BLOCKER,
  LOOKAHEAD_WORKER_INTERNAL_ERROR: GATE_BLOCKER,
  MARKET_EVIDENCE_DIGEST_DRIFT: MARKET_DRIFT,
  MARKET_INSPECTION_COVERAGE_MISMATCH: MARKET_BLOCKER,
  MARKET_MEMBER_DIGEST_DRIFT: MARKET_DRIFT,
  MARKET_RECEIPT_NOT_ACCEPTED: MARKET_BLOCKER,
  MARKET_SNAPSHOT_DIGEST_DRIFT: MARKET_DRIFT,
  MARKET_SNAPSHOT_EMPTY: MARKET_BLOCKER,
  MARKET_SNAPSHOT_INVALID: MARKET_BLOCKER,
  MARKET_SNAPSHOT_UNSET: guidance("行情快照尚未设置", "API 没有返回可用于当前研究 lineage 的 canonical market snapshot。", "查看行情证据", "/v13/market-data", "warning"),
  MARKET_TARGET_COVERAGE_MISMATCH: MARKET_BLOCKER,
  OVERALL_SCORE_BELOW_MINIMUM: guidance("总分低于最低门槛", "Qualification receipt 明确记录总分未达到冻结配置中的最低门槛。", "查看评分证据", "/v13/research", "warning"),
  PENDING_FIRST_BACKTEST: guidance("等待首次回测事实", "尚无获授权且已持久化的首次真实回测事实。", "查看研究状态", "/v13/research", "warning"),
  PER_TARGET_ALLOCATION_UNSET: CONFIGURATION_BLOCKER,
  PER_TARGET_CAP_UNSET: CONFIGURATION_BLOCKER,
  QUALIFICATION_RECEIPT_DIGEST_DRIFT: RUNTIME_DRIFT,
  QUALIFIED_BASELINE_ACCEPTED: guidance("合格基线已接受", "Optimization baseline receipt 明确接受了该 qualification 决策；不自动授权 Runtime。", "查看优化状态", "/v13/optimization", "success"),
  QUALIFIED_DECISION_REQUIRED: RUNTIME_BLOCKER,
  REAL_FUNDS_INVARIANT_FAILED: RUNTIME_DRIFT,
  REQUIRED_WINDOWS_UNSET: MARKET_BLOCKER,
  REQUIRED_WINDOW_CANDLE_COUNT_LOW: MARKET_BLOCKER,
  REQUIRED_WINDOW_CONTRACT_INVALID: MARKET_BLOCKER,
  REQUIRED_WINDOW_COVERAGE_MISSING: MARKET_BLOCKER,
  REQUIRED_WINDOW_GATE_FAILED: guidance("必需窗口 Gate 未通过", "Qualification receipt 明确记录至少一个必需窗口未通过。", "查看窗口与 Gate 证据", "/v13/research", "warning"),
  REQUIRED_WINDOW_TIMEZONE_UNSET: MARKET_BLOCKER,
  RESEARCH_ACTIVATION_AMBIGUOUS: RESEARCH_BLOCKER,
  RESEARCH_BUNDLE_UNSET: guidance("研究 Bundle 尚未激活", "API 未找到当前 scope/workflow 的 canonical configuration activation。", "查看配置与研究", "/v13/configuration", "warning"),
  RESEARCH_SCOPE_INCOMPLETE: guidance("研究范围选择不完整", "Scope 与 Workflow 必须成对提供；API 不会使用隐藏默认值。", "重新选择研究范围", "/v13/configuration", "warning"),
  RUNTIME_HEALTH_RECEIPT_MISSING: RUNTIME_BLOCKER,
  RUNTIME_HEARTBEAT_IN_FUTURE: RUNTIME_DRIFT,
  RUNTIME_HEARTBEAT_STALE: RUNTIME_BLOCKER,
  RUNTIME_HEARTBEAT_UNSET: RUNTIME_BLOCKER,
  RUNTIME_LAUNCH_CAPABILITY_DRIFT: RUNTIME_DRIFT,
  RUNTIME_LAUNCH_SPEC_DIGEST_DRIFT: RUNTIME_DRIFT,
  RUNTIME_NOT_HEALTHY: RUNTIME_BLOCKER,
  RUNTIME_ORDER_WRITER_FORBIDDEN: RUNTIME_DRIFT,
  RUNTIME_RECEIPT_CAPABILITY_DRIFT: RUNTIME_DRIFT,
  RUNTIME_RECEIPT_DIGEST_DRIFT: RUNTIME_DRIFT,
  SELECTED_PROFILE_NOT_FOUND: CONFIGURATION_BLOCKER,
  SELECTED_TARGET_NOT_FOUND: MARKET_BLOCKER,
  SELECTED_VERSION_NOT_FOUND: CONFIGURATION_BLOCKER,
  STATIC_FINDINGS_PRESENT: GATE_BLOCKER,
  TARGET_ALLOCATION_CARDINALITY_MISMATCH: CONFIGURATION_BLOCKER,
  TARGET_SET_UNSET: CONFIGURATION_BLOCKER,
  TRADING_DISABLED: guidance("交易能力已明确禁用", "Canonical API 明确返回 TRADING_DISABLED；页面不会显示可交易或运行成功。", "查看 Runtime 诊断", "/v13/research", "warning"),
  UNKNOWN_CONTRACT_VALUE: CONTRACT_FAILURE,
  UNKNOWN_SNAPSHOT_KIND: CONFIGURATION_BLOCKER,
  UNKNOWN_SUCCESS_DTO: CONTRACT_FAILURE,
  UNKNOWN_URL_KEY: CONTRACT_FAILURE,
  WINDOW_SET_UNSET: CONFIGURATION_BLOCKER,
};

export const CANONICAL_ENUMERATED_REASON_CODES = Object.freeze(Object.keys(REASON));

function normalizedReasonKey(code: string): string {
  const separator = code.indexOf(":");
  return separator === -1 ? code : code.slice(0, separator);
}

export function canonicalReasonGuidance(code: string): CanonicalGuidance {
  const normalized = normalizedReasonKey(code);
  const known = REASON[code] ?? REASON[normalized];
  if (known) return { ...known, known: true, raw: code };
  return {
    actionLabel: "返回工作台核对 API",
    actionTo: "/v13",
    explanation: "API 返回了尚未枚举的原因码；页面保持阻断，不补写任何正向业务结论。",
    known: false,
    label: "未知原因码",
    raw: code,
    tone: "danger",
  };
}

export function canonicalStatusesKnown(...statuses: readonly string[]): boolean {
  return statuses.every((status) => canonicalStatusPresentation(status).known);
}

export type CanonicalHomeEvidence = {
  configurations: ConfigurationCatalogProjection | null;
  market: MarketInventoryProjection | null;
  optimization: OptimizationListProjection | null;
  research: ReadinessProjection | null;
  runtime: ReadinessProjection | null;
  strategies: StrategyCatalogProjection | null;
};

export type CanonicalHomeDecision = {
  kind: "blocked" | "pending" | "ready" | "unknown";
  title: string;
  summary: string;
  rawStatus: string;
  reasonCodes: readonly string[];
  nextAction: { label: string; to: string };
};

const UNKNOWN_HOME_DECISION: CanonicalHomeDecision = {
  kind: "unknown",
  title: "项目状态未知",
  summary: "至少一个必需的 Canonical API projection 不可用或包含未知合同状态；页面不会从其他来源补齐。",
  rawStatus: "CANONICAL_API_UNAVAILABLE",
  reasonCodes: [],
  nextAction: { label: "查看研究与运行", to: "/v13/research" },
};

export function canonicalHomeDecision(evidence: CanonicalHomeEvidence): CanonicalHomeDecision {
  const { configurations, market, optimization, research, runtime, strategies } = evidence;
  if (!configurations || !market || !optimization || !research || !runtime || !strategies) {
    return UNKNOWN_HOME_DECISION;
  }
  const projectionStatuses = [
    strategies.status,
    configurations.status,
    market.status,
    research.status,
    runtime.status,
    optimization.status,
    ...strategies.items.flatMap((item) => [item.catalog_status, item.intake_status, item.validation_status, item.qualification_status]),
    ...configurations.items.flatMap((profile) => profile.versions.map((version) => version.lifecycle_status)),
    ...optimization.items.map((item) => item.status),
  ];
  if (!canonicalStatusesKnown(...projectionStatuses)) return UNKNOWN_HOME_DECISION;

  if (strategies.status === "EMPTY") {
    return {
      kind: "blocked",
      title: "尚无 canonical 策略",
      summary: "策略目录由 API 明确返回 EMPTY；这不代表加载失败，也不会从 Legacy 补齐。",
      rawStatus: "EMPTY",
      reasonCodes: [],
      nextAction: { label: "提交第一个策略", to: "/v13/submission" },
    };
  }
  if (configurations.status === "UNSET" || configurations.unset_kinds.length > 0) {
    return {
      kind: "blocked",
      title: "研究配置尚未完整",
      summary: "配置目录明确存在未设置项；页面不会生成隐藏默认值。",
      rawStatus: "UNSET",
      reasonCodes: configurations.unset_kinds.map((kind) => `${kind}_UNSET`),
      nextAction: { label: "查看配置缺口", to: "/v13/configuration" },
    };
  }
  if (market.status === "MARKET_SNAPSHOT_UNSET") {
    return {
      kind: "blocked",
      title: "行情证据尚未设置",
      summary: "Market inventory 由 API 明确返回 MARKET_SNAPSHOT_UNSET；历史 receipt 不会作为 fallback。",
      rawStatus: market.status,
      reasonCodes: [market.status],
      nextAction: { label: "查看行情证据", to: "/v13/market-data" },
    };
  }
  if (research.status === "BLOCKED") {
    return {
      kind: "blocked",
      title: "研究流程被阻断",
      summary: "研究 readiness 由 Canonical API 明确返回 BLOCKED。",
      rawStatus: research.status,
      reasonCodes: research.reason_codes,
      nextAction: { label: "查看研究阻断", to: "/v13/research" },
    };
  }
  if (research.status === "PENDING_FIRST_BACKTEST") {
    return {
      kind: "pending",
      title: "等待首次回测事实",
      summary: "研究 bundle 已冻结，但 API 尚未提供获授权的首次回测事实。",
      rawStatus: research.status,
      reasonCodes: research.reason_codes,
      nextAction: { label: "查看研究状态", to: "/v13/research" },
    };
  }
  return {
    kind: "ready",
    title: "研究准备已就绪",
    summary: "Research readiness 由 Canonical API 明确返回 READY；runtime 与 qualification 仍按各自 projection 独立展示。",
    rawStatus: research.status,
    reasonCodes: research.reason_codes,
    nextAction: { label: "进入策略目录", to: "/v13/strategies" },
  };
}

export function canonicalErrorText(error: unknown): string {
  return error instanceof Error ? error.message : "UNKNOWN_CANONICAL_ERROR";
}
