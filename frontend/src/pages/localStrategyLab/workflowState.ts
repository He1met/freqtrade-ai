import type {
  AcceptanceState,
  DataSource,
  LocalStrategyLabEvidenceStage,
  MvpData,
} from "../../api/types";

export type LabPhase = "generation" | "backtest" | "score" | "dry-run";
export type LabStageState = AcceptanceState;
export type LabPhaseProgress = "completed" | "current" | "locked";

export type LabWorkflowStage = {
  id: LabPhase;
  label: string;
  state: LabStageState;
  progress: LabPhaseProgress;
  reason: string;
  nextAction: string;
};

export type LabWorkflowModel = {
  currentPhase: LabPhase;
  stages: LabWorkflowStage[];
};

const labels: Record<LabPhase, string> = {
  generation: "策略生成",
  backtest: "回测验证",
  score: "评分选择",
  "dry-run": "受控 Dry-run",
};

function missingStage(
  id: LabPhase,
  reason: string,
  nextAction: string,
): Omit<LabWorkflowStage, "progress"> {
  return { id, label: labels[id], state: "NOT_RUN", reason, nextAction };
}

function evidenceStage(
  id: LabPhase,
  stage: LocalStrategyLabEvidenceStage | undefined,
): Omit<LabWorkflowStage, "progress"> {
  return stage
    ? {
        id,
        label: labels[id],
        state: stage.state,
        reason: stage.reason,
        nextAction: stage.nextAction,
      }
    : missingStage(id, "尚无可核对的 API/DB 阶段证据。", "刷新真实数据并从前一阶段开始处理。");
}

function generationStage(
  stages: LocalStrategyLabEvidenceStage[],
): Omit<LabWorkflowStage, "progress"> {
  const generation = stages.find((stage) => stage.key === "generation");
  const strategyFile = stages.find((stage) => stage.key === "strategy_file");
  const incomplete = [generation, strategyFile].find((stage) => !stage?.canAccept);
  if (incomplete) {
    return evidenceStage("generation", incomplete);
  }
  if (!generation || !strategyFile) {
    return missingStage(
      "generation",
      "策略生成或策略文件证据不完整。",
      "核对 generation run、strategy version、database IDs 与策略文件。",
    );
  }
  return {
    id: "generation",
    label: labels.generation,
    state: "ACCEPTABLE",
    reason: "策略生成与策略文件均有可验收的 API/DB 证据。",
    nextAction: "进入回测验证，使用已持久化的 strategy version。",
  };
}

function dryRunStage(
  data: MvpData,
  source: DataSource | undefined,
): Omit<LabWorkflowStage, "progress"> {
  const runtime = data.operatorDashboard.runtimeContract.dryRunReadiness;
  const manifest = data.dryRun.manifest;
  const snapshot = data.dryRun.snapshot;
  const normalizedStatus = snapshot.status.trim().toUpperCase();
  const normalizedManifestStatus = manifest?.status.trim().toUpperCase();
  const hasMatchingPersistentIdentity =
    snapshot.strategyVersionId !== null &&
    Boolean(snapshot.artifactManifestPath?.trim()) &&
    manifest?.strategyVersionId === snapshot.strategyVersionId &&
    Boolean(manifest.manifestPath?.trim()) &&
    manifest.manifestPath === snapshot.artifactManifestPath &&
    (normalizedManifestStatus === "SUCCESS" || normalizedManifestStatus === "RUNNING");
  if (
    source === "api" &&
    snapshot.dryRun === true &&
    normalizedStatus === "RUNNING" &&
    hasMatchingPersistentIdentity
  ) {
    return {
      id: "dry-run",
      label: labels["dry-run"],
      state: "ACCEPTABLE",
      reason: "真实 API 返回的持久 manifest 与 snapshot 身份一致，受控 Dry-run 正在运行且 dry_run=true。",
      nextAction: "继续监控持久 snapshot；本阶段始终禁止 live trading 和真实订单。",
    };
  }
  if (source !== "api") {
    return {
      id: "dry-run",
      label: labels["dry-run"],
      state: source === "fixture" ? "NOT_ACCEPTABLE" : "API_GAP",
      reason:
        source === "fixture"
          ? "Dry-run 数据来自 fixture，不能作为真实运行证据。"
          : "缺少真实 Dry-run API 数据，不能确认受控运行状态。",
      nextAction: "恢复真实 Dry-run API/DB 数据并刷新；不得用 fixture 或本地操作记录推进阶段。",
    };
  }
  if (snapshot.dryRun === true && normalizedStatus === "RUNNING" && !hasMatchingPersistentIdentity) {
    return {
      id: "dry-run",
      label: labels["dry-run"],
      state: "API_GAP",
      reason: "Dry-run snapshot 显示 RUNNING，但 manifest 未成功或缺少、不匹配持久 strategy version / path。",
      nextAction: "补齐并核对真实 API 返回的成功 manifest、strategy_version_id 与 artifact manifest path。",
    };
  }
  const runtimeState = runtime.status.trim().toUpperCase();
  const state: AcceptanceState =
    runtimeState === "FAILED" ||
    runtimeState === "BLOCKED" ||
    runtimeState === "API_GAP" ||
    runtimeState === "NOT_ACCEPTABLE" ||
    runtimeState === "NOT_RUN"
      ? runtimeState
      : runtimeState === "UNAVAILABLE"
        ? "API_GAP"
        : "NOT_RUN";
  return {
    id: "dry-run",
    label: labels["dry-run"],
    state,
    reason:
      snapshot.blockedReason ??
      snapshot.failedReason ??
      runtime.blockedReason ??
      runtime.unavailableReason ??
      runtime.staleReason ??
      runtime.summary,
    nextAction:
      runtime.status === "READY"
        ? "检查 readiness、人工批准与 dry_run=true 后，才可启动受控 Dry-run。"
        : "先处理 Dry-run readiness 的阻断原因；不得切换到 live trading。",
  };
}

export function deriveLabWorkflow(
  data: MvpData,
  options: {
    dryRunSource?: DataSource;
    isLoading?: boolean;
    error?: string | null;
  } = {},
): LabWorkflowModel {
  const evidence = data.localStrategyLabEvidence?.stages ?? [];
  const rawStages: Array<Omit<LabWorkflowStage, "progress">> = [
    generationStage(evidence),
    evidenceStage("backtest", evidence.find((stage) => stage.key === "backtest")),
    evidenceStage("score", evidence.find((stage) => stage.key === "score")),
    dryRunStage(data, options.dryRunSource),
  ];

  if (options.isLoading) {
    rawStages[0] = missingStage("generation", "正在加载真实 API/DB 证据。", "等待数据加载完成。");
  } else if (options.error) {
    rawStages[0] = {
      ...missingStage("generation", options.error, "恢复 Backend API 后刷新真实数据。"),
      state: "API_GAP",
    };
  }

  const firstIncomplete = rawStages.findIndex((stage) => stage.state !== "ACCEPTABLE");
  const currentIndex = firstIncomplete === -1 ? rawStages.length - 1 : firstIncomplete;
  const stages = rawStages.map((stage, index): LabWorkflowStage => ({
    ...stage,
    progress:
      index < currentIndex
        ? "completed"
        : index === currentIndex
          ? "current"
          : "locked",
  }));
  return { currentPhase: stages[currentIndex].id, stages };
}
