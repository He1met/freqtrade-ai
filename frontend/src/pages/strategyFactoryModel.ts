import type {
  CandidateLifecycleRead,
  CandidateLifecycleSummary,
  CandidateLifecycleStatus,
  FormalResearchRun,
  StrategyResearchBatch,
  StrategyResearchWorkspace,
} from "../api/strategyResearchApi";

const lifecycleStatuses = new Set<CandidateLifecycleStatus>([
  "NOT_APPLICABLE_REJECTED",
  "NOT_APPLICABLE_VALIDATION_FAILED",
  "UNBRIDGED_REVALIDATION_REQUIRED",
  "BRIDGED_PENDING_CANONICAL_VALIDATION",
  "BRIDGED_PENDING_APPROVAL",
  "BRIDGED_APPROVAL_REJECTED",
  "APPROVED_NOT_DEPLOYED",
  "DEPLOYED_ACTIVE_DEMO",
  "DEPLOYED_DISABLED",
  "UNKNOWN",
]);

export type LifecycleStepState = "COMPLETE" | "CURRENT" | "BLOCKED" | "UNKNOWN" | "NOT_APPLICABLE";

export type LifecycleDisplay = {
  label: string;
  detail: string;
  status: CandidateLifecycleStatus;
  steps: [LifecycleStepState, LifecycleStepState, LifecycleStepState, LifecycleStepState];
};

export function strictCandidateLifecycleStatus(value: unknown): CandidateLifecycleStatus {
  return typeof value === "string" && lifecycleStatuses.has(value as CandidateLifecycleStatus)
    ? value as CandidateLifecycleStatus
    : "UNKNOWN";
}

export function candidateLifecycleDisplay(value: unknown): LifecycleDisplay {
  const status = strictCandidateLifecycleStatus(value);
  const displays: Record<CandidateLifecycleStatus, Omit<LifecycleDisplay, "status">> = {
    NOT_APPLICABLE_REJECTED: {
      label: "质量门拒绝",
      detail: "候选未通过质量门，不进入 canonical bridge。",
      steps: ["BLOCKED", "NOT_APPLICABLE", "NOT_APPLICABLE", "NOT_APPLICABLE"],
    },
    NOT_APPLICABLE_VALIDATION_FAILED: {
      label: "验证失败",
      detail: "候选验证未完成，不进入 canonical bridge。",
      steps: ["BLOCKED", "NOT_APPLICABLE", "NOT_APPLICABLE", "NOT_APPLICABLE"],
    },
    UNBRIDGED_REVALIDATION_REQUIRED: {
      label: "需补充 Blueprint v2 证据",
      detail: "尚未建立确定性等价 bridge；不能提升为 canonical 策略。",
      steps: ["COMPLETE", "BLOCKED", "NOT_APPLICABLE", "NOT_APPLICABLE"],
    },
    BRIDGED_PENDING_CANONICAL_VALIDATION: {
      label: "已桥接，待 canonical 验证",
      detail: "仅确认 Blueprint v2 等价与 canonical 身份；尚未进入批准或部署。",
      steps: ["COMPLETE", "COMPLETE", "CURRENT", "NOT_APPLICABLE"],
    },
    BRIDGED_PENDING_APPROVAL: {
      label: "已桥接，待批准",
      detail: "canonical 验证已完成，尚无明确批准证据。",
      steps: ["COMPLETE", "COMPLETE", "CURRENT", "NOT_APPLICABLE"],
    },
    BRIDGED_APPROVAL_REJECTED: {
      label: "批准未通过",
      detail: "Bridge 证据保留，但批准已拒绝、过期或撤销；不得部署。",
      steps: ["COMPLETE", "COMPLETE", "BLOCKED", "NOT_APPLICABLE"],
    },
    APPROVED_NOT_DEPLOYED: {
      label: "已批准，未部署",
      detail: "已有明确批准证据，尚无 OKX_DEMO ACTIVE 部署。",
      steps: ["COMPLETE", "COMPLETE", "COMPLETE", "CURRENT"],
    },
    DEPLOYED_ACTIVE_DEMO: {
      label: "Demo 运行中",
      detail: "权威投影确认 OKX_DEMO ACTIVE 部署。",
      steps: ["COMPLETE", "COMPLETE", "COMPLETE", "COMPLETE"],
    },
    DEPLOYED_DISABLED: {
      label: "Demo 部署已停用",
      detail: "权威投影确认部署记录存在但当前不是 ACTIVE。",
      steps: ["COMPLETE", "COMPLETE", "COMPLETE", "BLOCKED"],
    },
    UNKNOWN: {
      label: "生命周期未知",
      detail: "权威 bridge 投影缺失或不可用；页面不从候选状态推断。",
      steps: ["UNKNOWN", "UNKNOWN", "UNKNOWN", "UNKNOWN"],
    },
  };
  return { status, ...displays[status] };
}

