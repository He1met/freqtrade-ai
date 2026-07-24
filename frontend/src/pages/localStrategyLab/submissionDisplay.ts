import type {
  DataSourceTraceSummary,
  StrategyGenerationApiResult,
} from "../../api/types";

export type SubmissionDisplayState =
  | { kind: "idle" }
  | { kind: "submitting"; promptSummary: string; requestedCount: number }
  | { kind: "success"; result: StrategyGenerationApiResult }
  | { kind: "unauthorized"; message: string; statusCode: number | null; statusText: string | null }
  | {
      kind: "blocked";
      message: string;
      result?: StrategyGenerationApiResult;
      runId?: string | null;
      statusCode?: number | null;
      statusText?: string | null;
    }
  | {
      kind: "failed";
      message: string;
      runId: string | null;
      statusCode: number | null;
      statusText: string | null;
    };

export type SubmissionDisplayStatus =
  | "IDLE"
  | "RUNNING"
  | "SUCCESS"
  | "FAILED"
  | "BLOCKED"
  | "UNAUTHORIZED"
  | "API_GAP";

export type SubmissionDisplayModel = {
  status: SubmissionDisplayStatus;
  label: string;
  tone: "success" | "danger" | "warning" | "info" | "neutral";
  title: string;
  summary: string;
  nextAction: string;
  runId: string | null;
  strategyCount: number;
  versionCount: number;
  statusCode: number | null;
  statusText: string | null;
  detail: string | null;
};

function isCoreSource(source: DataSourceTraceSummary, allowedType: "database" | "api_aggregate"): boolean {
  return (
    source.sourceType === allowedType &&
    source.coreData === true &&
    source.providerProvenance !== "non-core" &&
    source.providerProvenance !== "unknown" &&
    Object.keys(source.databaseIds).length > 0
  );
}

export function hasPersistentGenerationEvidence(result: StrategyGenerationApiResult): boolean {
  const hasStrategy = result.strategies.some(
    (strategy) => Boolean(strategy.id) && isCoreSource(strategy.dataSource, "database"),
  );
  const hasVersion = result.strategyVersions.some(
    (version) =>
      Boolean(version.id) &&
      Boolean(version.filePath) &&
      isCoreSource(version.dataSource, "database"),
  );
  return (
    result.run.status === "succeeded" &&
    Boolean(result.run.id) &&
    isCoreSource(result.dataSource, "api_aggregate") &&
    isCoreSource(result.run.dataSource, "database") &&
    hasStrategy &&
    hasVersion
  );
}

function resultFields(result: StrategyGenerationApiResult | undefined) {
  return {
    runId: result?.run.id || null,
    strategyCount: result?.strategies.length ?? 0,
    versionCount: result?.strategyVersions.length ?? 0,
  };
}

export function submissionDisplayModel(submission: SubmissionDisplayState): SubmissionDisplayModel {
  if (submission.kind === "idle") {
    return {
      status: "IDLE",
      label: "等待提交",
      tone: "neutral",
      title: "尚未提交策略生成请求",
      summary: "填写策略构想和本地操作授权后再提交。",
      nextAction: "确认策略约束、请求数量和授权范围。",
      runId: null,
      strategyCount: 0,
      versionCount: 0,
      statusCode: null,
      statusText: null,
      detail: null,
    };
  }

  if (submission.kind === "submitting") {
    return {
      status: "RUNNING",
      label: "提交中",
      tone: "info",
      title: "正在等待 Backend API 响应",
      summary: `正在提交 ${submission.requestedCount} 个本地策略生成请求，尚未确认持久结果。`,
      nextAction: "等待请求完成；需要中止时使用“取消等待”。",
      runId: null,
      strategyCount: 0,
      versionCount: 0,
      statusCode: null,
      statusText: null,
      detail: null,
    };
  }

  if (submission.kind === "success") {
    const result = resultFields(submission.result);
    if (!hasPersistentGenerationEvidence(submission.result)) {
      return {
        status: "API_GAP",
        label: "持久证据缺失",
        tone: "warning",
        title: "响应不能证明生成成功",
        summary: "Backend 已响应，但缺少完整 API/DB ID、核心来源或策略文件证据。",
        nextAction: "核对 data_source、database_ids 和策略文件；不要将该响应视为成功。",
        ...result,
        statusCode: null,
        statusText: null,
        detail: "提交状态为 success，但持久证据校验未通过。",
      };
    }
    return {
      status: "SUCCESS",
      label: "持久成功",
      tone: "success",
      title: "生成记录和策略版本已持久化",
      summary: `已确认 1 个生成记录、${result.strategyCount} 个策略和 ${result.versionCount} 个策略版本。`,
      nextAction: "刷新证据并继续校验、回测和人工复核。",
      ...result,
      statusCode: null,
      statusText: null,
      detail: null,
    };
  }

  if (submission.kind === "unauthorized") {
    return {
      status: "UNAUTHORIZED",
      label: "授权失败",
      tone: "warning",
      title: "本地操作授权未通过",
      summary: "请求未获得有效的本地 operator 授权。",
      nextAction: "核对本地 operator token 后重试；不要在页面、日志或 Issue 中记录 token。",
      runId: null,
      strategyCount: 0,
      versionCount: 0,
      statusCode: submission.statusCode,
      statusText: submission.statusText,
      detail: submission.message,
    };
  }

  if (submission.kind === "failed") {
    return {
      status: "FAILED",
      label: "生成失败",
      tone: "danger",
      title: "Backend 返回失败状态",
      summary: submission.runId ? "失败结果已关联持久生成记录。" : "请求失败，未确认持久生成记录。",
      nextAction: "检查 Provider、验证错误和持久记录；稳定复现后创建 Bug Issue。",
      runId: submission.runId,
      strategyCount: 0,
      versionCount: 0,
      statusCode: submission.statusCode,
      statusText: submission.statusText,
      detail: submission.message,
    };
  }

  const result = resultFields(submission.result);
  const isApiGap =
    Boolean(submission.result) ||
    /API 不可用|非核心响应|core_data|database_ids|strategy file path/i.test(submission.message);

  return {
    status: isApiGap ? "API_GAP" : "BLOCKED",
    label: isApiGap ? "持久证据缺失" : "提交受阻",
    tone: "warning",
    title: isApiGap ? "响应不能证明生成成功" : "前置条件未满足",
    summary: isApiGap
      ? "当前响应缺少完整、可核验的 API/DB 持久证据。"
      : "请求未进入可确认成功的生成流程。",
    nextAction: isApiGap
      ? "补齐核心来源、database_ids 和策略文件证据后重试。"
      : "根据阻塞原因补齐前置条件后重试。",
    ...result,
    runId: result.runId ?? submission.runId ?? null,
    statusCode: submission.statusCode ?? null,
    statusText: submission.statusText ?? null,
    detail: submission.message,
  };
}
