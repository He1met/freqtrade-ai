import { fetchJson, postJson } from "./http";

/** Minimum read-only contract for the lease-protected serial candidate queue.
 * Legacy batch data must never be used to infer active work, lease state or progress.
 */
export type CandidateResearchQueueStatus =
  | "PENDING" | "CLAIMED" | "RUNNING" | "VALIDATED"
  | "REJECTED" | "FAILED" | "QUALIFIED_PENDING_DEPLOYMENT"
  | "DEPLOYING" | "DEPLOYED";

export type CandidateResearchQueueEvidence = {
  label: string;
  href: string | null;
  reference: string;
};

export type CandidateResearchQueueItem = {
  candidate_id: string;
  candidate_name: string;
  pair: string | null;
  timeframe: string | null;
  generated_at: string | null;
  queue_position: number | null;
  status: CandidateResearchQueueStatus;
  current_step: string | null;
  completed_steps: string[];
  next_step: string | null;
  progress_percent: number | null;
  started_at: string | null;
  completed_at: string | null;
  elapsed_seconds: number | null;
  preceding_count: number | null;
  attempt: number | null;
  reason_code: string | null;
  reason_message: string | null;
  evidence: CandidateResearchQueueEvidence[];
  actions: {
    cancel_available: false;
    retry_available: false;
    reason_code: string;
  };
};

export type CandidateResearchQueueRead = {
  schema_version: "formal-candidate-validation-queue-read-v1";
  as_of: string;
  availability: "AVAILABLE";
  serial_execution: true;
  batch: {
    run_id: string;
    expected_count: 60;
    generation_status: "NOT_GENERATED" | "GENERATING" | "GENERATED";
    generated_count: number;
    enqueued_count: number;
    active_count: 0 | 1;
    waiting_count: number;
    completed_count: number;
    remaining_count: number;
  };
  health: {
    status: "HEALTHY" | "IDLE" | "BLOCKED" | "STALE" | "UNKNOWN";
    reason_code: string;
    lease_owner_present: boolean;
    lease_expires_at: string | null;
  };
  active_candidate: CandidateResearchQueueItem | null;
  waiting_candidates: CandidateResearchQueueItem[];
  completed_candidates: CandidateResearchQueueItem[];
};

export function fetchCandidateResearchQueue(signal?: AbortSignal) {
  return fetchJson<CandidateResearchQueueRead>(
    "/strategy-research/candidate-validation-queue",
    signal,
  );
}

export type BihourlyResearchTriggerResult = {
  schema_version: "bihourly-strategy-research-trigger-v1";
  status: "GENERATED" | "NO_OP" | "FAILED";
  reason_code: string;
  trigger: "manual" | "automation";
  run_id: string;
  persisted_count: number;
  runtime_status: string;
  opening_guard: "RUNNING" | "BLOCKED";
  generation_only: true;
  serial_consumer_separate: true;
  backtest_started: false;
  deployment_started: false;
  signal_or_order_started: false;
  real_orders: false;
  allow_real_funds: false;
  exchange_access: "PUBLIC_MARKET_DATA_ONLY";
};

export function triggerBihourlyResearchGeneration(operatorToken: string, signal?: AbortSignal) {
  return postJson<BihourlyResearchTriggerResult>(
    "/strategy-research/bihourly-generation",
    {},
    {
      idempotencyKey: `bihourly-manual-${crypto.randomUUID()}`,
      operatorToken,
      signal,
    },
  );
}
