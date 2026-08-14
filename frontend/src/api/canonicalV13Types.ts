export type CanonicalId = string;
export type Sha256Digest = string;
export type IsoDateTime = string;

export const CANONICAL_CONFIGURATION_KINDS = [
  "TARGET",
  "WINDOW",
  "GENERATION",
  "DIVERSITY",
  "QUALITY_QUALIFICATION",
  "SCORING",
  "RESEARCH_AGGREGATE",
] as const;

export type CanonicalConfigurationKind = typeof CANONICAL_CONFIGURATION_KINDS[number];

export type CanonicalErrorResponse = {
  status: "BLOCKED";
  error: { code: string; detail: string };
};

export type SubmissionVersionCommand = {
  source_strategy_key: string;
  version_id: string;
  version_number: number;
  artifact_base64: string;
};

export type SubmissionCommand = {
  caller_identity: string;
  idempotency_key: string;
  display_name: string;
  archive_snapshot_digest: Sha256Digest;
  source_entry_key: string;
  source_strategy_key: string;
  current_version_id: string;
  versions: SubmissionVersionCommand[];
};

export type SubmissionReceipt = {
  submission_id: CanonicalId;
  artifact_id: CanonicalId;
  strategy_id: CanonicalId;
  strategy_version_id: CanonicalId;
  intake_receipt_id: CanonicalId;
  request_digest: Sha256Digest;
  artifact_digest: Sha256Digest;
  receipt_digest: Sha256Digest;
  intake_status: "INTAKE_ACCEPTED";
  catalog_status: "DRAFT";
  validation_status: "UNVALIDATED";
  qualification_status: "NOT_EVALUATED";
  execution_authorized: false;
  idempotent_replay: boolean;
};

export type StrategyProjection = {
  strategy_id: CanonicalId;
  display_name: string;
  catalog_status: "DRAFT" | "ACTIVE" | "ARCHIVED";
  intake_status: "INTAKE_ACCEPTED" | "REJECTED" | "BLOCKED";
  current_version_id: CanonicalId;
  version_number: number;
  artifact_id: CanonicalId;
  artifact_digest: Sha256Digest;
  validation_status: "UNVALIDATED" | "VALIDATING" | "VALIDATED" | "REJECTED" | "BLOCKED";
  qualification_status: "NOT_EVALUATED" | "PENDING" | "QUALIFIED" | "REJECTED" | "BLOCKED" | "FAILED";
  execution_authorized: boolean;
  created_at: IsoDateTime;
};

export type StrategyCatalogProjection = {
  status: "EMPTY" | "AVAILABLE";
  items: StrategyProjection[];
};

export type ConfigurationDependencyCommand = {
  version_id: CanonicalId;
  expected_kind: string;
  relation_key: string;
};

export type ConfigurationDraftCommand = {
  actor_identity: string;
  idempotency_key: string;
  profile_key: string;
  scope_key: string;
  workflow_key: string;
  schema_json: Record<string, unknown>;
  payload_json: Record<string, unknown>;
  adapter_identity: string;
  adapter_digest: Sha256Digest;
  dependencies: ConfigurationDependencyCommand[];
};

export type ConfigurationValidateCommand = {
  actor_identity: string;
  idempotency_key: string;
  adapter_manifest_digest: Sha256Digest;
};

export type ConfigurationDraftResult = {
  profile_id: CanonicalId;
  version_id: CanonicalId;
  version_number: number;
  configuration_kind: string;
  lifecycle_status: "DRAFT";
  schema_digest: Sha256Digest;
  payload_digest: Sha256Digest;
  idempotency_receipt_id: CanonicalId;
  receipt_digest: Sha256Digest;
  idempotent_replay: boolean;
};

export type ConfigurationValidationResult = {
  snapshot_id: CanonicalId;
  version_id: CanonicalId;
  configuration_kind: string;
  lifecycle_status: "VALIDATED";
  snapshot_digest: Sha256Digest;
  dependency_digest: Sha256Digest;
  member_count: number;
  target_count: number;
  total_candidate_count: number;
  repeat_noop: boolean;
  idempotency_receipt_id: CanonicalId;
  receipt_digest: Sha256Digest;
  idempotent_replay: boolean;
};

