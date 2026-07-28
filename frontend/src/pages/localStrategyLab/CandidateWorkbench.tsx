import { useEffect, useRef, useState } from "react";

import { ingestBacktestArtifact, triggerLocalBacktest } from "../../api/client";
import {
  loadStrategyPromotionStatus,
  type StrategyPromotionStatus,
} from "../../api/strategyPromotionApi";
import type { MvpData } from "../../api/types";
import { CopyableValue, StatusBadge } from "../../components/DisplayPrimitives";
import { LatestActionFeedback } from "./ActionTimeline";
import {
  backtestBlockReason,
  buildLocalBacktestProfile,
  candidateWorkbenchChain,
  DEFAULT_BACKTEST_PROFILE_DRAFT,
  ingestBlockReason,
  sanitizedBlockedReasons,
  type BacktestProfileDraft,
  type LabSelection,
} from "./candidateWorkbenchModel";
import {
  createActionEvidence,
  createActionLifecycleId,
  type ActionEvidence,
} from "./actionEvidence";
import "../../styles/local-strategy-lab-candidate-workbench.css";

type RecordActionEvidence = (entry: ActionEvidence) => void;
type PendingProof =
  | {
      kind: "backtest";
      lifecycleId: string;
      strategyVersionId: string;
      backtestRunId: string;
      blockedReason: string | null;
      startedAt: number;
    }
  | {
      kind: "score";
      lifecycleId: string;
      strategyVersionId: string;
      backtestTaskId: string;
      backtestResultId: string;
      scoreId: string;
      artifactPaths: Array<string | null | undefined>;
      startedAt: number;
    };

