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

export function deploymentHandoffText(batch: StrategyResearchBatch): string {
  return batch.qualified_count > 0 && validatedCandidateCount(batch) === batch.persisted_count
    ? "已由 QUALIFIED 持久化状态进入既有自动部署评审队列"
    : "未进入（本批次没有具备完整生命周期证据的 QUALIFIED 候选）";
}

export function canStartFormalResearch(run: FormalResearchRun | null, submitting: boolean): boolean {
  return !submitting && run?.status === "READY" && !run.active
    && hasOfficialAggressiveContract(run);
}
