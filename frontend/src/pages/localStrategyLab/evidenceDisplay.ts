import type {
  AcceptanceState,
  LocalStrategyLabEvidenceRecord,
  LocalStrategyLabEvidenceSummary,
} from "../../api/types";

export type EvidenceStateDisplay = {
  label: string;
  tone: "success" | "danger" | "warning" | "info" | "neutral";
  emptyTitle: string;
};

const STATE_DISPLAY: Record<AcceptanceState, EvidenceStateDisplay> = {
  ACCEPTABLE: {
    label: "链路可验收",
    tone: "success",
    emptyTitle: "核心证据链完整",
  },
  FAILED: {
    label: "链路失败",
    tone: "danger",
    emptyTitle: "真实流程执行失败",
  },
  BLOCKED: {
    label: "链路受阻",
    tone: "warning",
    emptyTitle: "前置条件未满足",
  },
  NOT_RUN: {
    label: "尚未运行",
    tone: "neutral",
    emptyTitle: "暂无真实运行记录",
  },
  NOT_ACCEPTABLE: {
    label: "不可验收",
    tone: "warning",
    emptyTitle: "仅观察到非核心记录",
  },
  API_GAP: {
    label: "API 证据缺口",
    tone: "warning",
    emptyTitle: "持久证据字段不完整",
  },
};

export function evidenceStateDisplay(state: AcceptanceState): EvidenceStateDisplay {
  return STATE_DISPLAY[state];
}

export function isCoreEvidenceRecord(record: LocalStrategyLabEvidenceRecord): boolean {
  const source = record.source;
  return (
    source.coreData === true &&
    (source.sourceType === "database" || source.sourceType === "api_aggregate") &&
    source.providerProvenance !== "non-core" &&
    source.providerProvenance !== "unknown" &&
    Object.keys(source.databaseIds).length > 0
  );
}

export function partitionEvidenceRecords(summary: LocalStrategyLabEvidenceSummary): {
  core: Array<LocalStrategyLabEvidenceRecord & { stage: string }>;
  diagnostic: Array<LocalStrategyLabEvidenceRecord & { stage: string }>;
} {
  const core: Array<LocalStrategyLabEvidenceRecord & { stage: string }> = [];
  const diagnostic: Array<LocalStrategyLabEvidenceRecord & { stage: string }> = [];

  for (const stage of summary.stages) {
    for (const record of stage.records) {
      const target = isCoreEvidenceRecord(record) ? core : diagnostic;
      target.push({ ...record, stage: stage.label });
    }
  }
  return { core, diagnostic };
}

export function formatTraceEntries(record: Record<string, number | string>): string {
  const entries = Object.entries(record);
  return entries.length > 0
    ? entries.map(([key, value]) => `${key}: ${value}`).join("\n")
    : "暂无";
}
