import type { LabPhase } from "./workflowState";

export type ActionEvidenceStatus =
  | "IDLE"
  | "RUNNING"
  | "SUCCESS"
  | "FAILED"
  | "BLOCKED"
  | "UNAUTHORIZED"
  | "API_GAP";

export type ActionEvidencePhase = LabPhase | "system";
export type ActionEvidenceEnvironmentScope = "current" | "historical" | "unknown";

export type ActionEvidence = {
  schemaVersion: 2;
  eventId: string;
  lifecycleId: string;
  environmentScope: ActionEvidenceEnvironmentScope;
  phase: ActionEvidencePhase;
  action: string;
  artifactPaths: string[];
  entityIds: Record<string, string>;
  /** @deprecated Kept so v1 consumers can migrate without losing audit context. */
  databaseIds: Record<string, string>;
  message: string;
  nextAction: string;
  recommendBug: boolean;
  repeatCount: number;
  status: ActionEvidenceStatus;
  updatedAt: string;
};

type LegacyActionEvidence = Omit<
  ActionEvidence,
  "schemaVersion" | "eventId" | "lifecycleId" | "environmentScope" | "phase" | "entityIds" | "repeatCount"
>;

export type ActionEvidenceInput = Omit<
  ActionEvidence,
  | "schemaVersion"
  | "eventId"
  | "lifecycleId"
  | "environmentScope"
  | "phase"
  | "artifactPaths"
  | "entityIds"
  | "databaseIds"
  | "repeatCount"
  | "status"
> & {
  artifactPaths?: Array<string | null | undefined>;
  databaseIds?: Record<string, number | string | null | undefined>;
  entityIds?: Record<string, number | string | null | undefined>;
  eventId?: string;
  lifecycleId?: string;
  environmentScope?: ActionEvidenceEnvironmentScope;
  phase?: ActionEvidencePhase;
  status: ActionEvidenceStatus;
};

export type ActionEvidenceHistoryState =
  | "restored-v2"
  | "migrated-v1"
  | "empty"
  | "invalid"
  | "unavailable";

export const ACTION_EVIDENCE_STORAGE_KEY = "freqtrade-ai.local-strategy-lab.action-evidence.v2";
export const LEGACY_ACTION_EVIDENCE_STORAGE_KEY = "freqtrade-ai.local-strategy-lab.action-evidence.v1";
export const ACTION_EVIDENCE_HISTORY_LIMIT = 24;

const MAX_ACTION_LENGTH = 80;
const MAX_MESSAGE_LENGTH = 1_000;
const MAX_NEXT_ACTION_LENGTH = 600;
const MAX_ID_VALUE_LENGTH = 128;
const MAX_PATH_LENGTH = 512;
const MAX_ARTIFACT_PATHS = 8;
const allowedIdKeys = new Set([
  "strategy_generation_run_id",
  "strategy_id",
  "strategy_version_id",
  "backtest_run_id",
  "backtest_task_id",
  "backtest_result_id",
  "strategy_score_id",
]);
const validStatuses = new Set<ActionEvidenceStatus>([
  "IDLE",
  "RUNNING",
  "SUCCESS",
  "FAILED",
  "BLOCKED",
  "UNAUTHORIZED",
  "API_GAP",
]);
const validPhases = new Set<ActionEvidencePhase>([
  "generation",
  "backtest",
  "score",
  "dry-run",
  "system",
]);
const validEnvironmentScopes = new Set<ActionEvidenceEnvironmentScope>([
  "current",
  "historical",
  "unknown",
]);

function redactSensitive(value: string): string {
  return value
    .replace(/<[^>]*>/g, "")
    .replace(/\bBearer\s+[A-Za-z0-9._~+/=-]+/gi, "Bearer [REDACTED]")
    .replace(
      /((?:api[_-]?key|token|authorization|secret|password)\s*[:=]\s*)[^\s,;]+/gi,
      "$1[REDACTED]",
    )
    .replace(/\bsk-[A-Za-z0-9_-]{8,}\b/g, "[REDACTED]")
    .replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/g, "");
}

