import type {
  CanonicalErrorResponse,
  ConfigurationCatalogProjection,
  ConfigurationDraftCommand,
  ConfigurationDraftResult,
  ConfigurationValidateCommand,
  ConfigurationValidationResult,
  MarketInventoryProjection,
  MarketSnapshotProjection,
  OptimizationListProjection,
  ReadinessProjection,
  ResearchBundleActivateCommand,
  ResearchBundleActivation,
  ResearchBundlePreview,
  ResearchBundlePreviewCommand,
  StrategyCatalogProjection,
  StrategyProjection,
  SubmissionCommand,
  SubmissionReceipt,
} from "./canonicalV13Types";

export const CANONICAL_V13_API_ROOT = "/api/canonical-v13";

export class CanonicalV13ApiError extends Error {
  readonly code: string;
  readonly detail: string;
  readonly status: number;

  constructor(status: number, code: string, detail: string) {
    super(`${code}: ${detail}`);
    this.name = "CanonicalV13ApiError";
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

export class CanonicalV13ClientContractError extends Error {
  readonly code: string;

  constructor(code: string, detail: string) {
    super(`${code}: ${detail}`);
    this.name = "CanonicalV13ClientContractError";
    this.code = code;
  }
}

type RequestOptions = {
  body?: unknown;
  method?: "GET" | "POST";
  signal?: AbortSignal;
};

type Shape = Readonly<Record<string, "array" | "boolean" | "number" | "object" | "string" | "nullable-string">>;

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function assertShape(contract: string, value: unknown, shape: Shape): asserts value is Record<string, unknown> {
  if (!isRecord(value)) {
    throw new CanonicalV13ClientContractError("INVALID_SUCCESS_DTO", `${contract} is not an object`);
  }
  for (const [key, kind] of Object.entries(shape)) {
    const field = value[key];
    const valid = kind === "array" ? Array.isArray(field)
      : kind === "object" ? isRecord(field)
        : kind === "nullable-string" ? field === null || typeof field === "string"
          : typeof field === kind;
    if (!valid) {
      throw new CanonicalV13ClientContractError("INVALID_SUCCESS_DTO", `${contract}.${key} has invalid shape`);
    }
  }
}

function assertRecordArray(contract: string, value: unknown): asserts value is Record<string, unknown>[] {
  if (!Array.isArray(value) || !value.every(isRecord)) {
    throw new CanonicalV13ClientContractError("INVALID_SUCCESS_DTO", `${contract} is not an object array`);
  }
}

function validateSuccessDto(contract: string, value: unknown): void {
  const shapes: Record<string, Shape> = {
    submission: { submission_id: "string", strategy_id: "string", strategy_version_id: "string", intake_status: "string", catalog_status: "string", validation_status: "string", qualification_status: "string", execution_authorized: "boolean", idempotent_replay: "boolean" },
    strategy: { strategy_id: "string", display_name: "string", catalog_status: "string", intake_status: "string", validation_status: "string", qualification_status: "string", execution_authorized: "boolean" },
    strategies: { status: "string", items: "array" },
    configurations: { status: "string", configured_kinds: "array", unset_kinds: "array", items: "array" },
    configurationDraft: { profile_id: "string", version_id: "string", configuration_kind: "string", lifecycle_status: "string" },
    configurationValidation: { snapshot_id: "string", version_id: "string", lifecycle_status: "string", snapshot_digest: "string", repeat_noop: "boolean" },
    bundlePreview: { status: "string", reason_codes: "array", snapshot_ids: "object", snapshot_digests: "object", bundle_digest: "nullable-string", prospective_bundle_id: "nullable-string" },
    bundleActivation: { configuration_bundle_id: "string", configuration_activation_id: "string", bundle_digest: "string", repeat_noop: "boolean", created_bundle: "boolean", execution_side_effects: "number" },
    marketInventory: { status: "string", profile_count: "number", artifact_count: "number", snapshots: "array" },
    marketSnapshot: { snapshot_id: "string", snapshot_digest: "string", status: "string", reason_codes: "array", members: "array" },
    readiness: { status: "string", reason_codes: "array", configuration_bundle_id: "nullable-string", deployment_id: "nullable-string", runtime_instance_id: "nullable-string" },
    optimizations: { status: "string", items: "array" },
  };
  const shape = shapes[contract];
  if (!shape) throw new CanonicalV13ClientContractError("UNKNOWN_SUCCESS_DTO", contract);
  assertShape(contract, value, shape);
  if (["strategies", "configurations", "marketInventory", "marketSnapshot", "optimizations"].includes(contract)) {
    const itemsKey = contract === "marketInventory" ? "snapshots" : contract === "marketSnapshot" ? "members" : "items";
    assertRecordArray(`${contract}.${itemsKey}`, value[itemsKey]);
    if (contract === "configurations") {
      for (const [index, profile] of (value.items as Record<string, unknown>[]).entries()) {
        assertShape(`configurations.items[${index}]`, profile, { profile_id: "string", configuration_kind: "string", versions: "array" });
        assertRecordArray(`configurations.items[${index}].versions`, profile.versions);
      }
    }
  }
}

function isErrorResponse(value: unknown): value is CanonicalErrorResponse {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<CanonicalErrorResponse>;
  return candidate.status === "BLOCKED"
    && Boolean(candidate.error)
    && typeof candidate.error?.code === "string"
    && typeof candidate.error?.detail === "string";
}

async function request<T>(path: string, contract: string, options: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(`${CANONICAL_V13_API_ROOT}${path}`, {
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    headers,
    method: options.method ?? "GET",
    signal: options.signal,
  });
  let payload: unknown = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!response.ok) {
    if (isErrorResponse(payload)) {
      throw new CanonicalV13ApiError(response.status, payload.error.code, payload.error.detail);
    }
    throw new CanonicalV13ApiError(
      response.status,
      "BLOCKED_CANONICAL_API_RESPONSE",
      `canonical API returned ${response.status} ${response.statusText}`,
    );
  }
  validateSuccessDto(contract, payload);
  return payload as T;
}

function segment(value: string): string {
  if (!value.trim()) {
    throw new CanonicalV13ClientContractError("INVALID_PATH_IDENTITY", "path identity is empty");
  }
  return encodeURIComponent(value);
}

export function submitCanonicalStrategy(command: SubmissionCommand, signal?: AbortSignal) {
  return request<SubmissionReceipt>("/submissions", "submission", { body: command, method: "POST", signal });
}

export function fetchCanonicalStrategies(signal?: AbortSignal, limit?: number) {
  if (limit !== undefined && (!Number.isInteger(limit) || limit < 1 || limit > 200)) {
    throw new CanonicalV13ClientContractError("INVALID_LIMIT", "strategy limit must be 1..200");
  }
  const query = limit === undefined ? "" : `?limit=${limit}`;
  return request<StrategyCatalogProjection>(`/strategies${query}`, "strategies", { signal });
}

export function fetchCanonicalStrategy(strategyId: string, signal?: AbortSignal) {
  return request<StrategyProjection>(`/strategies/${segment(strategyId)}`, "strategy", { signal });
}

export function fetchCanonicalConfigurations(signal?: AbortSignal) {
  return request<ConfigurationCatalogProjection>("/configurations", "configurations", { signal });
}

export function createCanonicalConfigurationDraft(
  kind: string,
  command: ConfigurationDraftCommand,
  signal?: AbortSignal,
) {
  return request<ConfigurationDraftResult>(`/configurations/${segment(kind)}/drafts`, "configurationDraft", {
    body: command,
    method: "POST",
    signal,
  });
}

export function validateCanonicalConfiguration(
  kind: string,
  versionId: string,
  command: ConfigurationValidateCommand,
  signal?: AbortSignal,
) {
  return request<ConfigurationValidationResult>(
    `/configurations/${segment(kind)}/${segment(versionId)}/validate`,
    "configurationValidation",
    { body: command, method: "POST", signal },
  );
}

export function previewCanonicalResearchBundle(
  command: ResearchBundlePreviewCommand,
  signal?: AbortSignal,
) {
  return request<ResearchBundlePreview>("/research-bundles/preview", "bundlePreview", {
    body: command,
    method: "POST",
    signal,
  });
}

export function activateCanonicalResearchBundle(
  bundleId: string,
  command: ResearchBundleActivateCommand,
  signal?: AbortSignal,
) {
  return request<ResearchBundleActivation>(`/research-bundles/${segment(bundleId)}/activate`, "bundleActivation", {
    body: command,
    method: "POST",
    signal,
  });
}

export function fetchCanonicalMarketInventory(signal?: AbortSignal) {
  return request<MarketInventoryProjection>("/market-data", "marketInventory", { signal });
}

export function fetchCanonicalMarketSnapshot(snapshotId: string, signal?: AbortSignal) {
  return request<MarketSnapshotProjection>(`/market-data/snapshots/${segment(snapshotId)}`, "marketSnapshot", { signal });
}

export function fetchCanonicalResearchReadiness(
  scopeKey?: string | null,
  workflowKey?: string | null,
  signal?: AbortSignal,
) {
  if (Boolean(scopeKey) !== Boolean(workflowKey)) {
    throw new CanonicalV13ClientContractError(
      "RESEARCH_SCOPE_INCOMPLETE",
      "scope and workflow must be supplied together",
    );
  }
  const query = new URLSearchParams();
  if (scopeKey && workflowKey) {
    query.set("scope_key", scopeKey);
    query.set("workflow_key", workflowKey);
  }
  const suffix = query.size ? `?${query.toString()}` : "";
  return request<ReadinessProjection>(`/readiness/research${suffix}`, "readiness", { signal });
}

export function fetchCanonicalRuntimeReadiness(signal?: AbortSignal) {
  return request<ReadinessProjection>("/readiness/runtime", "readiness", { signal });
}

export function fetchCanonicalOptimizations(signal?: AbortSignal) {
  return request<OptimizationListProjection>("/optimizations", "optimizations", { signal });
}
