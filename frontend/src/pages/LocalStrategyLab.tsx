import { type FormEvent, useEffect, useRef, useState } from "react";

import { StrategyGenerationApiError, createStrategyGenerationRun } from "../api/client";
import { combineDataSources } from "../api/sourceState";
import { useMvpData } from "../api/useMvpData";
import type { StrategyGenerationApiResult } from "../api/types";
import { PageHeader, StatusBadge } from "../components/DisplayPrimitives";
import "../styles/local-strategy-lab-submit.css";
import {
  type SubmissionState,
  isCoreGenerationResult,
  PersistentEvidence,
  ResultDetails,
} from "./localStrategyLab/EvidencePanels";
import { SubmissionStatusPanel } from "./localStrategyLab/SubmissionStatusPanel";
import { createActionEvidence } from "./localStrategyLab/actionEvidence";
import { submissionDisplayModel } from "./localStrategyLab/submissionDisplay";
import { useActionEvidence } from "./localStrategyLab/useActionEvidence";

const DEFAULT_IDEA =
  "构建一个仅用于本地 Dry-run 的 RSI 均值回归策略，包含保守风险检查，不假设或启用实盘交易。";
export function LocalStrategyLab() {
  const [idea, setIdea] = useState(DEFAULT_IDEA);
  const requestedCount = 1 as const;
  const [operatorToken, setOperatorToken] = useState("");
  const [authorizeRealProvider, setAuthorizeRealProvider] = useState(false);
  const [submission, setSubmission] = useState<SubmissionState>({ kind: "idle" });
  const [snapshotRefreshToken, setSnapshotRefreshToken] = useState(0);
  const snapshot = useMvpData(snapshotRefreshToken);
  const { history, record: recordAction } = useActionEvidence();
  const controllerRef = useRef<AbortController | null>(null);
  const isSubmitting = submission.kind === "submitting";
  const submissionDisplay = submissionDisplayModel(submission);
  const currentResult = submission.kind === "success" || submission.kind === "blocked" ? submission.result : undefined;

  useEffect(() => {
    return () => {
      controllerRef.current?.abort();
    };
  }, []);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const promptSummary = idea.trim();

    if (!promptSummary) {
      const message = "Strategy idea 不能为空；未提交 API 请求。";
      setSubmission({
        kind: "blocked",
        message,
      });
      recordAction(createActionEvidence({
        action: "生成策略", status: "BLOCKED", message,
        nextAction: "输入本地策略想法后重试。", recommendBug: false, updatedAt: new Date().toISOString(),
      }));
      return;
    }
    if (!operatorToken) {
      const message = "请输入本地 operator token；token 只用于本次请求，不会写入页面存储。";
      setSubmission({
        kind: "unauthorized",
        message,
        statusCode: null,
        statusText: null,
      });
      recordAction(createActionEvidence({
        action: "生成策略", status: "UNAUTHORIZED", message,
        nextAction: "提供本地 operator token 后重试；不要在页面或日志中保存 token。", recommendBug: false,
        updatedAt: new Date().toISOString(),
      }));
      return;
    }

    const controller = new AbortController();
    controllerRef.current?.abort();
    controllerRef.current = controller;
    setSubmission({ kind: "submitting", promptSummary, requestedCount });
    const authorizeThisRequest = authorizeRealProvider;
    setAuthorizeRealProvider(false);
    recordAction(createActionEvidence({
      action: "生成策略", status: "RUNNING", message: "正在提交本地策略生成请求。",
      nextAction: "等待 backend API/DB 响应。", recommendBug: false, updatedAt: new Date().toISOString(),
    }));

    try {
      const result = await createStrategyGenerationRun(
        {
          promptSummary,
          requestedCount,
          operatorToken,
          authorizeRealProvider: authorizeThisRequest,
        },
        controller.signal,
      );

      if (isCoreGenerationResult(result)) {
        setSubmission({ kind: "success", result });
        recordAction(createActionEvidence({
          action: "生成策略", status: "SUCCESS", message: "生成记录、策略版本和文件路径均已由 API/DB 返回。",
          nextAction: "刷新并核对 generation run、strategy version 与后续回测证据。", recommendBug: false,
          databaseIds: { strategy_generation_run_id: result.run.id },
          artifactPaths: result.strategyVersions.map((version) => version.filePath), updatedAt: new Date().toISOString(),
        }));
        setSnapshotRefreshToken((current) => current + 1);
        return;
      }

      setSubmission({
        kind: "blocked",
        message:
          "Backend 响应缺少可证明的 api_aggregate/database core_data、database_ids 或 strategy file path；未展示为核心成功。",
        result,
      });
      recordAction(createActionEvidence({
        action: "生成策略", status: "BLOCKED",
        message: "Backend 响应缺少可证明的核心 API/DB 证据或策略文件路径。",
        nextAction: "检查 data_source、database_ids 和策略文件；不要将其视为核心成功。", recommendBug: false,
        databaseIds: { strategy_generation_run_id: result.run.id }, updatedAt: new Date().toISOString(),
      }));
      setSnapshotRefreshToken((current) => current + 1);
    } catch (error) {
      if (error instanceof Error && error.name === "AbortError") {
        return;
      }

      if (error instanceof StrategyGenerationApiError) {
        if (error.operationStatus === "UNAUTHORIZED") {
          setSubmission({
            kind: "unauthorized",
            message: error.message,
            statusCode: error.status,
            statusText: error.statusText,
          });
          recordAction(createActionEvidence({
            action: "生成策略", status: "UNAUTHORIZED", message: error.message,
            nextAction: "核对本地 operator authorization 后重试。", recommendBug: false, updatedAt: new Date().toISOString(),
          }));
          return;
        }
        if (error.operationStatus === "BLOCKED") {
          setSubmission({
            kind: "blocked",
            message: error.message,
            runId: error.strategyGenerationRunId,
            statusCode: error.status,
            statusText: error.statusText,
          });
          recordAction(createActionEvidence({
            action: "生成策略", status: "BLOCKED", message: error.message,
            nextAction: "按持久 run 的 BLOCKED 原因补齐前置条件后重试。", recommendBug: false,
            databaseIds: { strategy_generation_run_id: error.strategyGenerationRunId }, updatedAt: new Date().toISOString(),
          }));
          return;
        }
        const hasPersistedFailedRun = Boolean(error.strategyGenerationRunId || error.failedReason);
        if (hasPersistedFailedRun) {
          setSubmission({
            kind: "failed",
            message: error.failedReason ?? error.message,
            runId: error.strategyGenerationRunId,
            statusCode: error.status,
            statusText: error.statusText,
          });
          recordAction(createActionEvidence({
            action: "生成策略", status: "FAILED", message: error.failedReason ?? error.message,
            nextAction: "检查持久 generation run 和 Provider/验证错误；若可稳定复现，创建 Bug Issue。", recommendBug: true,
            databaseIds: { strategy_generation_run_id: error.strategyGenerationRunId }, updatedAt: new Date().toISOString(),
          }));
          return;
        }

        setSubmission({
          kind: "blocked",
          message: `Strategy generation API 不可用或返回非核心响应：${error.message}`,
        });
        recordAction(createActionEvidence({
          action: "生成策略", status: "BLOCKED", message: `Strategy generation API 不可用或返回非核心响应：${error.message}`,
          nextAction: "恢复 API 或补齐核心证据后重试。", recommendBug: true, updatedAt: new Date().toISOString(),
        }));
        return;
      }

      const message = error instanceof Error ? error.message : "Strategy generation API 请求失败。";
      setSubmission({
        kind: "blocked",
        message,
      });
      recordAction(createActionEvidence({
        action: "生成策略", status: "FAILED", message,
        nextAction: "检查 API 与网络错误；若可稳定复现，创建 Bug Issue。", recommendBug: true, updatedAt: new Date().toISOString(),
      }));
    }
  }

  function handleCancel() {
    controllerRef.current?.abort();
    controllerRef.current = null;
    const message = "已取消等待本次请求；尚未确认是否存在持久生成记录。";
    setSubmission({ kind: "blocked", message });
    recordAction(createActionEvidence({
      action: "生成策略",
      status: "BLOCKED",
      message,
      nextAction: "刷新下方持久证据；确认没有进行中的记录后再决定是否重试。",
      recommendBug: false,
      updatedAt: new Date().toISOString(),
    }));
  }

  return (
    <section className="page local-strategy-lab">
      <PageHeader
        description="输入策略约束并提交本地生成请求；只有完整 API/DB 持久证据才会显示为成功。"
        status={
          <StatusBadge
            label={submissionDisplay.label}
            showRaw
            status={submissionDisplay.status}
            tone={submissionDisplay.tone}
          />
        }
        title="本地策略实验室（Local Strategy Lab）"
      />

      <aside className="notice lab-safety-notice" role="status">
        本页只提交本地策略生成请求；不连接交易所、不启动 live trading、不下真实订单，也不表示生产就绪。
      </aside>

      <form className="lab-form" onSubmit={handleSubmit}>
        <label className="field-group" htmlFor="strategy-idea">
          <span>策略构想（Strategy idea）</span>
          <small>描述入场、退出、风险和运行边界；不要粘贴 API key、token 或其他凭据。</small>
          <textarea
            id="strategy-idea"
            maxLength={4000}
            minLength={1}
            onChange={(event) => setIdea(event.currentTarget.value)}
            required
            rows={7}
            value={idea}
          />
        </label>
        <div className="lab-form-actions">
          <label className="field-group compact-field" htmlFor="requested-count">
            <span>请求数量（requested_count）</span>
            <small>当前单次固定生成 1 个策略。</small>
            <input
              disabled
              id="requested-count"
              max={1}
              min={1}
              type="number"
              value={requestedCount}
            />
          </label>
          <label className="field-group compact-field" htmlFor="operator-token">
            <span>操作授权令牌（operator token）</span>
            <small>仅用于本地请求；页面不回显，也不写入浏览器存储。</small>
            <input
              autoComplete="off"
              id="operator-token"
              onChange={(event) => setOperatorToken(event.currentTarget.value)}
              required
              type="password"
              value={operatorToken}
            />
          </label>
          <label className="inline-check" htmlFor="provider-authorization">
            <input
              checked={authorizeRealProvider}
              id="provider-authorization"
              onChange={(event) => setAuthorizeRealProvider(event.currentTarget.checked)}
              type="checkbox"
            />
            <span>
              授权本次请求尝试真实 Provider
              <small>提交后自动取消勾选；不授权交易、Dry-run 或实盘操作。</small>
            </span>
          </label>
          <button
            aria-busy={isSubmitting}
            className="primary-button"
            disabled={isSubmitting || !idea.trim() || !operatorToken}
            type="submit"
          >
            {isSubmitting ? "提交中" : "提交生成"}
          </button>
          {isSubmitting ? (
            <button className="secondary-button" onClick={handleCancel} type="button">
              取消等待
            </button>
          ) : null}
        </div>
        <p className="lab-submit-timeout-note">
          请求取消或网络超时不会显示为成功；请刷新下方证据区核对是否已经产生持久记录。
        </p>
      </form>

      <SubmissionStatusPanel submission={submission} />

      <PersistentEvidence
        data={snapshot.data}
        error={snapshot.error}
        history={history}
        isLoading={snapshot.isLoading}
        onRefresh={() => setSnapshotRefreshToken((current) => current + 1)}
        operatorToken={operatorToken}
        promptSummary={idea.trim()}
        recordAction={recordAction}
        source={combineDataSources(snapshot.sources, [
          "strategyVersions",
          "generationRuns",
          "backtestTasks",
          "backtestResults",
          "ranking",
        ])}
      />

      {currentResult ? <ResultDetails result={currentResult} /> : null}
    </section>
  );
}
