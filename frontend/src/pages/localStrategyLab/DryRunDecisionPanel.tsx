import { useEffect, useRef, useState } from "react";

import {
  StrategyGenerationApiError,
  checkDryRunReadiness,
  startControlledDryRun,
  stopControlledDryRun,
} from "../../api/client";
import type {
  DataSource,
  DryRunControlReport,
  DryRunReadinessReport,
  MvpData,
} from "../../api/types";
import type { LabSelection } from "./candidateWorkbenchModel";
import {
  CopyableValue,
  ExpandableText,
  StatusBadge,
} from "../../components/DisplayPrimitives";
import { EMPTY_TEXT, displayBoolean } from "../uiCopy";
import {
  actionStatusMessage,
  createActionEvidence,
  createActionLifecycleId,
  latestActionEnvironmentScope,
  type ActionEvidence,
} from "./actionEvidence";
import { LatestActionFeedback } from "./ActionTimeline";
import {
  candidateIdentityMatches,
  deriveDryRunCandidate,
  deriveDryRunDecision,
  deriveDryRunRequestTarget,
  inactiveActionLabel,
  readinessReason,
  readinessTerminalStatus,
  reconcileCandidateScopedValue,
  runningEvidenceReason,
  stoppedEvidenceReason,
  type CandidateScopedValue,
  type DryRunTransientState,
} from "./dryRunDecisionModel";
import "../../styles/dry-run-decision.css";

type RecordActionEvidence = (entry: ActionEvidence) => void;

function asStatus(error: unknown): "UNAUTHORIZED" | "BLOCKED" | "FAILED" {
  if (error instanceof StrategyGenerationApiError) {
    return error.operationStatus ?? (error.status === 401 || error.status === 403 ? "UNAUTHORIZED" : "FAILED");
  }
  return "FAILED";
}

function asMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

function formatEvidence(value: Record<string, unknown>): string {
  const rows = Object.entries(value);
  return rows.length ? rows.map(([key, item]) => `${key}: ${String(item)}`).join(", ") : EMPTY_TEXT;
}

