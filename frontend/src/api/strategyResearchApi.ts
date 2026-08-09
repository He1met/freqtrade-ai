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
    | "QUEUED_FOR_EXISTING_AUTOMATION";
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

export function fetchStrategyResearchBatches(signal?: AbortSignal) {
  return fetchJson<StrategyResearchBatch[]>("/strategy-research-batches?limit=20", signal);
}

export function fetchFormalResearchRun(signal?: AbortSignal) {
  return fetchJson<FormalResearchRun>("/strategy-research/formal-run", signal);
}

export function startFormalResearchRun(signal?: AbortSignal) {
  return postJson<FormalResearchRun>("/strategy-research/formal-run", {}, { signal });
}
