import type {
  LocalStrategyLabEvidenceStage,
  RankingEntry,
  RankingScoreBreakdownItem,
} from "../api/types";

export type RankingViewKind = "normal" | "empty" | "filtered" | "failed" | "blocked";

export type RankingViewModel = {
  kind: RankingViewKind;
  label: string;
  tone: "success" | "danger" | "warning" | "info" | "neutral";
  summary: string;
  nextAction: string | null;
  entries: RankingEntry[];
};

export function scoreEvidenceStage(
  stages: LocalStrategyLabEvidenceStage[] | undefined,
): LocalStrategyLabEvidenceStage | undefined {
  return stages?.find((stage) => stage.key === "score");
}

export function isAcceptableRankingEntry(
  entry: RankingEntry,
  stage: LocalStrategyLabEvidenceStage | undefined,
): boolean {
  if (!stage?.canAccept || !entry.scoreId || !entry.backtestResultId) {
    return false;
  }
  const ids = entry.dataSource.databaseIds;
  if (
    entry.dataSource.coreData !== true ||
    !["database", "api_aggregate"].includes(entry.dataSource.sourceType) ||
    String(ids.strategy_score_id ?? "") !== entry.scoreId ||
    String(ids.backtest_result_id ?? "") !== entry.backtestResultId
  ) {
    return false;
  }
  return stage.records.some(
    (record) =>
      record.id === entry.scoreId &&
      record.parentId === entry.backtestResultId &&
      record.source.coreData === true &&
      Object.keys(record.source.databaseIds).length > 0,
  );
}

export function buildRankingViewModel({
  entries,
  error,
  scoreStage,
  source,
}: {
  entries: RankingEntry[];
  error: string | null;
  scoreStage: LocalStrategyLabEvidenceStage | undefined;
  source: string;
}): RankingViewModel {
  if (error || source === "failed" || scoreStage?.state === "FAILED") {
    return {
      kind: "failed",
      label: "评分加载失败",
      tone: "danger",
      summary: error ?? scoreStage?.reason ?? "排行榜真实数据不可用。",
      nextAction: scoreStage?.nextAction ?? "恢复 Backend API 并重新核对评分证据。",
      entries: [],
    };
  }

  const acceptedEntries = entries.filter((entry) => isAcceptableRankingEntry(entry, scoreStage));
  if (acceptedEntries.length > 0) {
    return {
      kind: "normal",
      label: "核心排名",
      tone: "success",
      summary: `当前显示 ${acceptedEntries.length} 条具有真实 BacktestResult/Score 持久证据的排名。`,
      nextAction: null,
      entries: acceptedEntries,
    };
  }

  const observedCount = Math.max(entries.length, scoreStage?.observedCount ?? 0);
  if (
    observedCount > 0 &&
    (entries.length > 0 ||
      scoreStage?.state === "NOT_ACCEPTABLE" ||
      (scoreStage?.coreCount ?? 0) === 0)
  ) {
    return {
      kind: "filtered",
      label: "记录已筛除",
      tone: "warning",
      summary: `观察到 ${observedCount} 条评分记录，但没有记录满足真实 BacktestResult/Score 核心证据链。`,
      nextAction: scoreStage?.nextAction ?? "补齐核心来源和数据库 ID 后刷新。",
      entries: [],
    };
  }

  if (scoreStage?.state === "BLOCKED" || scoreStage?.state === "API_GAP") {
    return {
      kind: "blocked",
      label: scoreStage.state === "API_GAP" ? "API 证据缺口" : "评分受阻",
      tone: "warning",
      summary: scoreStage.reason,
      nextAction: scoreStage.nextAction,
      entries: [],
    };
  }

  return {
    kind: "empty",
    label: "暂无真实排名",
    tone: "neutral",
    summary: scoreStage?.reason ?? "尚未观察到关联核心回测结果的评分记录。",
    nextAction: scoreStage?.nextAction ?? "先完成真实回测与评分，再刷新排行榜。",
    entries: [],
  };
}

const SCORE_LABELS: Readonly<Record<string, string>> = {
  profit_score: "收益",
  risk_score: "风险",
  stability_score: "稳定性",
  quality_score: "质量",
};

export function rankingScoreLabel(item: RankingScoreBreakdownItem): string {
  return SCORE_LABELS[item.name] ?? item.name.replace(/_/g, " ");
}

export function rankingConclusion(entry: RankingEntry): {
  label: string;
  tone: "success" | "danger" | "warning";
  summary: string;
  details: string | null;
} {
  const signals = [...entry.elimination.reasons, ...entry.warnings];
  const details =
    signals.length > 0
      ? signals.map((signal) => `${signal.severity.toUpperCase()}：${signal.message}`).join("\n")
      : null;
  if (entry.elimination.eliminated) {
    return {
      label: "已淘汰",
      tone: "danger",
      summary: entry.elimination.reasons[0]?.message ?? "评分规则已将该策略淘汰。",
      details,
    };
  }
  if (entry.warnings.length > 0) {
    return {
      label: "入榜，需关注",
      tone: "warning",
      summary: entry.warnings[0].message,
      details,
    };
  }
  return {
    label: "已入榜",
    tone: "success",
    summary: "当前未记录淘汰原因或评分警告。",
    details: null,
  };
}

export function formatAuditRecord(record: Record<string, number | string>): string {
  const entries = Object.entries(record);
  return entries.length > 0
    ? entries.map(([key, value]) => `${key}: ${value}`).join("\n")
    : "暂无";
}