function recordValue(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function responseId(value: unknown): string | null {
  const id = recordValue(value).id;
  return id === null || id === undefined || !String(id).trim() ? null : String(id);
}

function responseText(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

function SelectField({
  disabled = false,
  label,
  onChange,
  options,
  placeholder,
  value,
}: {
  disabled?: boolean;
  label: string;
  onChange: (value: string | null) => void;
  options: Array<{ id: string; label: string }>;
  placeholder: string;
  value: string | null;
}) {
  return (
    <label className="lab-workbench__select">
      <span>{label}</span>
      <select
        disabled={disabled || options.length === 0}
        onChange={(event) => onChange(event.target.value || null)}
        value={value ?? ""}
      >
        <option value="">{placeholder}</option>
        {options.map((option) => (
          <option key={option.id} value={option.id}>{option.label}</option>
        ))}
      </select>
    </label>
  );
}

export function CandidateWorkbench({
  data,
  history,
  onRefresh,
  operatorToken,
  recordAction,
  selection,
  select,
}: {
  data: MvpData;
  history: ActionEvidence[];
  onRefresh: () => void;
  operatorToken: string;
  recordAction: RecordActionEvidence;
  selection: LabSelection;
  select: (key: keyof LabSelection, value: string | null) => void;
}) {
  const [busyAction, setBusyAction] = useState<"backtest" | "score" | null>(null);
  const [pendingProof, setPendingProof] = useState<PendingProof | null>(null);
  const [profileDraft, setProfileDraft] = useState<BacktestProfileDraft>(DEFAULT_BACKTEST_PROFILE_DRAFT);
  const [promotion, setPromotion] = useState<StrategyPromotionStatus | null>(null);
  const [promotionError, setPromotionError] = useState<string | null>(null);
  const busyRef = useRef(false);
  const chain = candidateWorkbenchChain(data, selection);
  const strategyNames = new Map(data.strategies.map((strategy) => [strategy.id, strategy.name]));
  const candidate = chain.versions.find((version) => version.id === selection.strategyVersionId);
  const selectedRun = chain.runs.find((run) => run.id === selection.backtestRunId);
  const selectedTask = chain.tasks.find((task) => task.id === selection.backtestTaskId);
  const selectedResult = chain.results.find((result) => result.id === selection.backtestResultId);
  const selectedScore = chain.scores.find((score) => score.scoreId === selection.scoreId);
  const strategyName = candidate
    ? candidate.fileState?.className ?? strategyNames.get(candidate.strategyId) ?? null
    : null;
  const builtProfile = buildLocalBacktestProfile(profileDraft, {
    name: strategyName,
    path: candidate?.filePath,
  });
  const backtestReason = pendingProof?.kind === "backtest"
    ? "回测请求已接收；正在等待 GET 返回同一 backtest_run_id 的当前环境持久记录。"
    : backtestBlockReason(data, selection, operatorToken, builtProfile.profile, builtProfile.reason);
  const scoreReason = pendingProof?.kind === "score"
    ? "导入请求已接收；正在等待 GET 同时返回匹配的 BacktestResult 与 StrategyScore。"
    : ingestBlockReason(data, selection, operatorToken);

  useEffect(() => {
    if (!pendingProof) return;
    const timeout = window.setTimeout(() => {
      recordAction(createActionEvidence({
        action: pendingProof.kind === "backtest" ? "触发本地回测" : "导入回测结果并计算评分",
        lifecycleId: pendingProof.lifecycleId,
        status: "API_GAP",
        message: "请求已返回，但在等待窗口内没有通过 GET 对账到匹配的当前环境持久记录。",
        nextAction: "刷新 API/DB；按同一候选链核对 database_ids，确认没有运行中任务后再决定是否重试。",
        recommendBug: true,
        databaseIds: pendingProof.kind === "backtest"
          ? {
              strategy_version_id: pendingProof.strategyVersionId,
              backtest_run_id: pendingProof.backtestRunId,
            }
          : {
              strategy_version_id: pendingProof.strategyVersionId,
              backtest_task_id: pendingProof.backtestTaskId,
              backtest_result_id: pendingProof.backtestResultId,
              strategy_score_id: pendingProof.scoreId,
            },
        artifactPaths: pendingProof.kind === "score" ? pendingProof.artifactPaths : [],
        updatedAt: new Date().toISOString(),
      }));
      setPendingProof(null);
    }, Math.max(0, 15_000 - (Date.now() - pendingProof.startedAt)));
    return () => window.clearTimeout(timeout);
  }, [pendingProof, recordAction]);

  useEffect(() => {
    if (!pendingProof) return;
    if (pendingProof.kind === "backtest") {
      const run = data.backtestRuns.find(
        (item) =>
          item.id === pendingProof.backtestRunId &&
          item.strategyVersionId === pendingProof.strategyVersionId,
      );
      const verified = run && candidateWorkbenchChain(data, {
        strategyVersionId: pendingProof.strategyVersionId,
        backtestRunId: run.id,
        backtestTaskId: null,
        backtestResultId: null,
        scoreId: null,
      }).runs.some((item) => item.id === run.id);
      if (!verified) return;
      const normalized = run.status.toLowerCase();
      const status = normalized === "failed"
        ? "FAILED"
        : normalized === "blocked"
          ? "BLOCKED"
          : "SUCCESS";
      recordAction(createActionEvidence({
        action: "触发本地回测",
        lifecycleId: pendingProof.lifecycleId,
        status,
        message: pendingProof.blockedReason ?? `GET 已对账到当前环境 backtest run，持久状态为 ${run.status}。`,
        nextAction: status === "SUCCESS"
          ? "选择该 run 并等待其 BacktestTask/BacktestResult 持久化。"
          : "查看该 run 的失败或阻断原因，修复后再重试。",
        recommendBug: status === "FAILED",
        databaseIds: {
          strategy_version_id: pendingProof.strategyVersionId,
          backtest_run_id: run.id,
        },
        updatedAt: new Date().toISOString(),
      }));
      setPendingProof(null);
      return;
    }

    const result = data.backtestResults.find(
      (item) =>
        item.id === pendingProof.backtestResultId &&
        item.taskId === pendingProof.backtestTaskId,
    );
    const score = data.ranking.find(
      (item) =>
        item.scoreId === pendingProof.scoreId &&
        item.strategyVersionId === pendingProof.strategyVersionId &&
        item.backtestResultId === pendingProof.backtestResultId,
    );
    const proofSelection = {
      strategyVersionId: pendingProof.strategyVersionId,
      backtestRunId: result?.runId ?? null,
      backtestTaskId: pendingProof.backtestTaskId,
      backtestResultId: pendingProof.backtestResultId,
      scoreId: pendingProof.scoreId,
    };
    const proofChain = candidateWorkbenchChain(data, proofSelection);
    if (
      !result ||
      !score ||
      !proofChain.results.some((item) => item.id === result.id) ||
      !proofChain.scores.some((item) => item.scoreId === score.scoreId)
    ) {
      return;
    }
    recordAction(createActionEvidence({
      action: "导入回测结果并计算评分",
      lifecycleId: pendingProof.lifecycleId,
      status: "SUCCESS",
      message: "GET 已对账到同一候选链上的持久 BacktestResult 与 StrategyScore。",
      nextAction: "查看评分摘要；后续 Dry-run 仍需独立 readiness 与人工批准。",
      recommendBug: false,
      databaseIds: {
        strategy_version_id: pendingProof.strategyVersionId,
        backtest_task_id: pendingProof.backtestTaskId,
        backtest_result_id: result.id,
        strategy_score_id: score.scoreId,
      },
      artifactPaths: pendingProof.artifactPaths,
      updatedAt: new Date().toISOString(),
    }));
    setPendingProof(null);
  }, [data, pendingProof, recordAction]);

  useEffect(() => {
    if (!selection.strategyVersionId || !selection.backtestResultId || !selection.scoreId) {
      setPromotion(null);
      setPromotionError(null);
      return;
    }
    const controller = new AbortController();
    setPromotion(null);
    setPromotionError(null);
    loadStrategyPromotionStatus(
      selection.strategyVersionId,
      selection.backtestResultId,
      selection.scoreId,
      controller.signal,
    )
      .then(setPromotion)
      .catch((error: unknown) => {
        if (!controller.signal.aborted) {
          setPromotionError(error instanceof Error ? error.message : "策略晋级状态读取失败。");
        }
      });
    return () => controller.abort();
  }, [selection.backtestResultId, selection.scoreId, selection.strategyVersionId]);

  async function handleBacktest() {
    if (busyRef.current || backtestReason || !selection.strategyVersionId || !builtProfile.profile) return;
    busyRef.current = true;
    setBusyAction("backtest");
    const lifecycleId = createActionLifecycleId("backtest");
    recordAction(createActionEvidence({
      action: "触发本地回测",
      lifecycleId,
      status: "RUNNING",
      message: "正在为明确选择的 strategy version 提交本地回测请求。",
      nextAction: "等待 POST 响应；响应后仍须由 GET 持久记录完成对账。",
      recommendBug: false,
      databaseIds: { strategy_version_id: selection.strategyVersionId },
      updatedAt: new Date().toISOString(),
    }));
    try {
      const response = await triggerLocalBacktest(selection.strategyVersionId, builtProfile.profile, operatorToken);
      const runId = responseId(response.run);
      const blocked = responseText(response.preflight_status)?.toLowerCase() === "blocked";
      const blockedReason = blocked ? sanitizedBlockedReasons(response.blocked_reasons) : null;
      if (!runId) {
        recordAction(createActionEvidence({
          action: "触发本地回测",
          lifecycleId,
          status: "API_GAP",
          message: blockedReason ?? "POST 响应缺少 backtest_run_id，不能等待或证明持久结果。",
          nextAction: "刷新持久数据；修复 API 响应契约后再重试。",
          recommendBug: !blocked,
          databaseIds: { strategy_version_id: selection.strategyVersionId, backtest_run_id: runId },
          updatedAt: new Date().toISOString(),
        }));
        onRefresh();
      } else {
        recordAction(createActionEvidence({
          action: "触发本地回测",
          lifecycleId,
          status: "RUNNING",
          message: blockedReason ?? "POST 已返回 run ID；尚未作为持久完成，正在刷新 GET 证据。",
          nextAction: blocked
            ? "等待 GET 对账 BLOCKED run；必须修改 profile 后才能重试。"
            : "等待 GET 返回相同 strategy_version_id 与 backtest_run_id。",
          recommendBug: false,
          databaseIds: { strategy_version_id: selection.strategyVersionId, backtest_run_id: runId },
          updatedAt: new Date().toISOString(),
        }));
        setPendingProof({
          kind: "backtest",
          lifecycleId,
          strategyVersionId: selection.strategyVersionId,
          backtestRunId: runId,
          blockedReason,
          startedAt: Date.now(),
        });
        onRefresh();
      }
    } catch (error) {
      recordAction(createActionEvidence({
        action: "触发本地回测",
        lifecycleId,
        status: "FAILED",
        message: errorMessage(error, "本地回测请求失败。"),
        nextAction: "检查授权、API 与持久 run；确认没有运行中任务后再重试。",
        recommendBug: true,
        databaseIds: { strategy_version_id: selection.strategyVersionId },
        updatedAt: new Date().toISOString(),
      }));
    } finally {
      busyRef.current = false;
      setBusyAction(null);
    }
  }

  async function handleIngest() {
    if (busyRef.current || scoreReason || !candidate || !selectedTask) return;
    busyRef.current = true;
    setBusyAction("score");
    const lifecycleId = createActionLifecycleId("score");
    const artifactPaths = [selectedTask.artifactManifest?.manifestPath, selectedTask.resultPath];
    recordAction(createActionEvidence({
      action: "导入回测结果并计算评分",
      lifecycleId,
      status: "RUNNING",
      message: "正在为明确选择的 BacktestTask 导入 artifact 并请求评分。",
      nextAction: "等待 POST 响应；只有 GET 同时返回匹配 result 与 score 才完成。",
      recommendBug: false,
      databaseIds: {
        strategy_version_id: candidate.id,
        backtest_task_id: selectedTask.id,
      },
      artifactPaths,
      updatedAt: new Date().toISOString(),
    }));
    try {
      const response = await ingestBacktestArtifact(selectedTask.id, {
        manifestPath: selectedTask.artifactManifest?.manifestPath,
        resultPath: selectedTask.resultPath,
        strategyName: selectedTask.strategyName,
      }, operatorToken);
      const taskId = responseId(response.task);
      const resultId = responseId(response.result);
      const scoreId = responseId(response.score);
      const status = responseText(response.ingest_status)?.toLowerCase();
      if (status === "blocked" || status === "failed" || taskId !== selectedTask.id || !resultId || !scoreId) {
        const blocked = status === "blocked";
        recordAction(createActionEvidence({
          action: "导入回测结果并计算评分",
          lifecycleId,
          status: blocked ? "BLOCKED" : status === "failed" ? "FAILED" : "API_GAP",
          message: responseText(response.reason) ?? "POST 响应缺少匹配的 task/result/score ID。",
          nextAction: "检查 artifact、任务状态和 API database_ids；不得将不完整响应视为完成。",
          recommendBug: !blocked,
          databaseIds: {
            strategy_version_id: candidate.id,
            backtest_task_id: taskId ?? selectedTask.id,
            backtest_result_id: resultId,
            strategy_score_id: scoreId,
          },
          artifactPaths,
          updatedAt: new Date().toISOString(),
        }));
      } else {
        recordAction(createActionEvidence({
          action: "导入回测结果并计算评分",
          lifecycleId,
          status: "RUNNING",
          message: "POST 已返回 result/score ID；尚未作为持久完成，正在刷新 GET 证据。",
          nextAction: "等待 GET 返回同一候选链的 BacktestResult 与 StrategyScore。",
          recommendBug: false,
          databaseIds: {
            strategy_version_id: candidate.id,
            backtest_task_id: selectedTask.id,
            backtest_result_id: resultId,
            strategy_score_id: scoreId,
          },
          artifactPaths,
          updatedAt: new Date().toISOString(),
        }));
        setPendingProof({
          kind: "score",
          lifecycleId,
          strategyVersionId: candidate.id,
          backtestTaskId: selectedTask.id,
          backtestResultId: resultId,
          scoreId,
          artifactPaths,
          startedAt: Date.now(),
        });
        onRefresh();
      }
    } catch (error) {
      recordAction(createActionEvidence({
        action: "导入回测结果并计算评分",
        lifecycleId,
        status: "FAILED",
        message: errorMessage(error, "artifact 导入或评分请求失败。"),
        nextAction: "检查 artifact、授权与持久任务；确认无并发请求后再重试。",
        recommendBug: true,
        databaseIds: {
          strategy_version_id: candidate.id,
          backtest_task_id: selectedTask.id,
        },
        artifactPaths,
        updatedAt: new Date().toISOString(),
      }));
    } finally {
      busyRef.current = false;
      setBusyAction(null);
    }
  }

  return (
    <section className="lab-workbench" aria-label="候选策略到评分主操作台">
      <div className="lab-workbench__heading">
        <div>
          <span>Candidate workbench</span>
          <h2>候选策略 → 回测 → 结果 → 评分</h2>
        </div>
        <StatusBadge
          label={selectedScore ? "评分已持久化" : pendingProof ? "等待持久证据" : candidate ? "候选已选择" : "待选择"}
          status={selectedScore ? "SUCCESS" : pendingProof ? "RUNNING" : candidate ? "READY" : "BLOCKED"}
        />
      </div>

      <div className="lab-workbench__candidate">
        <SelectField
          label="当前 strategy version"
          onChange={(value) => select("strategyVersionId", value)}
          options={chain.versions.map((version) => ({
            id: version.id,
            label: `${strategyNames.get(version.strategyId) ?? version.fileState?.className ?? "未命名策略"} · v${version.versionNumber} · ID ${version.id}`,
          }))}
          placeholder={chain.versions.length > 1 ? "请选择候选（不会默认第一条）" : "暂无可运行候选"}
          value={selection.strategyVersionId}
        />
        {candidate ? (
          <dl>
            <div><dt>策略文件</dt><dd>{candidate.filePath}</dd></div>
            <div><dt>文件状态</dt><dd>{candidate.fileState?.exists ? "存在且为文件" : "不可用"} · {candidate.validationStatus}</dd></div>
            <div><dt>来源结论</dt><dd>current · runnable · database ID 精确匹配</dd></div>
          </dl>
        ) : (
          <p className="lab-workbench__blocker">只有当前环境、runnable、core database 且文件有效的策略版本可进入操作台。</p>
        )}
      </div>

      <fieldset className="lab-workbench__profile">
        <legend>BacktestProfileV2（本地数据）</legend>
        {([
          ["profileName", "profile_name", "local-strategy-lab"],
          ["pair", "pair", "BTC/USDT"],
          ["timeframe", "timeframe", "5m"],
          ["timerange", "timerange", "20240101-20240201"],
        ] as const).map(([key, label, placeholder]) => (
          <label key={key}>
            <span>{label}</span>
            <input
              onChange={(event) => setProfileDraft((current) => ({ ...current, [key]: event.target.value }))}
              placeholder={placeholder}
              value={profileDraft[key]}
            />
          </label>
        ))}
        <small>
          strategy={strategyName ?? "未选择"} · path={candidate?.filePath ?? "未选择"} · data_source=local/okx/user_data/data
        </small>
        {builtProfile.reason ? <p className="lab-workbench__blocker">{builtProfile.reason}</p> : null}
      </fieldset>

      <ol className="lab-workbench__steps">
        <li>
          <div className="lab-workbench__step-title"><span>1</span><h3>触发本地回测</h3></div>
          <p>{backtestReason ?? "候选已通过当前环境与文件门禁，可创建一次本地回测。"}</p>
          <button
            className="primary-button"
            disabled={Boolean(backtestReason) || busyAction !== null}
            onClick={handleBacktest}
            type="button"
          >
            {busyAction === "backtest" ? "正在触发…" : "触发此候选的回测"}
          </button>
          <LatestActionFeedback
            actions={["触发本地回测"]}
            environmentScope={candidate?.dataSource.environment.scope ?? "unknown"}
            expectedEntityIds={{ strategy_version_id: candidate?.id }}
            history={history}
            phase="backtest"
          />
        </li>

        <li>
          <div className="lab-workbench__step-title"><span>2</span><h3>等待并核对持久结果</h3></div>
          <div className="lab-workbench__selectors">
            <SelectField
              label="BacktestRun"
              onChange={(value) => select("backtestRunId", value)}
              options={chain.runs.map((run) => ({ id: run.id, label: `Run ${run.id} · ${run.status}` }))}
              placeholder={chain.runs.length > 1 ? "请选择同一候选的 run" : "等待持久 run"}
              value={selection.backtestRunId}
            />
            <SelectField
              disabled={!selectedRun}
              label="BacktestTask"
              onChange={(value) => select("backtestTaskId", value)}
              options={chain.tasks.map((task) => ({ id: task.id, label: `Task ${task.id} · ${task.status}` }))}
              placeholder={chain.tasks.length > 1 ? "请选择同一 run 的 task" : "等待持久 task"}
              value={selection.backtestTaskId}
            />
            <SelectField
              disabled={!selectedTask}
              label="BacktestResult"
              onChange={(value) => select("backtestResultId", value)}
              options={chain.results.map((result) => ({ id: result.id, label: `Result ${result.id}` }))}
              placeholder={chain.results.length > 1 ? "请选择同一 task 的 result" : "等待持久 result"}
              value={selection.backtestResultId}
            />
          </div>
          <button className="primary-button" onClick={onRefresh} type="button">刷新持久结果</button>
          <p className="lab-workbench__hint">
            {selectedResult
              ? `已核对 result=${selectedResult.id}；仅 POST 成功不会到达此状态。`
              : selectedTask
                ? `task=${selectedTask.id} 已选择；等待 BacktestResult。`
                : "选择 run/task 时只显示当前候选链的精确 database IDs。"}
          </p>
        </li>

        <li>
          <div className="lab-workbench__step-title"><span>3</span><h3>导入并评分</h3></div>
          <p>{scoreReason ?? "artifact 与当前任务已对账，可导入并计算评分。"}</p>
          <button
            className="primary-button"
            disabled={Boolean(scoreReason) || busyAction !== null}
            onClick={handleIngest}
            type="button"
          >
            {busyAction === "score" ? "正在导入…" : "导入此任务并评分"}
          </button>
          {chain.scores.length > 1 ? (
            <SelectField
              label="StrategyScore"
              onChange={(value) => select("scoreId", value)}
              options={chain.scores.map((score) => ({ id: score.scoreId, label: `Score ${score.scoreId} · ${score.totalScore.toFixed(1)}` }))}
              placeholder="请选择同一 result 的 score"
              value={selection.scoreId}
            />
          ) : null}
          {selectedScore ? (
            <div className="lab-workbench__score">
              <span>持久评分</span>
              <strong>{selectedScore.totalScore.toFixed(1)}</strong>
              <CopyableValue label="StrategyScore ID" value={selectedScore.scoreId} />
            </div>
          ) : null}
          {selectedScore ? (
            <div className="lab-workbench__score" aria-live="polite">
              <span>Demo 晋级门槛</span>
              {promotionError ? (
                <p className="lab-workbench__blocker">无法复核晋级证据：{promotionError}</p>
              ) : promotion ? (
                <div>
                  <StatusBadge
                    label={promotion.status === "ELIGIBLE" ? "可申请 Demo 审批" : promotion.status}
                    status={promotion.status === "ELIGIBLE" ? "READY" : "BLOCKED"}
                  />
                  <p>{promotion.reason ?? "评分仅通过研究门槛；仍需独立人工审批与风险批准，不能直接交易。"}</p>
                  {promotion.policy ? (
                    <small>
                      policy={promotion.policy.policy_version} · 最低 {promotion.policy.min_total_trades} 笔 · 最大回撤 {Math.round(promotion.policy.max_drawdown_pct * 100)}%
                    </small>
                  ) : null}
                  {promotion.evidence ? (
                    <small>
                      净收益已计成本 · OOS {promotion.evidence.out_of_sample?.total_trades ?? 0} 笔 · 市场状态 {promotion.evidence.walk_forward?.market_states?.join(" / ") ?? "缺失"}
                    </small>
                  ) : null}
                  {promotion.approval ? (
                    <small>
                      审批 #{promotion.approval.database_id}：{promotion.approval.status} · {promotion.approval.reason ?? "无决定原因"}
                    </small>
                  ) : null}
                </div>
              ) : <small>正在从 API 复核 policy、样本外和 walk-forward 证据…</small>}
            </div>
          ) : null}
          <LatestActionFeedback
            actions={["导入回测结果并计算评分"]}
            environmentScope={selectedTask?.dataSource?.environment.scope ?? candidate?.dataSource.environment.scope ?? "unknown"}
            expectedEntityIds={{ backtest_task_id: selectedTask?.id }}
            history={history}
            phase="score"
          />
        </li>
      </ol>

      <aside role="note">本操作台只执行本地回测与结果导入，不会启动 dry-run、live trading 或真实订单。</aside>
    </section>
  );
}