export function candidateLifecycleFor(
  workspace: StrategyResearchWorkspace | null | undefined,
  candidateId: number,
): CandidateLifecycleRead | null {
  if (workspace?.sections?.bridge?.status !== "AVAILABLE") return null;
  return workspace.candidate_lifecycles?.find((item) => item.candidate_id === candidateId) ?? null;
}

export function lifecycleSummaryLabel(
  summary: CandidateLifecycleSummary | null | undefined,
): string {
  if (!summary) return "未知";
  return {
    NOT_EVALUATED: "尚未评估",
    NOT_QUEUED_NO_QUALIFIED: "无合格候选",
    UNBRIDGED_REVALIDATION_REQUIRED: "需补充 Blueprint v2 证据",
    BRIDGED_PENDING_CANONICAL_VALIDATION: "已桥接，待 canonical 验证",
    BRIDGED_PENDING_APPROVAL: "已桥接，待人工审批",
    APPROVED_NOT_DEPLOYED: "已批准，未部署",
    DEPLOYED_ACTIVE_DEMO: "Demo 运行中",
    MIXED: "候选处于多个阶段",
    UNKNOWN: "未知",
  }[summary.status];
}

export function lifecycleSummaryText(
  summary: CandidateLifecycleSummary | null | undefined,
  projectionAvailable: boolean,
): string {
  if (!projectionAvailable || !summary) {
    return "生命周期未知：权威 candidate → canonical 投影不可用";
  }
  return {
    NOT_EVALUATED: "尚未评估 candidate → canonical 生命周期",
    NOT_QUEUED_NO_QUALIFIED: "未衔接：本批次没有 QUALIFIED 候选",
    UNBRIDGED_REVALIDATION_REQUIRED: `待补证：${summary.unbridged_count} 个候选需要 Blueprint v2 等价复验`,
    BRIDGED_PENDING_CANONICAL_VALIDATION: `已 bridge：${summary.pending_canonical_validation_count} 个候选等待 canonical 验证`,
    BRIDGED_PENDING_APPROVAL: `待审批：${summary.pending_approval_count} 个候选已有 bridge 证据`,
    APPROVED_NOT_DEPLOYED: `已批准未部署：${summary.approved_not_deployed_count} 个候选`,
    DEPLOYED_ACTIVE_DEMO: `Demo 运行中：${summary.active_demo_count} 个候选具有完整映射`,
    MIXED: "候选处于多个正式生命周期阶段，请查看逐项证据",
    UNKNOWN: "生命周期未知：投影未能给出可信结论",
  }[summary.status];
}

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

export function researchQualityContractText(run: FormalResearchRun | null): string {
  const contract = run?.quality_contract;
  if (!contract) return "质量契约尚未读取";
  const drawdown = contract.max_drawdown_per_validation_window;
  const drawdownText = typeof drawdown === "number"
    ? `${(drawdown * 100).toFixed(drawdown * 100 % 1 === 0 ? 0 : 2)}%`
    : "未知";
  const label = contract.profile_label ?? `历史批次契约：最大回撤 ${drawdownText}`;
  const evidence = [
    contract.validation_requires_positive_net_profit === true ? "独立窗口成本后净收益为正" : null,
    contract.lookahead_analysis_required === true ? "要求 lookahead 检查" : null,
    typeof contract.fee_per_side === "number" ? `费用 ${(contract.fee_per_side * 100).toFixed(2)}%/侧` : null,
    typeof contract.slippage_per_side === "number" ? `滑点 ${(contract.slippage_per_side * 100).toFixed(2)}%/侧` : null,
    `最大回撤门 ${drawdownText}`,
  ].filter((item): item is string => item !== null);
  return `${label}；${evidence.join("、")}。契约校验：${hasOfficialAggressiveContract(run) ? "匹配当前 official contract" : "历史或不完整契约，不得自动部署"}。`;
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
