import { fetchOwnerJson, postJson, postOwnerReadJson } from "./http";

export type ConfigurationTypeRead = {
  type_key: string;
  name_zh: string;
  description_zh: string;
  schema_version: string;
  handler_key: string;
  editor_capability: Record<string, unknown>;
  enabled: boolean;
};

export type ConfigurationVersionRead = {
  id: number;
  type_key: string;
  version_number: number;
  lifecycle_status: string;
  payload_json: Record<string, unknown>;
  schema_version: string;
  config_digest: string;
  change_summary: string | null;
  created_by: string;
  created_at: string;
  validated_at: string | null;
};

export type ConfigurationBundleResolutionRead = {
  schema_version: string;
  persisted: boolean;
  snapshot_id: number | null;
  workflow_kind: string;
  scope_type: string;
  scope_key: string;
  aggregate_profile_version_id: number;
  resolved_versions: ConfigurationVersionRead[];
  dependencies: Array<{
    configuration_version_id: number;
    configuration_type: string;
    depends_on_version_id: number;
    depends_on_type: string;
    relation_key: string;
  }>;
  resolved_versions_json: Record<string, number>;
  resolved_digests_json: Record<string, string>;
  bundle_digest: string;
  capability_snapshot: Record<string, unknown>;
};

export type ActiveConfigurationRead = {
  scope_type: string;
  scope_key: string;
  activated_at: string;
  activated_by: string;
  configuration_type: ConfigurationTypeRead;
  version: ConfigurationVersionRead;
};

export type ConfigurationBundleSnapshotRead = ConfigurationBundleResolutionRead & {
  persisted: true;
  snapshot_id: number;
  created_at: string;
};

export type ConfigurationDependencyRead = {
  configuration_version_id: number;
  configuration_type: string;
  depends_on_version_id: number;
  depends_on_type: string;
  relation_key: string;
};

export type ConfigurationVersionListRead = {
  config_type: string;
  scope_type: string;
  scope_key: string;
  active_version_id: number | null;
  items: ConfigurationVersionRead[];
};

export type ConfigurationVersionDetailRead = {
  version: ConfigurationVersionRead;
  dependencies: ConfigurationDependencyRead[];
};

export type ConfigurationWriteResult = {
  schema_version: string;
  request_id: string;
  idempotent_replay: boolean;
  operation: string;
  scope_type: string;
  scope_key: string;
  version: ConfigurationVersionRead;
  dependencies: ConfigurationDependencyRead[];
  previous_active_version_id: number | null;
  active_version_id: number | null;
  validation_bundle: ConfigurationBundleResolutionRead | null;
};

export type ConfigurationAuditEventRead = {
  id: number;
  configuration_version_id: number;
  event_type: string;
  actor: string;
  request_id: string;
  scope_type: string | null;
  scope_key: string | null;
  reason: string | null;
  event_snapshot: Record<string, unknown>;
  created_at: string;
};

export type ConfigurationVersionDiffRead = {
  config_type: string;
  scope_type: string;
  scope_key: string;
  from_version_id: number;
  to_version_id: number;
  items: Array<{ path: string; before: unknown; after: unknown }>;
};

export type StrategyTargetProjectionRead = {
  id: number;
  strategy_version_id: number;
  execution_target_id: number;
  execution_target_key: string;
  instrument_id: string;
  pair: string;
  timeframe: string;
  status: string;
  validation_priority: number;
  latest_validation_plan_id: number | null;
  research_status: string;
  last_completed_validation_at: string | null;
  next_validation_not_before: string | null;
  created_at: string;
  updated_at: string;
};

export type StrategyCatalogPageRead = {
  schema_version: string;
  items: Array<{
    id: number;
    name: string;
    slug: string;
    description: string | null;
    source: string;
    tags: string[];
    catalog_status: string;
    current_version: {
      id: number;
      version_number: number;
      static_validation_status: string;
      created_at: string;
    } | null;
    targets: StrategyTargetProjectionRead[];
    target_count: number;
    created_at: string;
    updated_at: string;
  }>;
  next_cursor: string | null;
};

export type DynamicValidationWindowRead = {
  id: number;
  window_config_id: number | null;
  window_key: string | null;
  ordinal: number;
  attempt_number: number;
  name_zh: string | null;
  description_zh: string | null;
  projection_status: string;
  score: number | null;
  status: string;
  net_profit_after_cost: number | null;
  max_drawdown: number | null;
  volatility: number | null;
  total_trades: number | null;
  failure_reasons: Array<{
    code: string;
    message: string | null;
    quality_gate_rule_id: number | null;
    actual_value: number | null;
    operator: string | null;
    threshold_snapshot: Record<string, unknown> | null;
  }>;
};

export type StrategyValidationHistoryRead = {
  schema_version: string;
  strategy_id: number;
  cycles: Array<{
    id: number;
    strategy_version_id: number;
    strategy_target_id: number | null;
    target: StrategyTargetProjectionRead | null;
    cycle_number: number | null;
    status: string;
    required_window_count: number | null;
    passed_window_count: number | null;
    failed_window_count: number | null;
    overall_score: number | null;
    reason_codes: string[];
    configuration_bundle_snapshot_id: number | null;
    validation_window_config_set_id: number | null;
    created_at: string;
    started_at: string | null;
    completed_at: string | null;
    windows: DynamicValidationWindowRead[];
  }>;
};

export function fetchConfigurationCatalog(operatorToken: string, signal?: AbortSignal) {
  return fetchOwnerJson<{ schema_version: string; items: ConfigurationTypeRead[] }>(
    "/v1/configuration-catalog",
    operatorToken,
    signal,
  );
}

