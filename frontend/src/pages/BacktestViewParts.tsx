import type {
  BacktestArtifactManifest,
  BacktestResultSummary,
  DataSourceTraceSummary,
} from "../api/types";
import {
  CompactText,
  CopyableValue,
  ExpandableText,
  StatusBadge,
} from "../components/DisplayPrimitives";
import { EMPTY_TEXT, displayBoolean, displayStatus, displayValue } from "./uiCopy";
import {
  backtestResultState,
  matrixStatusLabel,
  metricRows,
  reasonText,
  summarizeText,
} from "./backtestDisplay";

function formatRecord(record: Record<string, number | string> | undefined): string {
  const entries = Object.entries(record ?? {});
  return entries.length > 0
    ? entries.map(([key, value]) => `${key}: ${value}`).join(", ")
    : EMPTY_TEXT;
}

export function BacktestResultMetrics({
  result,
  status,
}: {
  result: BacktestResultSummary | null;
  status: string;
}) {
  const state = backtestResultState(status, Boolean(result));

  if (!result) {
    const badgeStatus =
      state === "RESULT_MISSING"
        ? "not_acceptable"
        : state === "PENDING"
          ? "running"
          : state.toLowerCase();
    return (
      <div className="backtest-result-summary backtest-result-missing" data-result-state={state}>
        <StatusBadge label={matrixStatusLabel(state)} status={badgeStatus} />
        <span>
          {state === "RESULT_MISSING"
            ? "执行状态已成功，但没有关联的核心 BacktestResult，指标不可验收。"
            : "当前没有关联的核心 BacktestResult，不展示任务或批次自带指标。"}
        </span>
      </div>
    );
  }

  return (
    <div className="backtest-result-summary" data-result-state={state}>
      <div className="backtest-result-heading">
        <StatusBadge label="结果可验收" status="acceptable" />
        <span>BacktestResult #{result.id}</span>
      </div>
      <dl className="backtest-metric-grid">
        {metricRows(result.metrics).map(([label, value]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

export function BacktestTechnicalDetails({
  artifact,
  configPath,
  id,
  reason,
  resultPath,
  source,
  status,
}: {
  artifact: BacktestArtifactManifest | null;
  configPath: string | null;
  id: string;
  reason: string;
  resultPath: string | null;
  source: DataSourceTraceSummary | undefined;
  status: string;
}) {
  const hasReason = reason !== EMPTY_TEXT;
  const artifactReason = reasonText(artifact?.blockedReason ?? null, artifact?.failedReason ?? null);

  return (
    <details className="backtest-technical-details">
      <summary>{hasReason ? "查看原因与技术详情" : "查看技术详情"}</summary>
      <div className="backtest-technical-content">
        {hasReason ? (
          <section className="backtest-detail-section">
            <h3>原因</h3>
            <ExpandableText summary="展开完整原因" value={reason} />
          </section>
        ) : null}
        <dl className="backtest-detail-grid">
          <div>
            <dt>记录 ID</dt>
            <dd><CopyableValue label="记录 ID" value={id} /></dd>
          </div>
          <div>
            <dt>原始状态</dt>
            <dd>{displayStatus(status)}（{status}）</dd>
          </div>
          <div>
            <dt>Artifact 状态</dt>
            <dd>{displayStatus(artifact?.status)}</dd>
          </div>
          <div>
            <dt>返回码</dt>
            <dd>{displayValue(artifact?.returnCode)}</dd>
          </div>
          <div>
            <dt>数据源</dt>
            <dd>
              <CompactText
                label="数据源详情"
                value={[
                  `source_type: ${source?.sourceType ?? "unknown"}`,
                  `core_data: ${displayBoolean(source?.coreData)}`,
                  `database_ids: ${formatRecord(source?.databaseIds)}`,
                  `artifact_refs: ${formatRecord(source?.artifactRefs)}`,
                  `detail: ${source?.sourceDetail ?? EMPTY_TEXT}`,
                ].join(" | ")}
              />
            </dd>
          </div>
          <div>
            <dt>配置路径</dt>
            <dd><CopyableValue label="配置路径" value={configPath} /></dd>
          </div>
          <div>
            <dt>Manifest 路径</dt>
            <dd><CopyableValue label="Manifest 路径" value={artifact?.manifestPath} /></dd>
          </div>
          <div>
            <dt>结果路径</dt>
            <dd><CopyableValue label="结果路径" value={resultPath} /></dd>
          </div>
        </dl>
        {artifactReason !== EMPTY_TEXT && artifactReason !== reason ? (
          <section className="backtest-detail-section">
            <h3>Artifact 原因</h3>
            <ExpandableText summary="展开 Artifact 原因" value={artifactReason} />
          </section>
        ) : null}
        <section className="backtest-log-grid">
          <div>
            <h3>stdout</h3>
            <span>{summarizeText(artifact?.stdout)}</span>
            <ExpandableText mono summary="展开完整 stdout" value={artifact?.stdout} />
          </div>
          <div>
            <h3>stderr</h3>
            <span>{summarizeText(artifact?.stderr)}</span>
            <ExpandableText mono summary="展开完整 stderr" value={artifact?.stderr} />
          </div>
        </section>
      </div>
    </details>
  );
}
