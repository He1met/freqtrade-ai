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
  active_in_bundle: boolean;
  active_bundle_id: CanonicalId | null;
  active_bundle_digest: Sha256Digest | null;
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

export type MarketProfileVersionProjection = {
  market_profile_id: CanonicalId;
  profile_key: string;
  scope_key: string;
  version_id: CanonicalId;
  version_number: number;
  lifecycle_status: "DRAFT" | "VALIDATED" | "RETIRED";
  payload_digest: Sha256Digest;
  created_at: IsoDateTime;
  validated_at: IsoDateTime | null;
};

export type MarketInventoryProjection = {
  status: "MARKET_SNAPSHOT_UNSET" | "AVAILABLE";
  profile_count: number;
  validated_profile_count: number;
  artifact_count: number;
  accepted_receipt_count: number;
  profiles: MarketProfileVersionProjection[];
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

export type ResearchChainProjection = {
  validation_plan_id: CanonicalId;
  validation_plan_digest: Sha256Digest;
  strategy_version_id: CanonicalId;
  research_target_id: CanonicalId;
  target_key: string;
  plan_status: "DECLARED" | "READY" | "RUNNING" | "COMPLETE" | "FAILED" | "BLOCKED";
  validation_attempt_id: CanonicalId | null;
  attempt_status: "PENDING" | "RUNNING" | "SUCCEEDED" | "FAILED" | "BLOCKED" | null;
  attempt_receipt_digest: Sha256Digest | null;
  target_score_id: CanonicalId | null;
  overall_score: string | null;
  score_digest: Sha256Digest | null;
  qualification_decision_id: CanonicalId | null;
  qualification_status: "QUALIFIED" | "REJECTED" | "BLOCKED" | "FAILED" | null;
  qualification_reason_code: string | null;
  qualification_decision_digest: Sha256Digest | null;
};

export type ResearchAttemptProjection = {
  validation_attempt_id: CanonicalId;
  attempt_number: number;
  status: "PENDING" | "RUNNING" | "SUCCEEDED" | "FAILED" | "BLOCKED";
  executor_identity: string;
  executor_image_digest: Sha256Digest;
  receipt_digest: Sha256Digest | null;
  created_at: IsoDateTime;
  completed_at: IsoDateTime | null;
};

export type ResearchWindowResultProjection = {
  validation_window_result_id: CanonicalId;
  metrics_json: Record<string, unknown>;
  metrics_digest: Sha256Digest;
  receipt_digest: Sha256Digest;
  created_at: IsoDateTime;
};

export type ResearchGateEvaluationProjection = {
  gate_key: string;
  metric: string;
  operator: ">=" | ">" | "<=" | "<" | "==";
  threshold: string;
  observed: string;
  passed: boolean;
};

export type ResearchQualificationWindowEvidenceProjection = {
  qualification_window_evidence_id: CanonicalId;
  hard_gate_passed: boolean;
  gates: ResearchGateEvaluationProjection[];
  evidence_digest: Sha256Digest;
};

export type ResearchWindowProjection = {
  validation_plan_window_id: CanonicalId;
  window_key: string;
  required: boolean;
  window_start: IsoDateTime;
  window_end: IsoDateTime;
  window_member_digest: Sha256Digest;
  result: ResearchWindowResultProjection | null;
  qualification_evidence: ResearchQualificationWindowEvidenceProjection | null;
};

export type ResearchScoreProjection = {
  target_score_id: CanonicalId;
  scoring_snapshot_id: CanonicalId;
  overall_score: string;
  required_window_result_set_digest: Sha256Digest;
  score_digest: Sha256Digest;
  scorer_identity: string;
  created_at: IsoDateTime;
};

export type ResearchQualificationProjection = {
  qualification_decision_id: CanonicalId;
  target_score_id: CanonicalId;
  quality_snapshot_id: CanonicalId;
  status: "PENDING" | "QUALIFIED" | "REJECTED" | "BLOCKED" | "FAILED";
  reason_code: string;
  decision_digest: Sha256Digest;
  qualifier_identity: string;
  evidence_count: number;
  created_at: IsoDateTime;
};

export type ResearchResultsProjection = {
  validation_plan_id: CanonicalId;
  validation_plan_digest: Sha256Digest;
  strategy_version_id: CanonicalId;
  research_target_id: CanonicalId;
  target_key: string;
  configuration_bundle_id: CanonicalId;
  configuration_bundle_digest: Sha256Digest;
  market_snapshot_id: CanonicalId;
  market_snapshot_digest: Sha256Digest;
  plan_status: ResearchChainProjection["plan_status"];
  attempt: ResearchAttemptProjection | null;
  windows: ResearchWindowProjection[];
  score: ResearchScoreProjection | null;
  qualification: ResearchQualificationProjection | null;
};

export type Phase9AcceptanceStage =
  | "QUALIFICATION_HANDOFF"
  | "NO_ORDER_SOAK"
  | "SIGNAL_RISK_SHADOW"
  | "OKX_DEMO_CANARY"
  | "RECOVERY_SOAK";

export type Phase9QualificationHandoffProjection = {
  qualification_decision_id: CanonicalId;
  qualification_decision_digest: Sha256Digest;
  strategy_version_id: CanonicalId;
  research_target_id: CanonicalId;
  configuration_bundle_id: CanonicalId;
  configuration_bundle_digest: Sha256Digest;
  market_snapshot_id: CanonicalId;
  market_snapshot_digest: Sha256Digest;
  validation_plan_id: CanonicalId;
  validation_plan_digest: Sha256Digest;
};

export type Phase9ReadinessProjection = {
  contract: "canonical-v13-phase9-readiness-receipt-v2";
  stage: Phase9AcceptanceStage;
  status: "READY" | "BLOCKED";
  reason_codes: string[];
  qualification_status_counts: Record<string, number>;
  execution_domain_counts: Record<string, number>;
  lineage_evidence_counts: Record<string, number>;
  handoff: Phase9QualificationHandoffProjection | null;
  topology_digest: Sha256Digest;
  receipt_digest: Sha256Digest;
};

export type ResearchPlanCatalogProjection = {
  status: "EMPTY" | "AVAILABLE";
  items: ResearchChainProjection[];
};

export type GateProjection = {
  gate_attempt_id: CanonicalId;
  strategy_version_id: CanonicalId;
  research_target_id: CanonicalId;
  configuration_bundle_id: CanonicalId;
  configuration_bundle_digest: Sha256Digest;
  market_snapshot_id: CanonicalId;
  market_snapshot_digest: Sha256Digest;
  status: "PENDING" | "RUNNING" | "PASSED" | "FAILED" | "BLOCKED";
  terminal_reason_code: string | null;
  static_status: "PASSED" | "FAILED" | "BLOCKED" | null;
  static_reason_code: string | null;
  static_receipt_id: CanonicalId | null;
  static_receipt_digest: Sha256Digest | null;
  lookahead_status: "PASSED" | "FAILED" | "BLOCKED" | null;
  lookahead_reason_code: string | null;
  lookahead_receipt_id: CanonicalId | null;
  lookahead_receipt_digest: Sha256Digest | null;
  observed_signal_count: number | null;
  observed_trade_count: number | null;
  required_trade_count: number | null;
  validation_eligible: boolean;
  created_at: IsoDateTime;
  completed_at: IsoDateTime | null;
};

export type GateListProjection = {
  status: "AVAILABLE" | "EMPTY";
  items: GateProjection[];
};