export function fetchActiveConfiguration(
  configType: string,
  scope: { scope_type: string; scope_key: string },
  operatorToken: string,
  signal?: AbortSignal,
) {
  const query = new URLSearchParams(scope);
  return fetchOwnerJson<ActiveConfigurationRead>(
    `/v1/configurations/${encodeURIComponent(configType)}/active?${query.toString()}`,
    operatorToken,
    signal,
  );
}

export function fetchConfigurationVersions(
  configType: string,
  scope: { scope_type: string; scope_key: string },
  operatorToken: string,
  signal?: AbortSignal,
) {
  const query = new URLSearchParams(scope);
  return fetchOwnerJson<ConfigurationVersionListRead>(
    `/v1/configurations/${encodeURIComponent(configType)}/versions?${query.toString()}`,
    operatorToken,
    signal,
  );
}

export function fetchConfigurationVersionDetail(
  configType: string,
  versionId: number,
  operatorToken: string,
  signal?: AbortSignal,
) {
  return fetchOwnerJson<ConfigurationVersionDetailRead>(
    `/v1/configurations/${encodeURIComponent(configType)}/versions/${versionId}`,
    operatorToken,
    signal,
  );
}

export function fetchConfigurationAuditEvents(
  configType: string,
  scope: { scope_type: string; scope_key: string },
  operatorToken: string,
  signal?: AbortSignal,
) {
  const query = new URLSearchParams(scope);
  return fetchOwnerJson<{
    config_type: string;
    scope_type: string;
    scope_key: string;
    items: ConfigurationAuditEventRead[];
  }>(
    `/v1/configurations/${encodeURIComponent(configType)}/audit-events?${query.toString()}`,
    operatorToken,
    signal,
  );
}

export function fetchConfigurationBundleHistory(
  scope: { scope_type: string; scope_key: string },
  operatorToken: string,
  signal?: AbortSignal,
) {
  const query = new URLSearchParams(scope);
  return fetchOwnerJson<{
    scope_type: string;
    scope_key: string;
    items: ConfigurationBundleSnapshotRead[];
  }>(`/v1/configuration-bundles?${query.toString()}`, operatorToken, signal);
}

export function fetchConfigurationVersionDiff(
  configType: string,
  versionId: number,
  againstVersionId: number,
  scope: { scope_type: string; scope_key: string },
  operatorToken: string,
  signal?: AbortSignal,
) {
  const query = new URLSearchParams({
    ...scope,
    against_version_id: String(againstVersionId),
  });
  return fetchOwnerJson<ConfigurationVersionDiffRead>(
    `/v1/configurations/${encodeURIComponent(configType)}/versions/${versionId}/diff?${query.toString()}`,
    operatorToken,
    signal,
  );
}

export function createConfigurationDraft(
  configType: string,
  request: {
    scope_type: string;
    scope_key: string;
    change_summary: string;
    source_version_id?: number;
    payload_json: Record<string, unknown>;
    dependencies: Array<{ depends_on_version_id: number; relation_key: string }>;
  },
  operatorToken: string,
  idempotencyKey: string,
  signal?: AbortSignal,
) {
  return postJson<ConfigurationWriteResult>(
    `/v1/configurations/${encodeURIComponent(configType)}/versions`,
    request,
    { operatorToken, idempotencyKey, signal },
  );
}

export function applyConfigurationVersionAction(
  action: "validate" | "activate" | "retire",
  configType: string,
  versionId: number,
  request: { scope_type: string; scope_key: string; reason?: string },
  operatorToken: string,
  idempotencyKey: string,
  signal?: AbortSignal,
) {
  return postJson<ConfigurationWriteResult>(
    `/v1/configurations/${encodeURIComponent(configType)}/versions/${versionId}/${action}`,
    request,
    { operatorToken, idempotencyKey, signal },
  );
}

export function resolveConfigurationBundle(
  request: {
    workflow_kind: string;
    aggregate_config_type: string;
    scope_type: string;
    scope_key: string;
  },
  operatorToken: string,
  signal?: AbortSignal,
) {
  return postOwnerReadJson<ConfigurationBundleResolutionRead>(
    "/v1/configuration-bundles/resolve",
    request,
    operatorToken,
    signal,
  );
}

export function fetchConfigurationBundle(
  bundleId: number,
  operatorToken: string,
  signal?: AbortSignal,
) {
  return fetchOwnerJson<ConfigurationBundleSnapshotRead>(
    `/v1/configuration-bundles/${bundleId}`,
    operatorToken,
    signal,
  );
}

export function fetchStrategyCatalog(
  operatorToken: string,
  options: { limit?: number; cursor?: string; signal?: AbortSignal } = {},
) {
  const query = new URLSearchParams();
  if (options.limit !== undefined) query.set("limit", String(options.limit));
  if (options.cursor) query.set("cursor", options.cursor);
  const suffix = query.size ? `?${query.toString()}` : "";
  return fetchOwnerJson<StrategyCatalogPageRead>(
    `/v1/strategy-catalog${suffix}`,
    operatorToken,
    options.signal,
  );
}

export function fetchStrategyValidationHistory(
  strategyId: number,
  operatorToken: string,
  signal?: AbortSignal,
) {
  return fetchOwnerJson<StrategyValidationHistoryRead>(
    `/v1/strategies/${strategyId}/validation-history`,
    operatorToken,
    signal,
  );
}
