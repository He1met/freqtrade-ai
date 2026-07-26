import type {
  BacktestResultSummary,
  DataSourceTraceSummary,
  LocalStrategyLabEvidenceSummary,
  MvpData,
} from "../../api/types";
import { isCoreDataSourceTrace } from "../../api/sourceState.ts";
import { EMPTY_TEXT } from "../uiCopy.ts";

export const EVIDENCE_TABS = ["generation", "versions", "backtests", "scores"] as const;
export type EvidenceTab = (typeof EVIDENCE_TABS)[number];
export type EvidenceScope = "current" | "diagnostic";

export type EvidenceListRecord = {
  id: string;
  tab: EvidenceTab;
  title: string;
  subtitle: string;
  status: string;
  decisionFields: Array<{ label: string; value: string }>;
  databaseIds: Record<string, number>;
  artifactRefs: Record<string, string>;
  source: DataSourceTraceSummary | undefined;
  error: string | null;
  currentCore: boolean;
  related: {
    strategyVersionId?: string | null;
    backtestRunId?: string | null;
    backtestTaskId?: string | null;
    backtestResultId?: string | null;
    scoreId?: string | null;
  };
};

export const EVIDENCE_TAB_LABELS: Record<EvidenceTab, string> = {
  generation: "生成记录",
  versions: "策略版本",
  backtests: "回测结果",
  scores: "评分",
};

export function isEvidenceTab(value: string | null): value is EvidenceTab {
  return EVIDENCE_TABS.includes(value as EvidenceTab);
}

export function isEvidenceScope(value: string | null): value is EvidenceScope {
  return value === "current" || value === "diagnostic";
}

export function isCurrentCoreEvidence(source: DataSourceTraceSummary | undefined): boolean {
  return Boolean(
    isCoreDataSourceTrace(source) &&
      source?.environment.scope === "current" &&
      source.environment.runnable === true,
  );
}

function exactCurrentCoreSource(
  source: DataSourceTraceSummary | undefined,
  sourceType: "database" | "api_aggregate",
  key: string,
  value: string,
): boolean {
  return Boolean(
    isCurrentCoreEvidence(source) &&
      source?.sourceType === sourceType &&
      String(source.databaseIds[key] ?? "") === value,
  );
}

function formatMetric(value: number | null, suffix = ""): string {
  return value === null ? EMPTY_TEXT : `${value.toFixed(2)}${suffix}`;
}

function resultFields(result: BacktestResultSummary): EvidenceListRecord["decisionFields"] {
  return [
    { label: "收益", value: formatMetric(result.metrics.profitPct, "%") },
    { label: "最大回撤", value: formatMetric(result.metrics.maxDrawdownPct, "%") },
    { label: "胜率", value: result.metrics.winRate === null ? EMPTY_TEXT : formatMetric(result.metrics.winRate * 100, "%") },
    { label: "交易数", value: result.metrics.totalTrades === null ? EMPTY_TEXT : String(result.metrics.totalTrades) },
  ];
}

