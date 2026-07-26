import type {
  DataSource,
  DryRunArtifactManifest,
  DryRunReadinessReport,
  DryRunStatusSnapshot,
  MvpData,
  RuntimeStatusSummary,
} from "../../api/types";
import {
  isCurrentCoreVersion,
  type LabSelection,
} from "./candidateWorkbenchModel.ts";

export type DryRunDecisionState =
  | "NOT_CHECKED"
  | "CHECKING"
  | "READY"
  | "BLOCKED"
  | "STARTING"
  | "RUNNING"
  | "STOPPING"
  | "STOPPED"
  | "FAILED";

export type DryRunDecisionAction = "check" | "refresh" | "start" | "stop" | null;

export type DryRunCandidate = {
  strategyVersionId: string;
  strategyName: string | null;
};

export type DryRunTransientState =
  | { kind: "idle" }
  | { kind: "checking" }
  | { kind: "starting" }
  | { kind: "stopping" }
  | { kind: "reconcile-blocked"; operation: "start" | "stop"; reason: string }
  | { kind: "failed"; reason: string };

export type CandidateScopedValue<T> = {
  strategyVersionId: string | null;
  value: T | null;
};

export type CandidateIdentity = {
  strategyVersionId: string | null;
  epoch: number;
};

export type DryRunDecisionModel = {
  state: DryRunDecisionState;
  blocker: string | null;
  conclusion: string;
  nextAction: string;
  action: DryRunDecisionAction;
  persistedRunning: boolean;
  safetyVerified: boolean;
};

export function reconcileCandidateScopedValue<T>(
  current: CandidateScopedValue<T>,
  strategyVersionId: string | null,
): CandidateScopedValue<T> {
  return current.strategyVersionId === strategyVersionId
    ? current
    : { strategyVersionId, value: null };
}

export function candidateIdentityMatches(
  current: CandidateIdentity,
  expected: CandidateIdentity,
): boolean {
  return current.strategyVersionId === expected.strategyVersionId
    && current.epoch === expected.epoch;
}

export function inactiveActionLabel(state: DryRunDecisionState): string {
  if (state === "CHECKING") return "检查中";
  if (state === "STARTING") return "启动中";
  if (state === "STOPPING") return "停止中";
  return "暂无可执行动作";
}

function normalized(value: string | null | undefined): string {
  return value?.trim().toUpperCase() ?? "";
}

export function readinessReason({
  status,
  summary,
  blockedReason,
  unavailableReason,
  staleReason,
}: RuntimeStatusSummary): string {
  if (normalized(status) === "READY") return summary || "Dry-run readiness 持久证据已通过。";
  return blockedReason
    ?? unavailableReason
    ?? staleReason
    ?? summary
    ?? "Dry-run readiness 未提供可验收结论。";
}

function sameId(left: string | number | null | undefined, right: string | number | null | undefined): boolean {
  return left !== null && left !== undefined && right !== null && right !== undefined && String(left) === String(right);
}

export function deriveDryRunCandidate(data: MvpData, selection: LabSelection): DryRunCandidate | null {
  if (!selection.strategyVersionId) return null;
  const strategyById = new Map(data.strategies.map((strategy) => [strategy.id, strategy]));
  const version = data.strategyVersions.find(
    (item) => item.id === selection.strategyVersionId && isCurrentCoreVersion(item),
  );
  if (!version) return null;
  return {
    strategyVersionId: version.id,
    strategyName: version.fileState?.className ?? strategyById.get(version.strategyId)?.name ?? null,
  };
}

export function readinessMatchesCandidate(
  report: DryRunReadinessReport | null,
  candidate: DryRunCandidate | null,
): boolean {
  if (!report || !candidate || normalized(report.status) !== "READY") return false;
  if (!sameId(report.strategyVersionId, candidate.strategyVersionId)) return false;
  if (report.blockedReasons.length > 0 || report.checks.length === 0) return false;
  if (report.checks.some((check) => normalized(check.status) !== "READY")) return false;

  const preview = report.configPreview;
  if (preview.dry_run !== true || preview.initial_state !== "stopped") return false;

  const safety = report.safety;
  return safety.readiness_only === true
    && safety.starts_freqtrade === false
    && safety.live_trading === false
    && safety.real_orders === false
    && safety.exchange_connection === false;
}

