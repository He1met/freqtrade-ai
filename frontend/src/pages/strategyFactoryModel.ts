import type { FormalResearchRun, StrategyResearchBatch } from "../api/strategyResearchApi";

export function hasOfficialAggressiveContract(run: FormalResearchRun | null): boolean {
  const contract = run?.quality_contract;
  return contract?.contract_version === "formal-strategy-research-aggressive-v1"
    && contract.risk_profile === "AGGRESSIVE"
    && contract.profile_label === "进攻型：最大回撤 15%"
    && contract.max_drawdown_per_validation_window === 0.15
    && contract.validation_requires_positive_net_profit === true
    && contract.lookahead_analysis_required === true
    && contract.fee_per_side === 0.0005
    && contract.slippage_per_side === 0.0002;
}

export function validatedCandidateCount(batch: StrategyResearchBatch): number {
  return batch.candidates.filter((candidate) => candidate.status !== "VALIDATION_FAILED").length;
}

export function deploymentHandoffText(run: FormalResearchRun | null): string {
  if (run?.deployment_handoff_status === "QUEUED_FOR_EXISTING_AUTOMATION") {
    return "协调器已交接给既有自动部署评审";
  }
  if (run?.deployment_handoff_status === "NOT_QUEUED_NO_QUALIFIED") {
    return "未交接：本批次没有 QUALIFIED 候选";
  }
  return "未知：尚无权威部署交接状态";
}

export function canStartFormalResearch(run: FormalResearchRun | null, submitting: boolean): boolean {
  return !submitting && run?.status === "READY" && !run.active
    && hasOfficialAggressiveContract(run);
}