export function buildEvidenceRecords(data: MvpData, tab: EvidenceTab): EvidenceListRecord[] {
  const evidence = data.localStrategyLabEvidenceData ?? data;
  const versionById = new Map(evidence.strategyVersions.map((version) => [version.id, version]));
  const runById = new Map(evidence.backtestRuns.map((run) => [run.id, run]));
  const taskById = new Map(evidence.backtestTasks.map((task) => [task.id, task]));
  const resultById = new Map(evidence.backtestResults.map((result) => [result.id, result]));
  const isCurrentVersion = (versionId: string | null | undefined) => {
    if (!versionId) return false;
    const version = versionById.get(versionId);
    return Boolean(
      version &&
        exactCurrentCoreSource(version.dataSource, "database", "strategy_version_id", versionId),
    );
  };
  const isCurrentRun = (runId: string, strategyVersionId?: string | null) => {
    const run = runById.get(runId);
    return Boolean(
      run &&
        run.strategyVersionId &&
        (!strategyVersionId || run.strategyVersionId === strategyVersionId) &&
        isCurrentVersion(run.strategyVersionId) &&
        exactCurrentCoreSource(run.dataSource, "database", "backtest_run_id", runId) &&
        String(run.dataSource?.databaseIds.strategy_version_id ?? "") === run.strategyVersionId,
    );
  };
  const isCurrentTask = (taskId: string, runId: string) => {
    const task = taskById.get(taskId);
    return Boolean(
      task &&
        task.runId === runId &&
        isCurrentRun(runId) &&
        exactCurrentCoreSource(task.dataSource, "database", "backtest_task_id", taskId) &&
        String(task.dataSource?.databaseIds.backtest_run_id ?? "") === runId,
    );
  };
  const isCurrentResult = (resultId: string) => {
    const result = resultById.get(resultId);
    return Boolean(
      result &&
        isCurrentTask(result.taskId, result.runId) &&
        exactCurrentCoreSource(result.dataSource, "database", "backtest_result_id", resultId) &&
        String(result.dataSource.databaseIds.backtest_run_id ?? "") === result.runId &&
        String(result.dataSource.databaseIds.backtest_task_id ?? "") === result.taskId,
    );
  };
  if (tab === "generation") {
    return evidence.generationRuns.map((run) => ({
      id: run.id,
      tab,
      title: `${run.provider} / ${run.model}`,
      subtitle: `requested ${run.requestedCount} · accepted ${run.acceptedCount} · failed ${run.failedCount}`,
      status: run.status,
      decisionFields: [
        { label: "生成", value: String(run.generatedCount) },
        { label: "通过", value: String(run.acceptedCount) },
        { label: "失败", value: String(run.failedCount) },
      ],
      databaseIds: run.dataSource?.databaseIds ?? {},
      artifactRefs: run.dataSource?.artifactRefs ?? {},
      source: run.dataSource,
      error: run.errorMessage,
      currentCore: exactCurrentCoreSource(run.dataSource, "database", "strategy_generation_run_id", run.id),
      related: {},
    }));
  }
  if (tab === "versions") {
    const strategyById = new Map(data.strategies.map((strategy) => [strategy.id, strategy]));
    return evidence.strategyVersions.map((version) => ({
      id: version.id,
      tab,
      title: strategyById.get(version.strategyId)?.name ?? `Strategy ${version.strategyId}`,
      subtitle: `v${version.versionNumber} · ${version.validationStatus}`,
      status: version.fileState?.status ?? version.validationStatus,
      decisionFields: [
        { label: "版本", value: `v${version.versionNumber}` },
        { label: "验证", value: version.validationStatus },
        { label: "文件", value: version.fileState?.exists ? "存在" : "不可用" },
      ],
      databaseIds: version.dataSource.databaseIds,
      artifactRefs: {
        ...version.dataSource.artifactRefs,
        ...(version.filePath ? { strategy_file_path: version.filePath } : {}),
      },
      source: version.dataSource,
      error: version.fileState?.blockedReason ?? (version.validationErrors.map((item) => item.message).join("\n") || null),
      currentCore: exactCurrentCoreSource(version.dataSource, "database", "strategy_version_id", version.id),
      related: { strategyVersionId: version.id },
    }));
  }
  if (tab === "backtests") {
    return evidence.backtestResults.map((result) => {
      const task = taskById.get(result.taskId);
      const run = runById.get(result.runId);
      const currentCore = isCurrentResult(result.id);
      return {
        id: result.id,
        tab,
        title: task ? `${task.pair} · ${task.timeframe}` : `Result ${result.id}`,
        subtitle: run?.strategyName ?? task?.strategyName ?? "策略名称不可用",
        status: task?.status ?? "API_GAP",
        decisionFields: resultFields(result),
        databaseIds: result.dataSource.databaseIds,
        artifactRefs: {
          ...result.dataSource.artifactRefs,
          ...(result.resultPath ? { result_path: result.resultPath } : {}),
        },
        source: result.dataSource,
        error: task?.failedReason ?? task?.blockedReason ?? task?.errorMessage ?? (!task ? "缺少关联 BacktestTask，不能证明回测成功。" : null),
        currentCore,
        related: {
          strategyVersionId: run?.strategyVersionId,
          backtestRunId: result.runId,
          backtestTaskId: result.taskId,
          backtestResultId: result.id,
        },
      };
    });
  }
  return evidence.ranking.map((score) => {
    const result = score.backtestResultId ? resultById.get(score.backtestResultId) : undefined;
    const run = result ? runById.get(result.runId) : undefined;
    const currentCore =
      score.backtestResultId !== null &&
      exactCurrentCoreSource(score.dataSource, "api_aggregate", "strategy_score_id", score.scoreId) &&
      String(score.dataSource.databaseIds.strategy_version_id ?? "") === score.strategyVersionId &&
      String(score.dataSource.databaseIds.backtest_result_id ?? "") === score.backtestResultId &&
      isCurrentResult(score.backtestResultId) &&
      run?.strategyVersionId === score.strategyVersionId &&
      isCurrentVersion(score.strategyVersionId);
    return {
      id: score.scoreId,
      tab,
      title: score.strategyName,
      subtitle: `rank ${score.rank} · v${score.versionNumber}`,
      status: score.elimination.eliminated ? "ELIMINATED" : "RANKED",
      decisionFields: [
        { label: "总分", value: formatMetric(score.totalScore) },
        { label: "收益分", value: formatMetric(score.profitScore) },
        { label: "风险分", value: formatMetric(score.riskScore) },
        { label: "质量分", value: formatMetric(score.qualityScore) },
      ],
      databaseIds: score.dataSource.databaseIds,
      artifactRefs: {
        ...score.dataSource.artifactRefs,
        ...(score.filePath ? { strategy_file_path: score.filePath } : {}),
      },
      source: score.dataSource,
      error: [
        ...score.elimination.reasons.map((item) => item.message),
        ...score.warnings.map((item) => item.message),
      ].join("\n") || null,
      currentCore,
      related: {
        strategyVersionId: score.strategyVersionId,
        backtestRunId: result?.runId,
        backtestTaskId: result?.taskId,
        backtestResultId: score.backtestResultId,
        scoreId: score.scoreId,
      },
    };
  });
}

