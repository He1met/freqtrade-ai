import { type FormEvent, useEffect, useMemo, useRef, useState } from "react";

import { StrategyGenerationApiError, createStrategyGenerationRun } from "../api/client";
import { combineDataSources } from "../api/sourceState";
import { useMvpData } from "../api/useMvpData";
import type { StrategyGenerationApiResult } from "../api/types";
import { EMPTY_TEXT } from "./uiCopy";
import {
  type SubmissionState,
  isCoreGenerationResult,
  PersistentEvidence,
  ResultDetails,
  submissionMessage,
  submissionStatus,
} from "./localStrategyLab/EvidencePanels";
import { SubmissionStatusPanel } from "./localStrategyLab/SubmissionStatusPanel";

const DEFAULT_IDEA =
  "Build a local dry-run only RSI mean reversion strategy with conservative risk checks and no live trading assumptions.";
export function LocalStrategyLab() {
  const [idea, setIdea] = useState(DEFAULT_IDEA);
  const requestedCount = 1 as const;
  const [operatorToken, setOperatorToken] = useState("");
  const [authorizeRealProvider, setAuthorizeRealProvider] = useState(false);
  const [submission, setSubmission] = useState<SubmissionState>({ kind: "idle" });
  const [snapshotRefreshToken, setSnapshotRefreshToken] = useState(0);
  const snapshot = useMvpData(snapshotRefreshToken);
  const controllerRef = useRef<AbortController | null>(null);
  const isSubmitting = submission.kind === "submitting";
  const currentStatus = submissionStatus(submission);
  const currentResult = submission.kind === "success" || submission.kind === "blocked" ? submission.result : undefined;

  const statusRows = useMemo<Array<[string, string]>>(
    () => [
      ["状态", currentStatus.title],
      ["核心成功", submission.kind === "success" ? "是" : "否"],
      [
        "run id",
        submission.kind === "success"
          ? submission.result.run.id
          : submission.kind === "blocked"
            ? submission.result?.run.id ?? submission.runId ?? EMPTY_TEXT
          : submission.kind === "failed"
            ? submission.runId ?? EMPTY_TEXT
            : EMPTY_TEXT,
      ],
      [
        "错误 / 阻塞原因",
        submission.kind === "failed" || submission.kind === "blocked" || submission.kind === "unauthorized"
          ? submission.message
          : EMPTY_TEXT,
      ],
    ],
    [currentStatus.title, submission],
  );

  useEffect(() => {
    return () => {
      controllerRef.current?.abort();
    };
  }, []);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const promptSummary = idea.trim();

    if (!promptSummary) {
      setSubmission({
        kind: "blocked",
        message: "Strategy idea 不能为空；未提交 API 请求。",
      });
      return;
    }
    if (!operatorToken) {
      setSubmission({
        kind: "unauthorized",
        message: "请输入本地 operator token；token 只用于本次请求，不会写入页面存储。",
        statusCode: null,
        statusText: null,
      });
      return;
    }

    const controller = new AbortController();
    controllerRef.current?.abort();
    controllerRef.current = controller;
    setSubmission({ kind: "submitting", promptSummary, requestedCount });

    try {
      const result = await createStrategyGenerationRun(
        {
          promptSummary,
          requestedCount,
          operatorToken,
          authorizeRealProvider,
        },
        controller.signal,
      );

      if (isCoreGenerationResult(result)) {
        setSubmission({ kind: "success", result });
        setSnapshotRefreshToken((current) => current + 1);
        return;
      }

      setSubmission({
        kind: "blocked",
        message:
          "Backend 响应缺少可证明的 api_aggregate/database core_data、database_ids 或 strategy file path；未展示为核心成功。",
        result,
      });
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
          return;
        }

        setSubmission({
          kind: "blocked",
          message: `Strategy generation API 不可用或返回非核心响应：${error.message}`,
        });
        return;
      }

      setSubmission({
        kind: "blocked",
        message: error instanceof Error ? error.message : "Strategy generation API 请求失败。",
      });
    }
  }

  return (
    <section className="page local-strategy-lab">
      <header className="page-header">
        <h1>Local Strategy Lab</h1>
        <span className={`run-status ${currentStatus.className}`}>{currentStatus.label}</span>
      </header>

      <aside className="notice lab-safety-notice" role="status">
        本页只提交本地策略生成请求；不连接交易所、不启动 live trading、不下真实订单，也不表示生产就绪。
      </aside>

      <form className="lab-form" onSubmit={handleSubmit}>
        <label className="field-group" htmlFor="strategy-idea">
          <span>Strategy idea</span>
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
            <span>requested_count</span>
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
            <span>operator token</span>
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
            <span>仅授权一次真实 Provider 尝试</span>
          </label>
          <button className="primary-button" disabled={isSubmitting || !idea.trim() || !operatorToken} type="submit">
            {isSubmitting ? "提交中" : "提交生成"}
          </button>
        </div>
      </form>

      <SubmissionStatusPanel
        message={submissionMessage(submission)}
        rows={statusRows}
        status={currentStatus}
        submission={submission}
      />

      <PersistentEvidence
        data={snapshot.data}
        error={snapshot.error}
        isLoading={snapshot.isLoading}
        onRefresh={() => setSnapshotRefreshToken((current) => current + 1)}
        operatorToken={operatorToken}
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
