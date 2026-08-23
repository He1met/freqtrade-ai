import type { ResearchChainProjection, StrategyProjection } from "../../api/canonicalV13Types";
import { canonicalStatusGuidance, canonicalStatusPresentation } from "./canonicalV13Model.ts";

export type CanonicalResearchStepId = "intake" | "validation" | "plan" | "attempt" | "qualification";
export type CanonicalResearchStepState = "complete" | "current" | "not-started" | "blocked" | "unknown";

export type CanonicalResearchWorkflowStep = {
  apiStatus: string | null;
  id: CanonicalResearchStepId;
  label: string;
  nextAction: { label: string; to: string } | null;
  reasonCodes: readonly string[];
  state: CanonicalResearchStepState;
  summary: string;
};

export type CanonicalResearchWorkflow = {
  currentStepId: CanonicalResearchStepId | null;
  nextAction: { label: string; to: string };
  researchLink: { label: string; to: string };
  steps: readonly CanonicalResearchWorkflowStep[];
};

type WorkflowInput = {
  chain: ResearchChainProjection | null;
  links: { researchHref: string; strategyHref: string };
  selection: { planId: string | null; strategyId: string; targetId: string | null };
  strategy: StrategyProjection | null;
};

const STEP_LABELS: Readonly<Record<CanonicalResearchStepId, string>> = {
  intake: "策略入库",
  validation: "入库代码验证",
  plan: "研究计划",
  attempt: "回测执行",
  qualification: "OOS 资格决策",
};

function step(
  id: CanonicalResearchStepId,
  state: CanonicalResearchStepState,
  apiStatus: string | null,
  summary: string,
  nextAction: CanonicalResearchWorkflowStep["nextAction"],
  reasonCodes: readonly string[] = [],
): CanonicalResearchWorkflowStep {
  return { apiStatus, id, label: STEP_LABELS[id], nextAction, reasonCodes, state, summary };
}

function statusSummary(status: string): string {
  return canonicalStatusGuidance(status).explanation;
}

function unknownSteps(
  reasonCode: string,
  researchHref: string,
  qualificationStatus: string | null = null,
): CanonicalResearchWorkflowStep[] {
  return [
    step("plan", "unknown", null, "缺少可验证的精确研究计划上下文，步骤不会被点亮。", { label: "进入研究状态", to: researchHref }, [reasonCode]),
    step("attempt", "unknown", null, "没有 exact validation plan lineage，不能判断回测是否开始或完成。", { label: "查看研究上下文", to: researchHref }, [reasonCode]),
    step(
      "qualification",
      "unknown",
      qualificationStatus,
      qualificationStatus
        ? `策略 projection 返回 ${qualificationStatus}，但缺少 exact plan lineage，步骤保持未知。`
        : "缺少 exact plan lineage，不能判断资格决策阶段。",
      { label: "查看研究上下文", to: researchHref },
      [reasonCode],
    ),
  ];
}

function intakeStep(strategy: StrategyProjection, strategyHref: string): CanonicalResearchWorkflowStep {
  const status = strategy.intake_status;
  if (!canonicalStatusPresentation(status).known) {
    return step("intake", "unknown", status, "API 返回未知 intake 状态；后续步骤保持未知。", { label: "查看策略诊断", to: strategyHref }, ["UNKNOWN_CONTRACT_VALUE"]);
  }
  if (status === "INTAKE_ACCEPTED") {
    return step("intake", "complete", status, statusSummary(status), null);
  }
  return step("intake", "blocked", status, statusSummary(status), { label: "检查策略入库", to: "/v13/submission" });
}

function validationStep(strategy: StrategyProjection, strategyHref: string): CanonicalResearchWorkflowStep {
  const status = strategy.validation_status;
  if (!canonicalStatusPresentation(status).known) {
    return step("validation", "unknown", status, "API 返回未知 validation 状态；研究步骤保持未知。", { label: "查看策略诊断", to: strategyHref }, ["UNKNOWN_CONTRACT_VALUE"]);
  }
  if (status === "VALIDATED") return step("validation", "complete", status, statusSummary(status), null);
  if (status === "UNVALIDATED" || status === "VALIDATING") {
    return step("validation", "current", status, statusSummary(status), { label: "查看策略验证", to: strategyHref });
  }
  return step("validation", "blocked", status, statusSummary(status), { label: "查看验证阻断", to: strategyHref });
}