export function partitionBrowserRecords(records: EvidenceListRecord[]): {
  current: EvidenceListRecord[];
  diagnostic: EvidenceListRecord[];
} {
  return records.reduce(
    (result, record) => {
      result[record.currentCore ? "current" : "diagnostic"].push(record);
      return result;
    },
    { current: [], diagnostic: [] } as {
      current: EvidenceListRecord[];
      diagnostic: EvidenceListRecord[];
    },
  );
}

export function evidenceBrowserEmptyState(
  summary: LocalStrategyLabEvidenceSummary | undefined,
  tab: EvidenceTab,
  scope: EvidenceScope,
  allRecords: EvidenceListRecord[],
): { state: string; title: string; detail: string } {
  const stageKey = tab === "versions" ? "strategy_file" : tab === "backtests" ? "backtest" : tab === "scores" ? "score" : "generation";
  const stage = summary?.stages.find((item) => item.key === stageKey);
  if (scope === "diagnostic") {
    return {
      state: "EMPTY",
      title: "没有非核心诊断记录",
      detail: "当前标签没有 historical、fixture、filtered 或来源不完整的只读记录。",
    };
  }
  if (allRecords.length > 0) {
    return {
      state: "FILTERED",
      title: "核心记录被来源契约过滤",
      detail: "已观察到记录，但它们不是 current + runnable + core；可切换“诊断”查看，不能作为候选。",
    };
  }
  return {
    state: stage?.state ?? "NOT_RUN",
    title:
      stage?.state === "API_GAP"
        ? "API 证据缺口"
        : stage?.state === "BLOCKED"
          ? "真实流程受阻"
          : stage?.state === "FAILED"
            ? "真实流程失败"
            : "尚未运行",
    detail: stage ? `${stage.reason} 下一步：${stage.nextAction}` : "API 未返回该类持久记录。",
  };
}