function safeText(value: unknown, maxLength: number): string {
  if (typeof value !== "string") return "";
  const redacted = redactSensitive(value).trim();
  return redacted.length > maxLength ? `${redacted.slice(0, maxLength - 1)}…` : redacted;
}

function compactIds(
  ids: Record<string, number | string | null | undefined> | undefined,
): Record<string, string> {
  return Object.fromEntries(
    Object.entries(ids ?? {})
      .filter(([key, value]) => allowedIdKeys.has(key) && value !== null && value !== undefined)
      .map(([key, value]) => [key, safeText(String(value), MAX_ID_VALUE_LENGTH)])
      .filter(([, value]) => Boolean(value)),
  );
}

function compactPaths(paths: unknown): string[] {
  if (!Array.isArray(paths)) return [];
  return paths
    .filter((value): value is string => typeof value === "string")
    .map((value) => safeText(value, MAX_PATH_LENGTH))
    .filter(Boolean)
    .slice(0, MAX_ARTIFACT_PATHS);
}

function phaseForAction(action: string): ActionEvidencePhase {
  if (action.includes("生成策略") || action.includes("DeepSeek")) return "generation";
  if (action.includes("回测") && !action.includes("导入")) return "backtest";
  if (action.includes("导入") || action.includes("评分")) return "score";
  if (action.toLowerCase().includes("dry-run")) return "dry-run";
  return "system";
}

function stableLegacyEventId(action: string, updatedAt: string, index = 0): string {
  let hash = 2166136261;
  for (const character of `${action}:${updatedAt}:${index}`) {
    hash ^= character.charCodeAt(0);
    hash = Math.imul(hash, 16777619);
  }
  return `legacy-${(hash >>> 0).toString(36)}`;
}

function randomId(prefix: string): string {
  const value = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
  return `${prefix}-${value}`;
}

