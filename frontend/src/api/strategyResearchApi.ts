import { fetchJson } from "./http";

export type StrategyResearchRejectionReason = {
  code: string;
  message: string;
  evidence: Record<string, unknown>;
};

export type StrategyResearchCandidate = {
  id: number;
  batch_id: number;
  candidate_name: string;
  status: "QUALIFIED" | "REJECTED" | "VALIDATION_FAILED";
  score: number | null;
  rejection_reasons: StrategyResearchRejectionReason[];
};

export type StrategyResearchBatch = {
  id: number;
  run_id: string;
  status: "GENERATED" | "VALIDATED" | "FAILED";
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
  candidates: StrategyResearchCandidate[];
};

export function fetchStrategyResearchBatches(signal?: AbortSignal) {
  return fetchJson<StrategyResearchBatch[]>("/strategy-research-batches?limit=20", signal);
}