function planStep(status: ResearchChainProjection["plan_status"], researchHref: string): CanonicalResearchWorkflowStep {
  if (!canonicalStatusPresentation(status).known) {
    return step("plan", "unknown", status, "API 返回未知 research plan 状态。", { label: "查看研究诊断", to: researchHref }, ["UNKNOWN_CONTRACT_VALUE"]);
  }
  if (status === "DECLARED") return step("plan", "current", status, statusSummary(status), { label: "查看研究计划", to: researchHref });
  if (status === "READY" || status === "RUNNING" || status === "COMPLETE") return step("plan", "complete", status, statusSummary(status), null);
  return step("plan", "blocked", status, statusSummary(status), { label: "查看计划阻断", to: researchHref });
}

function attemptStep(status: ResearchChainProjection["attempt_status"], researchHref: string): CanonicalResearchWorkflowStep {
  if (status === null) {
    return step("attempt", "not-started", null, "API 对 exact plan 明确返回空 attempt，当前没有回测执行事实。", { label: "查看研究计划", to: researchHref });
  }
  if (!canonicalStatusPresentation(status).known) {
    return step("attempt", "unknown", status, "API 返回未知 validation attempt 状态。", { label: "查看研究诊断", to: researchHref }, ["UNKNOWN_CONTRACT_VALUE"]);
  }
  if (status === "SUCCEEDED") return step("attempt", "complete", status, statusSummary(status), null);
  if (status === "PENDING" || status === "RUNNING") return step("attempt", "current", status, statusSummary(status), { label: "查看回测执行", to: researchHref });
  return step("attempt", "blocked", status, statusSummary(status), { label: "查看回测阻断", to: researchHref });
}

function qualificationStep(
  status: ResearchChainProjection["qualification_status"],
  reasonCode: string | null,
  researchHref: string,
  attemptState: CanonicalResearchStepState,
): CanonicalResearchWorkflowStep {
  if (status === null) {
    return attemptState === "complete"
      ? step("qualification", "current", null, "回测已由 API 明确标记完成，exact plan 尚无 qualification decision。", { label: "查看资格决策", to: researchHref })
      : step("qualification", "not-started", null, "API 对 exact plan 明确返回空 qualification decision。", { label: "查看研究状态", to: researchHref });
  }
  if (!canonicalStatusPresentation(status).known) {
    return step("qualification", "unknown", status, "API 返回未知 qualification 状态。", { label: "查看研究诊断", to: researchHref }, ["UNKNOWN_CONTRACT_VALUE"]);
  }
  if (status === "QUALIFIED") return step("qualification", "complete", status, statusSummary(status), null, reasonCode ? [reasonCode] : []);
  return step("qualification", "blocked", status, statusSummary(status), { label: "查看资格阻断", to: researchHref }, reasonCode ? [reasonCode] : []);
}

function chainConflicts(chain: ResearchChainProjection): boolean {
  const attemptStarted = chain.attempt_status !== null;
  const qualificationStarted = chain.qualification_status !== null;
  const attemptIdentityConflict = attemptStarted !== (chain.validation_attempt_id !== null)
    || (!attemptStarted && chain.attempt_receipt_digest !== null);
  const qualificationIdentityConflict = qualificationStarted !== (chain.qualification_decision_id !== null)
    || qualificationStarted !== (chain.qualification_decision_digest !== null)
    || (!qualificationStarted && chain.qualification_reason_code !== null);
  const qualificationEvidenceConflict = qualificationStarted
    && (chain.target_score_id === null || chain.score_digest === null);
  return (
    attemptIdentityConflict
    || qualificationIdentityConflict
    || qualificationEvidenceConflict
    || ((chain.plan_status === "DECLARED" || chain.plan_status === "FAILED" || chain.plan_status === "BLOCKED") && attemptStarted)
    || (qualificationStarted && chain.attempt_status !== "SUCCEEDED")
  );
}