export type ConfigurationVersionProjection = {
  version_id: CanonicalId;
  version_number: number;
  lifecycle_status: "DRAFT" | "VALIDATED" | "RETIRED";
  schema_json: Record<string, unknown>;
  payload_json: Record<string, unknown>;
  schema_digest: Sha256Digest;
  payload_digest: Sha256Digest;
  adapter_identity: string;
  adapter_digest: Sha256Digest;
  snapshot_id: CanonicalId | null;
  snapshot_digest: Sha256Digest | null;
  created_at: IsoDateTime;
  validated_at: IsoDateTime | null;
};

export type ConfigurationProfileProjection = {
  profile_id: CanonicalId;
  profile_key: string;
  configuration_kind: string;
  scope_key: string;
  workflow_key: string;
  versions: ConfigurationVersionProjection[];
};

export type ConfigurationCatalogProjection = {
  status: "UNSET" | "AVAILABLE";
  configured_kinds: string[];
  unset_kinds: string[];
  items: ConfigurationProfileProjection[];
};

export type ResearchBundlePreviewCommand = {
  scope_key: string;
  workflow_key: string;
  snapshot_ids: Record<string, CanonicalId>;
  market_snapshot_id: CanonicalId | null;
};

export type ResearchBundleActivateCommand = ResearchBundlePreviewCommand & {
  actor_identity: string;
  expected_bundle_digest: Sha256Digest;
};

export type ResearchBundlePreview = {
  status: "READY" | "BLOCKED";
  reason_codes: string[];
  scope_key: string;
  workflow_key: string;
  snapshot_ids: Record<string, CanonicalId>;
  snapshot_digests: Record<string, Sha256Digest>;
  market_snapshot_id: CanonicalId | null;
  market_snapshot_digest: Sha256Digest | null;
  target_count: number;
  total_candidate_count: number;
  capability_json: Record<string, unknown>;
  bundle_digest: Sha256Digest | null;
  prospective_bundle_id: CanonicalId | null;
};

export type ResearchBundleActivation = {
  configuration_bundle_id: CanonicalId;
  configuration_activation_id: CanonicalId;
  bundle_digest: Sha256Digest;
  previous_bundle_id: CanonicalId | null;
  repeat_noop: boolean;
  created_bundle: boolean;
  execution_side_effects: 0;
};

export type MarketSnapshotSummary = {
  snapshot_id: CanonicalId;
  snapshot_digest: Sha256Digest;
  market_profile_version_id: CanonicalId;
  member_count: number;
  created_at: IsoDateTime;
};

export type MarketInventoryProjection = {
  status: "MARKET_SNAPSHOT_UNSET" | "AVAILABLE";
  profile_count: number;
  validated_profile_count: number;
  artifact_count: number;
  accepted_receipt_count: number;
  snapshots: MarketSnapshotSummary[];
};

export type MarketSnapshotMemberProjection = {
  market_artifact_id: CanonicalId;
  artifact_digest: Sha256Digest;
  market_receipt_id: CanonicalId;
  receipt_digest: Sha256Digest;
  receipt_status: "ACCEPTED" | "REJECTED" | "BLOCKED";
  research_target_id: CanonicalId;
  target_key: string;
  coverage_start: IsoDateTime;
  coverage_end: IsoDateTime;
  coverage_digest: Sha256Digest;
};

export type MarketSnapshotProjection = {
  snapshot_id: CanonicalId;
  snapshot_digest: Sha256Digest;
  market_profile_version_id: CanonicalId;
  status: "ACCEPTED" | "BLOCKED";
  reason_codes: string[];
  members: MarketSnapshotMemberProjection[];
  created_at: IsoDateTime;
};

export type ReadinessProjection = {
  status: "READY" | "BLOCKED" | "PENDING_FIRST_BACKTEST";
  reason_codes: string[];
  scope_key: string | null;
  workflow_key: string | null;
  configuration_bundle_id: CanonicalId | null;
  bundle_digest: Sha256Digest | null;
  market_snapshot_id: CanonicalId | null;
  target_count: number | null;
  total_candidate_count: number | null;
  deployment_id: CanonicalId | null;
  runtime_instance_id: CanonicalId | null;
};

export type OptimizationProjection = {
  optimization_run_id: CanonicalId;
  baseline_qualification_decision_id: CanonicalId;
  status: "NOT_STARTED" | "PENDING_BASELINE" | "RUNNING" | "SUCCEEDED" | "FAILED" | "BLOCKED";
  request_digest: Sha256Digest;
  receipt_digest: Sha256Digest | null;
  created_at: IsoDateTime;
  completed_at: IsoDateTime | null;
};

export type OptimizationListProjection = {
  status: "PENDING_FIRST_BACKTEST" | "AVAILABLE";
  items: OptimizationProjection[];
};
