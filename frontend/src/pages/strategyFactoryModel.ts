import type { FormalResearchRun, StrategyResearchBatch } from "../api/strategyResearchApi";

export function validatedCandidateCount(batch: StrategyResearchBatch): number {
  return batch.candidates.filter((candidate) => candidate.status !== "VALIDATION_FAILED").length;
}

export function deploymentHandoffText(batch: StrategyResearchBatch): string {
  return batch.qualified_count > 0 && validatedCandidateCount(batch) === batch.persisted_count
    ? "已由 QUALIFIED 持久化状态进入既有自动部署评审队列"
    : "未进入（本批次没有具备完整生命周期证据的 QUALIFIED 候选）";
}

export function canStartFormalResearch(run: FormalResearchRun | null, submitting: boolean): boolean {
  return !submitting && run?.status === "READY" && !run.active;
}