export function createActionLifecycleId(phase: ActionEvidencePhase): string {
  return randomId(`lifecycle-${phase}`);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function booleanValue(value: unknown): boolean {
  return value === true;
}

function stringRecord(value: unknown): Record<string, string> {
  if (!isRecord(value)) return {};
  return compactIds(value as Record<string, string | null | undefined>);
}

export function createActionEvidence(input: ActionEvidenceInput): ActionEvidence {
  const action = safeText(input.action, MAX_ACTION_LENGTH);
  const phase = input.phase ?? phaseForAction(action);
  const entityIds = compactIds(input.entityIds ?? input.databaseIds);
  const status =
    input.status === "SUCCESS" && phase !== "system" && Object.keys(entityIds).length === 0
      ? "API_GAP"
      : input.status;
  const eventId = safeText(input.eventId, MAX_ID_VALUE_LENGTH) || randomId("event");
  return {
    action,
    artifactPaths: compactPaths(input.artifactPaths),
    databaseIds: entityIds,
    entityIds,
    environmentScope: input.environmentScope ?? "current",
    eventId,
    lifecycleId:
      safeText(input.lifecycleId, MAX_ID_VALUE_LENGTH) ||
      createActionLifecycleId(phase),
    message: safeText(input.message, MAX_MESSAGE_LENGTH),
    nextAction: safeText(input.nextAction, MAX_NEXT_ACTION_LENGTH),
    phase,
    recommendBug: input.recommendBug,
    repeatCount: 1,
    schemaVersion: 2,
    status,
    updatedAt: safeText(input.updatedAt, 64),
  };
}

function parseV2Entry(value: unknown): ActionEvidence | null {
  if (!isRecord(value)) return null;
  const action = safeText(value.action, MAX_ACTION_LENGTH);
  const updatedAt = safeText(value.updatedAt, 64);
  const rawStatus = safeText(value.status, 32) as ActionEvidenceStatus;
  const rawPhase = safeText(value.phase, 32) as ActionEvidencePhase;
  const rawEnvironmentScope = safeText(value.environmentScope, 32) as ActionEvidenceEnvironmentScope;
  if (
    value.schemaVersion !== 2 ||
    !action ||
    !updatedAt ||
    !validStatuses.has(rawStatus) ||
    !validPhases.has(rawPhase)
  ) {
    return null;
  }
  const entityIds = stringRecord(value.entityIds);
  const eventId =
    safeText(value.eventId, MAX_ID_VALUE_LENGTH) ||
    stableLegacyEventId(action, updatedAt);
  const status =
    rawStatus === "SUCCESS" && rawPhase !== "system" && Object.keys(entityIds).length === 0
      ? "API_GAP"
      : rawStatus;
  return {
    action,
    artifactPaths: compactPaths(value.artifactPaths),
    databaseIds: entityIds,
    entityIds,
    environmentScope: validEnvironmentScopes.has(rawEnvironmentScope) ? rawEnvironmentScope : "unknown",
    eventId,
    lifecycleId: safeText(value.lifecycleId, MAX_ID_VALUE_LENGTH) || eventId,
    message: safeText(value.message, MAX_MESSAGE_LENGTH),
    nextAction: safeText(value.nextAction, MAX_NEXT_ACTION_LENGTH),
    phase: rawPhase,
    recommendBug: booleanValue(value.recommendBug),
    repeatCount:
      typeof value.repeatCount === "number" && Number.isInteger(value.repeatCount) && value.repeatCount > 0
        ? Math.min(value.repeatCount, 999)
        : 1,
    schemaVersion: 2,
    status,
    updatedAt,
  };
}

function migrateLegacyEntry(value: unknown, index: number): ActionEvidence | null {
  if (!isRecord(value)) return null;
  const action = safeText(value.action, MAX_ACTION_LENGTH);
  const updatedAt = safeText(value.updatedAt, 64);
  const status = safeText(value.status, 32) as ActionEvidenceStatus;
  if (!action || !updatedAt || !validStatuses.has(status)) return null;
  const legacy = value as unknown as LegacyActionEvidence;
  const eventId = stableLegacyEventId(action, updatedAt, index);
  return createActionEvidence({
    action,
    artifactPaths: compactPaths(legacy.artifactPaths),
    databaseIds: stringRecord(legacy.databaseIds),
    eventId,
    environmentScope: "unknown",
    lifecycleId: eventId,
    message: safeText(legacy.message, MAX_MESSAGE_LENGTH),
    nextAction: safeText(legacy.nextAction, MAX_NEXT_ACTION_LENGTH),
    phase: phaseForAction(action),
    recommendBug: booleanValue(legacy.recommendBug),
    status,
    updatedAt,
  });
}

function parseArray(raw: string | null): unknown[] | null {
  if (!raw) return [];
  try {
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

export function parseStoredActionEvidence(
  v2Raw: string | null,
  legacyRaw: string | null,
): { history: ActionEvidence[]; state: ActionEvidenceHistoryState } {
  const v2 = parseArray(v2Raw);
  if (v2 === null) return { history: [], state: "invalid" };
  if (v2.length > 0) {
    const history = v2
      .slice(0, ACTION_EVIDENCE_HISTORY_LIMIT)
      .map(parseV2Entry)
      .filter((entry): entry is ActionEvidence => entry !== null);
    return {
      history,
      state: history.length === Math.min(v2.length, ACTION_EVIDENCE_HISTORY_LIMIT) ? "restored-v2" : "invalid",
    };
  }

  const legacy = parseArray(legacyRaw);
  if (legacy === null) return { history: [], state: "invalid" };
  if (legacy.length > 0) {
    const history = legacy
      .slice(0, ACTION_EVIDENCE_HISTORY_LIMIT)
      .map(migrateLegacyEntry)
      .filter((entry): entry is ActionEvidence => entry !== null);
    return {
      history,
      state: history.length === Math.min(legacy.length, ACTION_EVIDENCE_HISTORY_LIMIT) ? "migrated-v1" : "invalid",
    };
  }
  return { history: [], state: "empty" };
}

function sameEntityIds(left: Record<string, string>, right: Record<string, string>): boolean {
  return JSON.stringify(Object.entries(left).sort()) === JSON.stringify(Object.entries(right).sort());
}

function sameArtifactPaths(left: string[], right: string[]): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

function isFoldableRefresh(entry: ActionEvidence): boolean {
  return entry.phase === "system" && entry.action === "刷新数据";
}

function collapseRepeated(
  next: ActionEvidence,
  current: ActionEvidence[],
  limit: number,
): ActionEvidence[] {
  const [latest, ...older] = current;
  if (
    latest &&
    latest.action === next.action &&
    latest.phase === next.phase &&
    latest.status === next.status &&
    isFoldableRefresh(latest) &&
    isFoldableRefresh(next) &&
    sameEntityIds(latest.entityIds, next.entityIds) &&
    latest.message === next.message &&
    latest.nextAction === next.nextAction &&
    sameArtifactPaths(latest.artifactPaths, next.artifactPaths)
  ) {
    return [{
      ...next,
      repeatCount: Math.min(latest.repeatCount + next.repeatCount, 999),
    }, ...older].slice(0, limit);
  }
  return [next, ...current].slice(0, limit);
}

export function recordActionEvidence(
  current: ActionEvidence[],
  next: ActionEvidence,
  requestedLimit = ACTION_EVIDENCE_HISTORY_LIMIT,
): ActionEvidence[] {
  const limit = Math.min(Math.max(requestedLimit, 1), ACTION_EVIDENCE_HISTORY_LIMIT);
  const lifecycleIndex = current.findIndex((entry) => entry.lifecycleId === next.lifecycleId);
  if (lifecycleIndex >= 0) {
    const existing = current[lifecycleIndex];
    const remaining = current.filter((_, index) => index !== lifecycleIndex);
    const entityIds = { ...existing.entityIds, ...next.entityIds };
    return collapseRepeated({
      ...next,
      databaseIds: entityIds,
      entityIds,
      eventId: existing.eventId,
      repeatCount: existing.repeatCount,
    }, remaining, limit);
  }
  return collapseRepeated(next, current, limit);
}

export type LatestActionFeedbackApplicability =
  | "current"
  | "historical"
  | "mismatch"
  | "unknown"
  | "empty";

function findLatestActionEvidence({
  actions,
  history,
  phase,
}: {
  actions?: string[];
  history: ActionEvidence[];
  phase: ActionEvidencePhase;
}): ActionEvidence | null {
  return history.find(
    (candidate) => candidate.phase === phase && (!actions || actions.includes(candidate.action)),
  ) ?? null;
}

export function latestActionEnvironmentScope({
  actions,
  history,
  phase,
}: {
  actions?: string[];
  history: ActionEvidence[];
  phase: ActionEvidencePhase;
}): ActionEvidenceEnvironmentScope {
  return findLatestActionEvidence({ actions, history, phase })?.environmentScope ?? "unknown";
}

export function resolveLatestActionFeedback({
  actions,
  environmentScope,
  expectedEntityIds,
  history,
  phase,
}: {
  actions?: string[];
  environmentScope: ActionEvidenceEnvironmentScope;
  expectedEntityIds?: Record<string, number | string | null | undefined>;
  history: ActionEvidence[];
  phase: ActionEvidencePhase;
}): { applicability: LatestActionFeedbackApplicability; entry: ActionEvidence | null } {
  const entry = findLatestActionEvidence({ actions, history, phase });
  if (!entry) return { applicability: "empty", entry };
  if (environmentScope === "historical" || entry.environmentScope === "historical") {
    return { applicability: "historical", entry };
  }
  if (environmentScope !== "current" || entry.environmentScope !== "current") {
    return { applicability: "unknown", entry };
  }

  const declaredExpected = Object.entries(expectedEntityIds ?? {});
  if (declaredExpected.some(([, value]) => value === null || value === undefined || !String(value).trim())) {
    return { applicability: "unknown", entry };
  }
  const expected = declaredExpected;
  const mismatch = expected.some(([key, value]) => entry.entityIds[key] !== String(value));
  return { applicability: mismatch ? "mismatch" : "current", entry };
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