export function DryRunDecisionPanel({
  data,
  dryRunSource,
  error,
  history,
  isLoading,
  onRefresh,
  onOperatorTokenChange,
  onReconciliationChange,
  operatorToken,
  recordAction,
  selection,
}: {
  data: MvpData;
  dryRunSource: DataSource;
  error: string | null;
  history: ActionEvidence[];
  isLoading: boolean;
  onRefresh: () => void;
  onOperatorTokenChange: (value: string) => void;
  onReconciliationChange: (pending: boolean) => void;
  operatorToken: string;
  recordAction: RecordActionEvidence;
  selection: LabSelection;
}) {
  const [readinessState, setReadinessState] = useState<CandidateScopedValue<DryRunReadinessReport>>({
    strategyVersionId: null,
    value: null,
  });
  const [lastControlReport, setLastControlReport] = useState<DryRunControlReport | null>(null);
  const [transient, setTransient] = useState<DryRunTransientState>({ kind: "idle" });
  const [pendingReconciliation, setPendingReconciliation] = useState<{
    kind: "start" | "stop";
    seenLoading: boolean;
  } | null>(null);
  const [approvalState, setApprovalState] = useState<CandidateScopedValue<boolean>>({
    strategyVersionId: null,
    value: null,
  });
  const candidate = deriveDryRunCandidate(data, selection);
  const candidateId = candidate?.strategyVersionId ?? null;
  const requestTarget = deriveDryRunRequestTarget(data, selection);
  const candidateRequestIdentity = candidate
    ? [candidate.strategyVersionId, requestTarget?.pair, requestTarget?.timeframe, requestTarget?.exchange]
      .map((value) => value ?? "MISSING")
      .join("|")
    : null;
  const candidateIdentity = useRef({ strategyVersionId: candidateRequestIdentity, epoch: 0 });
  if (candidateIdentity.current.strategyVersionId !== candidateRequestIdentity) {
    candidateIdentity.current = {
      strategyVersionId: candidateRequestIdentity,
      epoch: candidateIdentity.current.epoch + 1,
    };
  }
  const readiness = reconcileCandidateScopedValue(readinessState, candidateRequestIdentity).value;
  const manualApproval = reconcileCandidateScopedValue(approvalState, candidateRequestIdentity).value === true;
  const runtime = data.operatorDashboard.runtimeContract.dryRunReadiness;
  const manifest = data.dryRun.manifest;
  const snapshot = data.dryRun.snapshot;
  const model = deriveDryRunDecision({
    candidate,
    dryRunSource,
    manifest,
    readiness,
    runtimeReason: readinessReason(runtime),
    snapshot,
    transient,
  });
  const environmentScope = candidate
    ? data.strategyVersions.find((version) => version.id === candidate.strategyVersionId)?.dataSource?.environment.scope ?? "unknown"
    : "unknown";
  const targetBlockedReason = requestTarget
    ? null
    : "缺少当前候选链上持久 BacktestProfile 的显式 pair、timeframe 或 exchange；不会使用默认目标。";
  const actionDisabledReason = model.action === "check"
    ? targetBlockedReason
    : model.action === "start"
    ? targetBlockedReason
      ? targetBlockedReason
      : !operatorToken
      ? "请输入本地 operator token 后才能启动。"
      : !manualApproval
        ? "勾选本次人工批准后才能启动。"
        : null
    : model.action === "stop" && !operatorToken
      ? "请输入本地 operator token 后才能停止。"
      : null;

  useEffect(() => {
    setReadinessState((current) => reconcileCandidateScopedValue(current, candidateRequestIdentity));
    setApprovalState((current) => reconcileCandidateScopedValue(current, candidateRequestIdentity));
    setTransient((current) =>
      current.kind === "checking" || current.kind === "failed"
        ? { kind: "idle" }
        : current);
  }, [candidateRequestIdentity]);

  useEffect(() => {
    if (!pendingReconciliation) return;
    if (isLoading) {
      if (!pendingReconciliation.seenLoading) {
        setPendingReconciliation({ ...pendingReconciliation, seenLoading: true });
      }
      return;
    }
    if (!pendingReconciliation.seenLoading) return;

    if (error || dryRunSource === "failed") {
      setTransient({
        kind: "reconcile-blocked",
        operation: pendingReconciliation.kind,
        reason: `刷新 management 失败，控制结果待对账：${error ?? "Dry-run API source=failed"}`,
      });
      setPendingReconciliation(null);
      return;
    }
    const reason = pendingReconciliation.kind === "start"
      ? runningEvidenceReason({ candidate, dryRunSource, manifest, snapshot })
      : stoppedEvidenceReason({ candidate, dryRunSource, manifest, snapshot });
    setTransient(reason
      ? {
        kind: "reconcile-blocked",
        operation: pendingReconciliation.kind,
        reason: `持久状态未完成${pendingReconciliation.kind === "start" ? "启动" : "停止"}对账：${reason}`,
      }
      : { kind: "idle" });
    if (!reason) onReconciliationChange(false);
    setPendingReconciliation(null);
  }, [
    candidate,
    dryRunSource,
    error,
    isLoading,
    manifest,
    pendingReconciliation,
    snapshot,
    onReconciliationChange,
  ]);

  async function check() {
    if (!candidate || !requestTarget || !candidateRequestIdentity) return;
    const checkedCandidateId = candidate.strategyVersionId;
    const checkedCandidateRequestIdentity = candidateRequestIdentity;
    const checkedCandidateIdentity = { ...candidateIdentity.current };
    setReadinessState({ strategyVersionId: checkedCandidateRequestIdentity, value: null });
    setApprovalState({ strategyVersionId: checkedCandidateRequestIdentity, value: null });
    setTransient({ kind: "checking" });
    const lifecycleId = createActionLifecycleId("dry-run");
    recordAction(createActionEvidence({
      action: "检查 Dry-run readiness", lifecycleId, status: "RUNNING", message: actionStatusMessage("RUNNING"),
      nextAction: "等待 Backend readiness report。", recommendBug: false,
      databaseIds: { strategy_version_id: candidate.strategyVersionId }, updatedAt: new Date().toISOString(),
    }));
    try {
      const result = await checkDryRunReadiness({
        ...requestTarget,
        strategyName: candidate.strategyName,
        strategyVersionId: candidate.strategyVersionId,
      });
      const isCurrentCompletion = candidateIdentityMatches(candidateIdentity.current, checkedCandidateIdentity);
      if (isCurrentCompletion) {
        setReadinessState({ strategyVersionId: checkedCandidateRequestIdentity, value: result });
        setTransient({ kind: "idle" });
      }
      if (!isCurrentCompletion) {
        recordAction(createActionEvidence({
          action: "检查 Dry-run readiness（过期审计）",
          lifecycleId,
          status: "BLOCKED",
          message: `NOT_CURRENT：候选 identity 已变化，忽略旧请求返回；response strategy_version_id=${result.strategyVersionId ?? "missing"}。`,
          nextAction: "仅保留为旧候选审计；当前候选必须独立重新检查。",
          recommendBug: false,
          databaseIds: result.strategyVersionId
            ? { strategy_version_id: result.strategyVersionId }
            : { strategy_version_id: checkedCandidateId },
          updatedAt: new Date().toISOString(),
        }));
        return;
      }
      const terminalStatus = readinessTerminalStatus(result, candidate);
      const responseIdMismatch = terminalStatus === "API_GAP";
      const message = responseIdMismatch
        ? `Readiness response identity mismatch：请求 strategy_version_id=${checkedCandidateId}，响应 strategy_version_id=${result.strategyVersionId ?? "missing"}。`
        : terminalStatus === "BLOCKED"
          ? result.blockedReasons[0] ?? "Readiness report 未通过完整候选与安全校验。"
          : "当前候选 readiness report 已返回并通过完整校验。";
      recordAction(createActionEvidence({
        action: "检查 Dry-run readiness", lifecycleId, status: terminalStatus,
        message,
        nextAction: terminalStatus === "SUCCESS"
          ? "核对安全 config preview 后由人工批准本次受控 Dry-run。"
          : responseIdMismatch
            ? "修复 Backend response identity 后重新检查；不得批准或启动。"
            : "按 report 修复后重试。",
        recommendBug: responseIdMismatch,
        databaseIds: result.strategyVersionId
          ? { strategy_version_id: result.strategyVersionId }
          : {},
        updatedAt: new Date().toISOString(),
      }));
    } catch (error) {
      const reason = asMessage(error, "Readiness API 请求失败。");
      const isCurrentCompletion = candidateIdentityMatches(candidateIdentity.current, checkedCandidateIdentity);
      if (isCurrentCompletion) {
        setTransient({ kind: "failed", reason });
      }
      if (!isCurrentCompletion) {
        recordAction(createActionEvidence({
          action: "检查 Dry-run readiness（过期审计）",
          lifecycleId,
          status: "BLOCKED",
          message: `NOT_CURRENT：候选 identity 已变化，忽略旧请求错误：${reason}`,
          nextAction: "仅保留为旧候选审计；当前候选状态不受影响。",
          recommendBug: false,
          databaseIds: { strategy_version_id: checkedCandidateId },
          updatedAt: new Date().toISOString(),
        }));
        return;
      }
      recordAction(createActionEvidence({
        action: "检查 Dry-run readiness", lifecycleId, status: asStatus(error), message: reason,
        nextAction: "检查 API、策略版本与服务日志后重试。", recommendBug: true,
        databaseIds: { strategy_version_id: candidate.strategyVersionId }, updatedAt: new Date().toISOString(),
      }));
    }
  }

  async function start() {
    if (!candidate || !requestTarget || !candidateRequestIdentity || !manualApproval || !operatorToken) return;
    setTransient({ kind: "starting" });
    const lifecycleId = createActionLifecycleId("dry-run");
    recordAction(createActionEvidence({
      action: "启动 controlled dry-run", lifecycleId, status: "RUNNING", message: actionStatusMessage("RUNNING"),
      nextAction: "等待启动报告并刷新持久 snapshot。", recommendBug: false,
      databaseIds: { strategy_version_id: candidate.strategyVersionId }, updatedAt: new Date().toISOString(),
    }));
    try {
      const result = await startControlledDryRun({
        ...requestTarget,
        manualApproval: true,
        strategyName: candidate.strategyName,
        strategyVersionId: candidate.strategyVersionId,
      }, operatorToken);
      setLastControlReport(result);
      setReadinessState({ strategyVersionId: candidateRequestIdentity, value: null });
      setApprovalState({ strategyVersionId: candidateRequestIdentity, value: null });
      const status = result.status === "SUCCESS" ? "SUCCESS" : result.status === "BLOCKED" ? "BLOCKED" : "FAILED";
      recordAction(createActionEvidence({
        action: "启动 controlled dry-run", lifecycleId, status,
        message: result.failedReason ?? result.blockedReasons[0] ?? `启动请求返回 ${result.status}。`,
        nextAction: "刷新并以 API manifest/status snapshot 复核真实运行状态。", recommendBug: status === "FAILED",
        databaseIds: { strategy_version_id: candidate.strategyVersionId },
        artifactPaths: [result.manifestPath, result.statusSnapshotPath], updatedAt: new Date().toISOString(),
      }));
      if (status === "SUCCESS") {
        onReconciliationChange(true);
        setPendingReconciliation({ kind: "start", seenLoading: false });
        onRefresh();
      } else {
        setTransient({ kind: "failed", reason: result.failedReason ?? result.blockedReasons[0] ?? `启动请求返回 ${result.status}。` });
      }
    } catch (error) {
      const reason = asMessage(error, "受控 Dry-run 启动请求失败。");
      setTransient({ kind: "failed", reason });
      recordAction(createActionEvidence({
        action: "启动 controlled dry-run", lifecycleId, status: asStatus(error), message: reason,
        nextAction: "核对持久 snapshot 与本地授权后重新检查。", recommendBug: true,
        databaseIds: { strategy_version_id: candidate.strategyVersionId }, updatedAt: new Date().toISOString(),
      }));
    }
  }

  async function stop() {
    if (!operatorToken) return;
    setTransient({ kind: "stopping" });
    const lifecycleId = createActionLifecycleId("dry-run");
    recordAction(createActionEvidence({
      action: "停止 controlled dry-run", lifecycleId, status: "RUNNING", message: actionStatusMessage("RUNNING"),
      nextAction: "等待停止报告并刷新持久 snapshot。", recommendBug: false,
      databaseIds: candidateId ? { strategy_version_id: candidateId } : {},
      updatedAt: new Date().toISOString(),
    }));
    try {
      const result = await stopControlledDryRun(operatorToken);
      setLastControlReport(result);
      setReadinessState({ strategyVersionId: candidateId, value: null });
      const status = result.status === "STOPPED" || result.status === "SUCCESS"
        ? "SUCCESS"
        : result.status === "BLOCKED" ? "BLOCKED" : "FAILED";
      recordAction(createActionEvidence({
        action: "停止 controlled dry-run", lifecycleId, status,
        message: result.failedReason ?? result.blockedReasons[0] ?? `停止请求返回 ${result.status}。`,
        nextAction: "刷新并以 API status snapshot 确认 STOPPED。", recommendBug: status === "FAILED",
        databaseIds: candidateId ? { strategy_version_id: candidateId } : {},
        artifactPaths: [result.manifestPath, result.statusSnapshotPath], updatedAt: new Date().toISOString(),
      }));
      if (status === "SUCCESS") {
        onReconciliationChange(true);
        setPendingReconciliation({ kind: "stop", seenLoading: false });
        onRefresh();
      } else {
        setTransient({ kind: "failed", reason: result.failedReason ?? result.blockedReasons[0] ?? `停止请求返回 ${result.status}。` });
      }
    } catch (error) {
      const reason = asMessage(error, "受控 Dry-run 停止请求失败。");
      setTransient({ kind: "failed", reason });
      recordAction(createActionEvidence({
        action: "停止 controlled dry-run", lifecycleId, status: asStatus(error), message: reason,
        nextAction: "核对本地授权和持久运行状态后重试。", recommendBug: true,
        databaseIds: candidateId ? { strategy_version_id: candidateId } : {},
        updatedAt: new Date().toISOString(),
      }));
    }
  }

  function refreshReconciliation() {
    if (transient.kind !== "reconcile-blocked") return;
    setPendingReconciliation({ kind: transient.operation, seenLoading: false });
    setTransient({ kind: transient.operation === "start" ? "starting" : "stopping" });
    onRefresh();
  }

  const actionLabel = model.action === "check"
    ? "重新检查"
    : model.action === "refresh"
      ? "刷新状态"
    : model.action === "start"
      ? "启动 Dry-run"
      : model.action === "stop"
        ? "停止 Dry-run"
        : "";

  return (
    <section
      aria-label="Dry-run 统一决策区"
      className="dry-run-decision"
      data-state={model.state}
      data-testid="dry-run-decision"
    >
      <header className="dry-run-decision__header">
        <div>
          <span>运行前唯一决策</span>
          <h2>受控 Dry-run</h2>
        </div>
        <StatusBadge label={model.state} showRaw status={model.state} />
      </header>

      <div className="dry-run-decision__summary">
        <div><span>当前候选</span><strong>{candidate?.strategyName ?? EMPTY_TEXT}</strong><small>strategy_version_id={candidate?.strategyVersionId ?? EMPTY_TEXT}</small></div>
        <div><span>Readiness</span><strong>{readiness?.status ?? "NOT_CHECKED"}</strong><small>{runtime.summary}</small></div>
        <div><span>持久运行</span><strong>{snapshot.status}</strong><small>仅 API manifest/status snapshot 可证明</small></div>
        <div><span>安全结论</span><strong>{model.persistedRunning && model.safetyVerified ? "DRY_RUN_ONLY" : "未证明可继续"}</strong><small>dry_run={displayBoolean(snapshot.dryRun)}</small></div>
      </div>

      <div className="dry-run-decision__decision">
        <div>
          <span>{model.blocker ? "唯一阻断原因" : "当前结论"}</span>
          <strong>{model.blocker ?? model.conclusion}</strong>
        </div>
        <div><span>推荐下一步</span><strong>{model.nextAction}</strong></div>
      </div>

      <div className="dry-run-decision__action">
        {model.state === "READY" ? (
          <label className="inline-check">
            <input
              checked={manualApproval}
              onChange={(event) => setApprovalState({
                strategyVersionId: candidateRequestIdentity,
                value: event.target.checked,
              })}
              type="checkbox"
            />
            人工批准本次受控 Dry-run
          </label>
        ) : null}
        {model.action === "start" || model.action === "stop" ? (
          <label className="dry-run-decision__token">
            <span>Operator token</span>
            <input
              autoComplete="off"
              onChange={(event) => onOperatorTokenChange(event.currentTarget.value)}
              placeholder="仅用于本地本次请求"
              type="password"
              value={operatorToken}
            />
          </label>
        ) : null}
        {model.action ? (
          <button
            className="primary-button"
            disabled={Boolean(actionDisabledReason)}
            onClick={
              model.action === "check"
                ? check
                : model.action === "refresh"
                  ? refreshReconciliation
                  : model.action === "start"
                    ? start
                    : stop
            }
            type="button"
          >
            {actionLabel}
          </button>
        ) : <span className="dry-run-decision__inactive-action">{inactiveActionLabel(model.state)}</span>}
        {actionDisabledReason ? <small className="dry-run-decision__disabled-reason">{actionDisabledReason}</small> : null}
      </div>

      <LatestActionFeedback
        actions={["检查 Dry-run readiness", "启动 controlled dry-run", "停止 controlled dry-run"]}
        environmentScope={model.action === "stop"
          ? latestActionEnvironmentScope({ actions: ["停止 controlled dry-run"], history, phase: "dry-run" })
          : environmentScope}
        expectedEntityIds={{ strategy_version_id: candidateId }}
        history={history}
        phase="dry-run"
      />

      <details className="dry-run-decision__audit">
        <summary>展开 readiness、manifest、snapshot 与 checks</summary>
        <dl>
          <div><dt>profile</dt><dd><CopyableValue label="Dry-run profile" value={readiness?.profileName ?? snapshot.profileName ?? manifest?.profileName ?? EMPTY_TEXT} /></dd></div>
          <div><dt>manifest</dt><dd><CopyableValue label="Manifest 路径" value={manifest?.manifestPath ?? EMPTY_TEXT} /></dd></div>
          <div><dt>snapshot manifest</dt><dd><CopyableValue label="Snapshot manifest 路径" value={snapshot.artifactManifestPath ?? EMPTY_TEXT} /></dd></div>
          <div><dt>config preview</dt><dd><ExpandableText mono value={readiness ? formatEvidence(readiness.configPreview) : EMPTY_TEXT} /></dd></div>
          <div><dt>safety</dt><dd><ExpandableText mono value={readiness ? formatEvidence(readiness.safety) : EMPTY_TEXT} /></dd></div>
          <div><dt>最近 control report</dt><dd><ExpandableText mono value={lastControlReport ? `${lastControlReport.status} · ${lastControlReport.statusSnapshotPath}` : EMPTY_TEXT} /></dd></div>
        </dl>
        {readiness?.checks.length ? (
          <div className="dry-run-decision__checks">
            {readiness.checks.map((item) => (
              <article key={item.name}>
                <strong>{item.name}</strong>
                <StatusBadge showRaw status={item.status} />
                <p>{item.blockedReason ?? item.summary}</p>
              </article>
            ))}
          </div>
        ) : null}
      </details>

      <aside role="note">
        本区只允许当前本地环境的受控 Dry-run；始终禁止 live trading、真实订单和真实交易执行链路。
      </aside>
    </section>
  );
}
