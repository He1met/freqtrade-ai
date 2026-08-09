import { fetchJson, postJson } from "./http";

export type StrategyResearchRejectionReason = {
  code: string;
  message: string;
  evidence: Record<string, unknown>;
};

export type StrategyResearchQualityContract = {
  contract_version: "formal-strategy-research-aggressive-v1";
  risk_profile: "AGGRESSIVE";
  profile_label: "进攻型：最大回撤 15%";
  min_strategy_score: 50;
  min_trades_per_validation_window: 30;
  validation_requires_positive_net_profit: true;
  max_drawdown_per_validation_window: 0.15;
  lookahead_analysis_required: true;
  fee_per_side: 0.0005;
  slippage_per_side: 0.0002;
  required_validation_windows: ["wf_bull", "wf_range", "oos", "wf_bear"];
  score_source: string;
};

export type StrategyResearchCandidate = {
  id: number;
  batch_id: number;
  candidate_name: string;
  status: "QUALIFIED" | "REJECTED" | "VALIDATION_FAILED";
  source_path: string;
  code_digest: string;
  loadable: boolean;
  static_check: string;
  lookahead_status: string;
  score: number | null;
  validation_passed: boolean;
  deployable_candidate: boolean;
  rejection_reasons: StrategyResearchRejectionReason[];
  evidence_snapshot: Record<string, unknown>;
  quality_contract: StrategyResearchQualityContract;
};

export type StrategyResearchBatch = {
  id: number;
  run_id: string;
  status: "GENERATED" | "VALIDATED" | "FAILED";
  requested_count: number;
  generated_count: number;
  persisted_count: number;
  qualified_count: number;
  rejected_count: number;
  failure_reason: string | null;
  report_path: string;
  report_digest: string;
  repository_commit: string;
  completed_at: string | null;
  created_at: string;
  selection_policy: StrategyResearchQualityContract;
  candidates: StrategyResearchCandidate[];
};

export type FormalResearchRun = {
  status: "READY" | "RUNNING" | "COMPLETED" | "BLOCKED" | "FAILED";
  reason_code: string;
  reason: string;
  active: boolean;
  run_id: string | null;
  trigger: "manual" | "automation" | null;
  started_at: string | null;
  completed_at: string | null;
  requested_count: number;
  generated_count: number;
  validated_count: number;
  persisted_count: number;
  qualified_count: number;
  rejected_count: number;
  deployment_handoff_status:
    | "NOT_EVALUATED"
    | "NOT_QUEUED_NO_QUALIFIED"
    | "CANONICAL_LINK_UNAVAILABLE";
  quality_contract: StrategyResearchQualityContract;
  safety: {
    execution_target: "OKX_DEMO";
    allow_real_funds: false;
    real_orders: false;
    credentials_collected: false;
    dry_run_trading_authorized: false;
    grant_authorized: false;
    manual_order_authorized: false;
  };
};

export type StrategyResearchAttemptEvent = {
  id: number;
  attempt_id: string;
  sequence: number;
  run_id: string | null;
  batch_id: number | null;
  market_data_quality_receipt_id: number | null;
  trigger: "manual" | "automation";
  phase: "PRECHECK" | "STARTED" | "TERMINAL" | "RECOVERY";
  outcome: "NOT_GENERATED" | "RUNNING" | "COMPLETED" | "FAILED" | "BLOCKED";
  reason_code: string;
  redacted_reason: string;
  requested_count: number;
  generated_count: number;
  validated_count: number;
  persisted_count: number;
  qualified_count: number;
  rejected_count: number;
  created_at: string;
};

export type CandidateLifecycleStatus =
  | "NOT_APPLICABLE_REJECTED"
  | "NOT_APPLICABLE_VALIDATION_FAILED"
  | "UNBRIDGED_REVALIDATION_REQUIRED"
  | "BRIDGED_PENDING_CANONICAL_VALIDATION"
  | "BRIDGED_PENDING_APPROVAL"
  | "BRIDGED_APPROVAL_REJECTED"
  | "APPROVED_NOT_DEPLOYED"
  | "DEPLOYED_ACTIVE_DEMO"
  | "DEPLOYED_DISABLED"
  | "UNKNOWN";