export function readinessTerminalStatus(
  report: DryRunReadinessReport,
  candidate: DryRunCandidate,
): "SUCCESS" | "BLOCKED" | "API_GAP" {
  if (!sameId(report.strategyVersionId, candidate.strategyVersionId)) return "API_GAP";
  return readinessMatchesCandidate(report, candidate) ? "SUCCESS" : "BLOCKED";
}

export function runningEvidenceReason({
  candidate,
  dryRunSource,
  manifest,
  snapshot,
}: {
  candidate: DryRunCandidate | null;
  dryRunSource: DataSource;
  manifest: DryRunArtifactManifest | null;
  snapshot: DryRunStatusSnapshot;
}): string | null {
  if (dryRunSource !== "api") return "Dry-run 持久数据不是 API 来源，不能证明当前环境正在运行。";
  if (!candidate) return "缺少当前环境、带 database_ids 的核心候选版本。";
  if (!manifest?.manifestPath || !snapshot.artifactManifestPath) return "缺少 manifest 或 status snapshot 的 artifact identity。";
  if (!sameId(manifest.strategyVersionId, candidate.strategyVersionId)
    || !sameId(snapshot.strategyVersionId, candidate.strategyVersionId)) {
    return "Manifest、status snapshot 与当前候选 strategy version 不匹配。";
  }
  if (manifest.manifestPath !== snapshot.artifactManifestPath) {
    return "Status snapshot 引用的 manifest 与当前 manifest 不匹配。";
  }
  if (snapshot.dryRun !== true) return "Status snapshot 未证明 dry_run=true，禁止继续运行。";
  if (normalized(snapshot.status) !== "RUNNING") return `持久 status snapshot 为 ${normalized(snapshot.status) || "UNKNOWN"}，未证明正在运行。`;
  if (!["SUCCESS", "RUNNING"].includes(normalized(manifest.status))) {
    return `Manifest 状态为 ${normalized(manifest.status) || "UNKNOWN"}，不能证明当前运行。`;
  }
  return null;
}

export function stoppedEvidenceReason({
  candidate,
  dryRunSource,
  manifest,
  snapshot,
}: {
  candidate: DryRunCandidate | null;
  dryRunSource: DataSource;
  manifest: DryRunArtifactManifest | null;
  snapshot: DryRunStatusSnapshot;
}): string | null {
  if (dryRunSource !== "api") return "Dry-run 停止状态不是 API 持久来源。";
  if (!candidate) return "缺少当前核心候选，不能确认停止对象。";
  if (!sameId(snapshot.strategyVersionId, candidate.strategyVersionId)) return "停止 snapshot 与当前候选不匹配。";
  if (snapshot.dryRun !== true) return "停止 snapshot 未证明本次对象为 dry_run=true。";
  if (normalized(snapshot.status) !== "STOPPED") return "持久 status snapshot 尚未确认 STOPPED。";
  if (!manifest?.manifestPath || manifest.manifestPath !== snapshot.artifactManifestPath) {
    return "停止 snapshot 与当前 manifest identity 不匹配。";
  }
  if (!sameId(manifest.strategyVersionId, candidate.strategyVersionId)) return "停止 manifest 与当前候选不匹配。";
  return null;
}

