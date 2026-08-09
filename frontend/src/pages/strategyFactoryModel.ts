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

export function hasOfficialSafetyContract(run: FormalResearchRun | null): boolean {
  const safety = run?.safety;
  return safety?.execution_target === "OKX_DEMO"
    && safety.allow_real_funds === false
    && safety.real_orders === false
    && safety.credentials_collected === false
    && safety.dry_run_trading_authorized === false
    && safety.grant_authorized === false
    && safety.manual_order_authorized === false;
}

export function validatedCandidateCount(batch: StrategyResearchBatch): number {
  return batch.candidates.filter((candidate) => candidate.status !== "VALIDATION_FAILED").length;
}

export function deploymentHandoffText(run: FormalResearchRun | null): string {
  if (run?.deployment_handoff_status === "CANONICAL_LINK_UNAVAILABLE") {
    return "已有 QUALIFIED 候选，但正式生命周期衔接证据尚不可用";
  }
  if (run?.deployment_handoff_status === "NOT_QUEUED_NO_QUALIFIED") {
    return "未交接：本批次没有 QUALIFIED 候选";
  }
  return "未知：尚无权威部署交接状态";
}

export function canStartFormalResearch(run: FormalResearchRun | null, submitting: boolean): boolean {
  return !submitting && run?.status === "READY" && !run.active
    && hasOfficialAggressiveContract(run) && hasOfficialSafetyContract(run);
}