export type CandidateLifecycleRead = {
  candidate_id: number;
  batch_id: number;
  candidate_name: string;
  research_status: StrategyResearchCandidate["status"];
  lifecycle_status: CandidateLifecycleStatus;
  reason_code: string;
  source_code_digest: string;
  bridge_event_id: number | null;
  bridge_outcome: "REVALIDATION_REQUIRED" | "BRIDGED" | "FAILED" | null;
  bridge_contract_version: string | null;
  blueprint_digest: string | null;
  canonical_strategy_id: number | null;
  canonical_strategy_version_id: number | null;
  canonical_full_chain_run_id: number | null;
  candidate_approval_id: number | null;
  candidate_approval_status: "PENDING" | "APPROVED" | "REJECTED" | "EXPIRED" | "REVOKED" | null;
  deployment_id: number | null;
  deployment_status: "ACTIVE" | "DISABLED" | null;
  active_slot: number | null;
  created_at: string | null;
};

export type CandidateLifecycleSummary = {
  status:
    | "NOT_EVALUATED"
    | "NOT_QUEUED_NO_QUALIFIED"
    | "UNBRIDGED_REVALIDATION_REQUIRED"
    | "BRIDGED_PENDING_CANONICAL_VALIDATION"
    | "BRIDGED_PENDING_APPROVAL"
    | "APPROVED_NOT_DEPLOYED"
    | "DEPLOYED_ACTIVE_DEMO"
    | "MIXED"
    | "UNKNOWN";
  qualified_count: number;
  unbridged_count: number;
  pending_canonical_validation_count: number;
  pending_approval_count: number;
  approved_not_deployed_count: number;
  active_demo_count: number;
  unknown_count: number;
  reason_code: string;
};

export type StrategyResearchWorkspace = {
  schema_version: "formal-strategy-research-workspace-v1" | "formal-strategy-research-workspace-v2";
  as_of: string;
  source_type: "database";
  core_data: true;
  execution_target_id?: "OKX_DEMO";
  allow_real_funds?: false;
  real_orders?: false;
  evidence_status: "COMPLETE" | "PARTIAL";
  sections: {
    attempts: { status: "AVAILABLE" | "UNKNOWN"; reason_code: string | null };
    quality: { status: "AVAILABLE" | "UNKNOWN"; reason_code: string | null };
    batch: { status: "AVAILABLE" | "UNKNOWN"; reason_code: string | null };
    bridge?: { status: "AVAILABLE" | "UNKNOWN"; reason_code: string | null };
    approval?: { status: "AVAILABLE" | "UNKNOWN"; reason_code: string | null };
    deployment?: { status: "AVAILABLE" | "UNKNOWN"; reason_code: string | null };
  };
  attempts: Array<{
    attempt_id: string;
    latest_outcome: StrategyResearchAttemptEvent["outcome"];
    events: StrategyResearchAttemptEvent[];
  }>;
  latest_quality_receipt: null | {
    id: number;
    contract_version: string;
    exchange: string;
    pair: string;
    timeframe: string;
    file_format: string;
    inspected_at: string;
    row_count: number;
    first_open_at: string | null;
    last_open_at: string | null;
    expected_interval_seconds: number;
    missing_interval_count: number;
    duplicate_timestamp_count: number;
    out_of_order_count: number;
    misaligned_timestamp_count: number;
    null_ohlcv_count: number;
    invalid_ohlc_count: number;
    negative_volume_count: number;
    freshness_seconds: number | null;
    status: "PASSED" | "BLOCKED" | "FAILED";
    reason_codes: string[];
    created_at: string;
  };
  latest_batch: StrategyResearchBatch | null;
  handoff_status: FormalResearchRun["deployment_handoff_status"] | "UNKNOWN";
  candidate_lifecycles?: CandidateLifecycleRead[];
  lifecycle_summary?: CandidateLifecycleSummary;
};

export function fetchStrategyResearchBatches(signal?: AbortSignal) {
  return fetchJson<StrategyResearchBatch[]>("/strategy-research-batches?limit=20", signal);
}

export function fetchFormalResearchRun(signal?: AbortSignal) {
  return fetchJson<FormalResearchRun>("/strategy-research/formal-run", signal);
}

export function fetchStrategyResearchWorkspace(signal?: AbortSignal) {
  return fetchJson<StrategyResearchWorkspace>("/strategy-research/workspace?attempt_limit=10", signal);
}

export function startFormalResearchRun(signal?: AbortSignal) {
  return postJson<FormalResearchRun>("/strategy-research/formal-run", {}, { signal });
}