export function canonicalResearchWorkflow({ chain, links, selection, strategy }: WorkflowInput): CanonicalResearchWorkflow {
  const researchLink = { label: "进入研究状态", to: links.researchHref };
  if (!strategy) {
    const steps = (["intake", "validation", "plan", "attempt", "qualification"] as const).map((id) =>
      step(id, "unknown", null, "未取得所选策略的 Canonical API projection。", { label: "返回策略目录", to: "/v13/strategies" }, ["RESEARCH_STRATEGY_UNAVAILABLE"]));
    return { currentStepId: "intake", nextAction: steps[0].nextAction as { label: string; to: string }, researchLink, steps };
  }
  if (strategy.strategy_id !== selection.strategyId) {
    const steps = (["intake", "validation", "plan", "attempt", "qualification"] as const).map((id) =>
      step(id, "unknown", null, "API 返回的策略身份与 committed URL selection 不一致。", { label: "返回策略目录", to: "/v13/strategies" }, ["RESEARCH_CONTEXT_CONFLICT"]));
    return { currentStepId: "intake", nextAction: steps[0].nextAction as { label: string; to: string }, researchLink, steps };
  }

  const intake = intakeStep(strategy, links.strategyHref);
  let validation = validationStep(strategy, links.strategyHref);
  if (intake.state !== "complete" && (validation.state === "complete" || validation.state === "current")) {
    validation = step("validation", "unknown", strategy.validation_status, "Intake 与 validation 顺序发生冲突；页面不点亮该步骤。", { label: "查看策略诊断", to: links.strategyHref }, ["RESEARCH_CONTEXT_CONFLICT"]);
  }

  let researchSteps: CanonicalResearchWorkflowStep[];
  if (intake.state !== "complete" || validation.state === "unknown") {
    researchSteps = unknownSteps("RESEARCH_UPSTREAM_INCOMPLETE", links.researchHref, strategy.qualification_status);
  } else if (!chain) {
    researchSteps = unknownSteps("RESEARCH_CONTEXT_UNSELECTED", links.researchHref, strategy.qualification_status);
  } else if (
    (selection.planId !== null && chain.validation_plan_id !== selection.planId)
    || (selection.targetId !== null && chain.research_target_id !== selection.targetId)
  ) {
    researchSteps = unknownSteps("RESEARCH_CONTEXT_CONFLICT", links.researchHref, chain.qualification_status);
  } else if (chain.strategy_version_id !== strategy.current_version_id) {
    researchSteps = unknownSteps("RESEARCH_STRATEGY_LINEAGE_MISMATCH", links.researchHref, chain.qualification_status);
  } else if (chainConflicts(chain)) {
    researchSteps = unknownSteps("RESEARCH_CONTEXT_CONFLICT", links.researchHref, chain.qualification_status);
  } else {
    const plan = planStep(chain.plan_status, links.researchHref);
    if (plan.state === "unknown") {
      researchSteps = [plan, ...unknownSteps("RESEARCH_UPSTREAM_INCOMPLETE", links.researchHref, chain.qualification_status).slice(1)];
    } else {
      const attempt = attemptStep(chain.attempt_status, links.researchHref);
      const qualification = attempt.state === "unknown"
        ? unknownSteps("RESEARCH_UPSTREAM_INCOMPLETE", links.researchHref, chain.qualification_status)[2]
        : qualificationStep(chain.qualification_status, chain.qualification_reason_code, links.researchHref, attempt.state);
      researchSteps = [plan, attempt, qualification];
    }
  }

  const steps = [intake, validation, ...researchSteps];
  const active = steps.find((item) => item.state === "blocked")
    ?? steps.find((item) => item.state === "current")
    ?? steps.find((item) => item.state === "unknown")
    ?? steps.find((item) => item.state === "not-started")
    ?? null;
  const nextAction = active?.nextAction ?? { label: "查看优化状态", to: "/v13/optimization" };
  return { currentStepId: active?.id ?? null, nextAction, researchLink, steps };
}
