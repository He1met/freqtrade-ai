import type {
  CanonicalErrorResponse,
  ConfigurationCatalogProjection,
  ConfigurationDraftCommand,
  ConfigurationDraftResult,
  ConfigurationValidateCommand,
  ConfigurationValidationResult,
  GateListProjection,
  MarketInventoryProjection,
  MarketSnapshotProjection,
  OptimizationListProjection,
  Phase9AcceptanceStage,
  Phase9ReadinessProjection,
  ReadinessProjection,
  ResearchRunCatalogProjection,
  ResearchPlanCatalogProjection,
  ResearchResultsProjection,
  ResearchChainProjection,
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

type Shape = Readonly<Record<string, "array" | "boolean" | "number" | "object" | "string" | "nullable-array" | "nullable-number" | "nullable-object" | "nullable-string">>;

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
        : kind === "nullable-array" ? field === null || Array.isArray(field)
          : kind === "nullable-number" ? field === null || typeof field === "number"
        : kind === "nullable-object" ? field === null || isRecord(field)
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
    strategy: { strategy_id: "string", display_name: "string", catalog_status: "string", intake_status: "string", validation_status: "string", validation_status_domain: "string", qualification_status: "string", qualification_status_domain: "string", execution_authorized: "boolean" },
    strategies: { status: "string", items: "array" },
    configurations: { status: "string", configured_kinds: "array", unset_kinds: "array", items: "array" },
    configurationDraft: { profile_id: "string", version_id: "string", configuration_kind: "string", lifecycle_status: "string", idempotency_receipt_id: "string", receipt_digest: "string", idempotent_replay: "boolean" },
    configurationValidation: { snapshot_id: "string", version_id: "string", lifecycle_status: "string", snapshot_digest: "string", repeat_noop: "boolean", idempotency_receipt_id: "string", receipt_digest: "string", idempotent_replay: "boolean" },
    bundlePreview: { status: "string", reason_codes: "array", snapshot_ids: "object", snapshot_digests: "object", bundle_digest: "nullable-string", prospective_bundle_id: "nullable-string" },
    bundleActivation: { configuration_bundle_id: "string", configuration_activation_id: "string", bundle_digest: "string", repeat_noop: "boolean", created_bundle: "boolean", execution_side_effects: "number" },
    marketInventory: { status: "string", profile_count: "number", artifact_count: "number", profiles: "array", snapshots: "array" },
    marketSnapshot: { snapshot_id: "string", snapshot_digest: "string", status: "string", reason_codes: "array", members: "array" },
    readiness: { status: "string", reason_codes: "array", configuration_bundle_id: "nullable-string", deployment_id: "nullable-string", runtime_instance_id: "nullable-string" },
    optimizations: { status: "string", items: "array" },
    researchChain: { validation_plan_id: "string", validation_plan_digest: "string", strategy_version_id: "string", research_target_id: "string", target_key: "string", plan_status: "string", validation_attempt_id: "nullable-string", attempt_status: "nullable-string", attempt_receipt_digest: "nullable-string", target_score_id: "nullable-string", overall_score: "nullable-string", score_digest: "nullable-string", qualification_decision_id: "nullable-string", qualification_status: "nullable-string", qualification_reason_code: "nullable-string", qualification_decision_digest: "nullable-string" },
    researchPlans: { status: "string", items: "array" },
    researchResults: { validation_plan_id: "string", validation_plan_digest: "string", strategy_version_id: "string", research_target_id: "string", target_key: "string", configuration_bundle_id: "string", configuration_bundle_digest: "string", market_snapshot_id: "string", market_snapshot_digest: "string", plan_status: "string", attempt: "nullable-object", windows: "array", score: "nullable-object", qualification: "nullable-object" },
    researchRun: { run_id: "string", name: "string", hypothesis: "string", universe: "array", timeframe: "string", status: "string", reason_code: "string", dataset_digest: "string", artifact_path: "string", result_digest: "string", train_validation_holdout_summary: "object", metrics_summary: "object", created_at: "string" },
    researchRuns: { status: "string", items: "array" },
    gates: { status: "string", items: "array" },
    phase9Readiness: { contract: "string", stage: "string", status: "string", reason_codes: "array", qualification_status_counts: "object", execution_domain_counts: "object", lineage_evidence_counts: "object", handoff: "nullable-object", topology_digest: "string", receipt_digest: "string" },
  };
  const shape = shapes[contract];
  if (!shape) throw new CanonicalV13ClientContractError("UNKNOWN_SUCCESS_DTO", contract);
  assertShape(contract, value, shape);
  if (["strategies", "configurations", "marketInventory", "marketSnapshot", "optimizations", "researchPlans", "researchRuns", "gates"].includes(contract)) {
    const itemsKey = contract === "marketInventory" ? "snapshots" : contract === "marketSnapshot" ? "members" : "items";
    assertRecordArray(`${contract}.${itemsKey}`, value[itemsKey]);
    if (contract === "configurations") {
      for (const [index, profile] of (value.items as Record<string, unknown>[]).entries()) {
        assertShape(`configurations.items[${index}]`, profile, { profile_id: "string", configuration_kind: "string", versions: "array" });
        assertRecordArray(`configurations.items[${index}].versions`, profile.versions);
        for (const [versionIndex, version] of (profile.versions as Record<string, unknown>[]).entries()) {
          assertShape(`configurations.items[${index}].versions[${versionIndex}]`, version, {
            active_activation_id: "nullable-string",
            active_bundle_digest: "nullable-string",
            active_bundle_id: "nullable-string",
            active_in_bundle: "boolean",
            payload_digest: "string",
            payload_json: "object",
            schema_digest: "string",
            schema_json: "object",
            snapshot_digest: "nullable-string",
            snapshot_id: "nullable-string",
            version_id: "string",
            version_number: "number",
          });
        }
      }
    }
    if (contract === "strategies") {
      for (const [index, strategy] of (value.items as Record<string, unknown>[]).entries()) {
        assertShape(`strategies.items[${index}]`, strategy, shapes.strategy);
      }
    }
    if (contract === "optimizations") {
      for (const [index, run] of (value.items as Record<string, unknown>[]).entries()) {
        assertShape(`optimizations.items[${index}]`, run, {
          baseline_qualification_decision_id: "string",
          completed_at: "nullable-string",
          created_at: "string",
          optimization_run_id: "string",
          receipt_digest: "nullable-string",
          request_digest: "string",
          result_count: "nullable-number",
          result_digest: "nullable-string",
          status: "string",
          submitted_strategy_count: "nullable-number",
          terminal_reason_codes: "nullable-array",
          trial_count: "nullable-number",
        });
      }
    }
    if (contract === "marketInventory") {
      assertRecordArray("marketInventory.profiles", value.profiles);
      for (const [index, profile] of (value.profiles as Record<string, unknown>[]).entries()) {
        assertShape(`marketInventory.profiles[${index}]`, profile, {
          created_at: "string",
          lifecycle_status: "string",
          market_profile_id: "string",
          payload_digest: "string",
          profile_key: "string",
          scope_key: "string",
          validated_at: "nullable-string",
          version_id: "string",
          version_number: "number",
        });
      }
    }
    if (contract === "researchPlans") {
      for (const [index, plan] of (value.items as Record<string, unknown>[]).entries()) {
        assertShape(`researchPlans.items[${index}]`, plan, shapes.researchChain);
      }
    }
    if (contract === "researchRuns") {
      for (const [index, run] of (value.items as Record<string, unknown>[]).entries()) {
        assertShape(`researchRuns.items[${index}]`, run, shapes.researchRun);
        if (!(run.universe as unknown[]).every((member) => typeof member === "string" && member.trim())) {
          throw new CanonicalV13ClientContractError(
            "INVALID_SUCCESS_DTO",
            `researchRuns.items[${index}].universe has invalid members`,
          );
        }
        if (run.status === "QUALIFIED") {
          throw new CanonicalV13ClientContractError(
            "INVALID_SUCCESS_DTO",
            `researchRuns.items[${index}] cannot claim QUALIFIED`,
          );
        }
      }
    }
  }
  if (contract === "researchResults") {
    assertRecordArray("researchResults.windows", value.windows);
    for (const [index, window] of (value.windows as Record<string, unknown>[]).entries()) {
      assertShape(`researchResults.windows[${index}]`, window, {
        qualification_evidence: "nullable-object",
        required: "boolean",
        result: "nullable-object",
        validation_plan_window_id: "string",
        window_end: "string",
        window_key: "string",
        window_member_digest: "string",
        window_start: "string",
      });
      if (isRecord(window.result)) {
        assertShape(`researchResults.windows[${index}].result`, window.result, {
          created_at: "string",
          metrics_digest: "string",
          metrics_json: "object",
          receipt_digest: "string",
          validation_window_result_id: "string",
        });
      }
      if (isRecord(window.qualification_evidence)) {
        assertShape(`researchResults.windows[${index}].qualification_evidence`, window.qualification_evidence, {
          evidence_digest: "string",
          gates: "array",
          hard_gate_passed: "boolean",
          qualification_window_evidence_id: "string",
        });
        assertRecordArray(
          `researchResults.windows[${index}].qualification_evidence.gates`,
          window.qualification_evidence.gates,
        );
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

export function fetchCanonicalPhase9Readiness(
  handoff: Pick<ResearchResultsProjection, "strategy_version_id" | "configuration_bundle_id" | "market_snapshot_id"> & {
    qualification_decision_id: string;
  },
  stage: Phase9AcceptanceStage,
  signal?: AbortSignal,
) {
  const query = new URLSearchParams({
    qualification_decision_id: handoff.qualification_decision_id,
    strategy_version_id: handoff.strategy_version_id,
    configuration_bundle_id: handoff.configuration_bundle_id,
    market_snapshot_id: handoff.market_snapshot_id,
    stage,
  });
  return request<Phase9ReadinessProjection>(`/phase9/readiness?${query.toString()}`, "phase9Readiness", { signal });
}

export function fetchCanonicalOptimizations(signal?: AbortSignal) {
  return request<OptimizationListProjection>("/optimizations", "optimizations", { signal });
}

export function fetchCanonicalResearchChain(validationPlanId: string, signal?: AbortSignal) {
  return request<ResearchChainProjection>(
    `/research/validation-plans/${segment(validationPlanId)}`,
    "researchChain",
    { signal },
  );
}

export function fetchCanonicalResearchPlans(signal?: AbortSignal) {
  return request<ResearchPlanCatalogProjection>("/research/validation-plans", "researchPlans", { signal });
}

export function fetchCanonicalResearchRuns(signal?: AbortSignal) {
  return request<ResearchRunCatalogProjection>("/research-runs", "researchRuns", { signal });
}

export function fetchCanonicalResearchResults(validationPlanId: string, signal?: AbortSignal) {
  return request<ResearchResultsProjection>(
    `/research/validation-plans/${segment(validationPlanId)}/results`,
    "researchResults",
    { signal },
  );
}

export function fetchCanonicalResearchGates(signal?: AbortSignal) {
  return request<GateListProjection>("/research/gates", "gates", { signal });
}