export function deriveDryRunDecision({
  candidate,
  dryRunSource,
  manifest,
  readiness,
  runtimeReason,
  snapshot,
  transient,
}: {
  candidate: DryRunCandidate | null;
  dryRunSource: DataSource;
  manifest: DryRunArtifactManifest | null;
  readiness: DryRunReadinessReport | null;
  runtimeReason: string;
  snapshot: DryRunStatusSnapshot;
  transient: DryRunTransientState;
}): DryRunDecisionModel {
  const persistedStatus = normalized(snapshot.status);
  const runningReason = runningEvidenceReason({ candidate, dryRunSource, manifest, snapshot });
  const persistedRunning = persistedStatus === "RUNNING" && runningReason === null;
  const safetyVerified = snapshot.dryRun === true;

  if (transient.kind === "checking") {
    return {
      state: "CHECKING", blocker: null, conclusion: "正在检查当前候选的 readiness。",
      nextAction: "等待 Backend 返回 readiness report，不要重复提交。", action: null,
      persistedRunning, safetyVerified,
    };
  }
  if (transient.kind === "starting") {
    return {
      state: "STARTING", blocker: null, conclusion: "启动请求已提交，尚未由持久 snapshot 证明 RUNNING。",
      nextAction: "等待刷新 API 持久状态；期间不要重复提交。", action: null,
      persistedRunning, safetyVerified,
    };
  }
  if (transient.kind === "stopping") {
    return {
      state: "STOPPING", blocker: null, conclusion: "停止请求已提交，尚未由持久 snapshot 证明 STOPPED。",
      nextAction: "等待刷新 API 持久状态；期间不要重复提交。", action: null,
      persistedRunning, safetyVerified,
    };
  }
  if (transient.kind === "reconcile-blocked") {
    return {
      state: "BLOCKED", blocker: transient.reason, conclusion: "控制请求已返回，但持久状态尚未完成对账。",
      nextAction: "重新刷新 API management；未对账前不得推断启动或停止成功。", action: "refresh",
      persistedRunning, safetyVerified,
    };
  }
  if (!candidate) {
    return {
      state: "BLOCKED", blocker: "缺少当前环境、带 database_ids 的核心候选 strategy version。",
      conclusion: "当前没有可安全检查或启动的候选。", nextAction: "先完成生成、回测与评分并刷新核心证据。",
      action: null, persistedRunning, safetyVerified,
    };
  }
  if (persistedRunning) {
    return {
      state: "RUNNING", blocker: null, conclusion: "API manifest 与 snapshot 已证明当前候选仅在 Dry-run 运行。",
      nextAction: transient.kind === "failed"
        ? "停止请求失败；运行状态未改变，可核对辅助反馈后重试停止。"
        : "持续核对持久 snapshot；需要结束时执行停止。",
      action: "stop", persistedRunning, safetyVerified,
    };
  }
  if (transient.kind === "failed") {
    return {
      state: "FAILED", blocker: transient.reason, conclusion: "最近请求失败，未改变持久运行结论。",
      nextAction: "核对持久 snapshot 和错误证据后重新检查。", action: "check",
      persistedRunning, safetyVerified,
    };
  }
  if (persistedStatus === "RUNNING") {
    const canStopUnsafeCurrentRun = dryRunSource === "api"
      && sameId(manifest?.strategyVersionId, candidate.strategyVersionId)
      && sameId(snapshot.strategyVersionId, candidate.strategyVersionId);
    return {
      state: "BLOCKED", blocker: runningReason, conclusion: "存在 RUNNING 字样，但证据不足，不能视为安全运行。",
      nextAction: canStopUnsafeCurrentRun ? "立即停止当前受控运行并复核持久 snapshot。" : "修复来源或 identity 后重新检查；不要继续运行。",
      action: canStopUnsafeCurrentRun ? "stop" : "check", persistedRunning, safetyVerified,
    };
  }
  if (readiness) {
    if (readinessMatchesCandidate(readiness, candidate)) {
      return {
        state: "READY", blocker: null, conclusion: "当前候选的 readiness 与安全 config preview 已通过。",
        nextAction: "人工批准后仅启动本次受控 Dry-run。", action: "start",
        persistedRunning, safetyVerified,
      };
    }
    return {
      state: "BLOCKED",
      blocker: readiness.blockedReasons[0] ?? "Readiness report 与当前候选或安全 config preview 不匹配。",
      conclusion: "Readiness report 不可用于启动当前候选。",
      nextAction: "修复阻断后重新检查当前候选。", action: "check",
      persistedRunning, safetyVerified,
    };
  }
  if (persistedStatus === "STOPPED") {
    return {
      state: "STOPPED", blocker: null, conclusion: "持久 status snapshot 显示已停止。",
      nextAction: "需要再次运行时，先重新检查当前候选 readiness。", action: "check",
      persistedRunning, safetyVerified,
    };
  }
  if (persistedStatus === "FAILED") {
    return {
      state: "FAILED", blocker: snapshot.failedReason ?? "持久 status snapshot 显示 FAILED。",
      conclusion: "Dry-run 未成功运行。", nextAction: "修复失败原因后重新检查当前候选。",
      action: "check", persistedRunning, safetyVerified,
    };
  }
  if (persistedStatus === "BLOCKED") {
    return {
      state: "BLOCKED",
      blocker: snapshot.blockedReason ?? manifest?.blockedReason ?? runtimeReason,
      conclusion: "当前持久运行证据被阻断。", nextAction: "解决唯一阻断原因后重新检查当前候选。",
      action: "check", persistedRunning, safetyVerified,
    };
  }
  return {
    state: "NOT_CHECKED", blocker: null, conclusion: "尚未检查当前候选的 readiness。",
    nextAction: "检查当前候选 readiness。", action: "check", persistedRunning, safetyVerified,
  };
}
