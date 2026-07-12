export type ActionEvidenceStatus = "IDLE" | "RUNNING" | "SUCCESS" | "FAILED" | "BLOCKED" | "UNAUTHORIZED" | "API_GAP";

export type ActionEvidence = {
  action: string;
  artifactPaths: string[];
  databaseIds: Record<string, string>;
  message: string;
  nextAction: string;
  recommendBug: boolean;
  status: ActionEvidenceStatus;
  updatedAt: string;
};

export type ActionEvidenceInput = Omit<ActionEvidence, "artifactPaths" | "databaseIds"> & {
  artifactPaths?: Array<string | null | undefined>;
  databaseIds?: Record<string, number | string | null | undefined>;
};

export const ACTION_EVIDENCE_STORAGE_KEY = "freqtrade-ai.local-strategy-lab.action-evidence.v1";

export function createActionEvidence(input: ActionEvidenceInput): ActionEvidence {
  return {
    ...input,
    artifactPaths: (input.artifactPaths ?? []).filter((value): value is string => Boolean(value?.trim())),
    databaseIds: Object.fromEntries(
      Object.entries(input.databaseIds ?? {})
        .filter(([, value]) => value !== null && value !== undefined && String(value).trim())
        .map(([key, value]) => [key, String(value)]),
    ),
  };
}

export function recordActionEvidence(current: ActionEvidence[], next: ActionEvidence, limit = 12): ActionEvidence[] {
  return [next, ...current.filter((item) => item.action !== next.action)].slice(0, limit);
}

export function actionStatusClassName(status: ActionEvidenceStatus): string {
  if (status === "SUCCESS") return "status-success";
  if (status === "FAILED") return "status-failed";
  if (status === "BLOCKED" || status === "UNAUTHORIZED" || status === "API_GAP") return "status-blocked";
  return "status-neutral";
}

export function actionStatusMessage(status: ActionEvidenceStatus): string {
  if (status === "RUNNING") return "请求正在执行；完成后会保留本次可复核摘要。";
  if (status === "SUCCESS") return "请求完成；请通过下方 API/DB 持久证据再次对账。";
  if (status === "FAILED") return "请求失败；请查看失败原因和已返回的持久证据。";
  if (status === "BLOCKED") return "请求被安全或前置条件阻止；未将其展示为成功。";
  if (status === "UNAUTHORIZED") return "本地 operator 授权未通过；请求没有被当作成功处理。";
  if (status === "API_GAP") return "后端没有返回完成对账所需字段；请作为 API gap 处理。";
  return "尚未发起请求。";
}
