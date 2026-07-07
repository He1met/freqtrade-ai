import { Link } from "react-router-dom";

import { combineDataSources } from "../api/sourceState";
import type { DataSourceTraceSummary } from "../api/types";
import { useMvpData } from "../api/useMvpData";
import { FallbackNotice } from "./FallbackNotice";
import { EMPTY_TEXT, displayBoolean, displayLoadState, displayStatus, displayValue } from "./uiCopy";

function compactPath(path: string | null | undefined): string {
  if (!path) {
    return EMPTY_TEXT;
  }

  const normalized = path.replace(/\\/g, "/");
  const parts = normalized.split("/").filter(Boolean);
  if (parts.length <= 3) {
    return normalized;
  }
  return `.../${parts.slice(-3).join("/")}`;
}

function formatRecordSummary(record: Record<string, number | string>): string {
  const entries = Object.entries(record);
  if (entries.length === 0) {
    return EMPTY_TEXT;
  }

  return entries
    .slice(0, 2)
    .map(([key, value]) => `${key}: ${value}`)
    .join(", ");
}

function describeSourceTrace(source: DataSourceTraceSummary | undefined): string {
  if (!source) {
    return "source_type: unknown";
  }

  return [
    `source_type: ${source.sourceType}`,
    `core_data: ${displayBoolean(source.coreData)}`,
    `database_ids: ${formatRecordSummary(source.databaseIds)}`,
    `artifact_refs: ${formatRecordSummary(source.artifactRefs)}`,
    `detail: ${source.sourceDetail}`,
    source.blockedReason ? `blocked: ${source.blockedReason}` : null,
  ]
    .filter(Boolean)
    .join("\n");
}

function SourceTraceSummary({ source }: { source: DataSourceTraceSummary | undefined }) {
  const sourceType = source?.sourceType ?? "unknown";
  const databaseCount = source ? Object.keys(source.databaseIds).length : 0;
  const artifactCount = source ? Object.keys(source.artifactRefs).length : 0;
  const detail = source?.blockedReason ?? source?.sourceDetail ?? "Source metadata was not provided.";

  return (
    <div
      className="strategy-source-summary"
      data-core-source={source?.coreData === true ? "true" : "false"}
      title={describeSourceTrace(source)}
    >
      <div className="strategy-source-summary-heading">
        <strong>{sourceType}</strong>
        <span>{source?.coreData ? "core" : "non-core"}</span>
      </div>
      <span className="strategy-source-summary-detail">{detail}</span>
      <span className="strategy-source-summary-meta">
        db {databaseCount} / artifacts {artifactCount}
      </span>
    </div>
  );
}

export function Strategies() {
  const { data, sources, isLoading, error } = useMvpData();
  const source = combineDataSources(sources, ["strategies", "strategyVersions"]);

  return (
    <section className="page">
      <header className="page-header">
        <h1>策略</h1>
        <span className="status-pill">{displayLoadState(isLoading, source)}</span>
      </header>
      <FallbackNotice
        context="策略列表、状态、timeframe、来源和版本文件路径。"
        error={error}
        isLoading={isLoading}
        source={source}
      />
      <div className="table-shell strategies-table-shell">
        <table>
          <colgroup>
            <col className="strategies-col-name" />
            <col className="strategies-col-status" />
            <col className="strategies-col-timeframe" />
            <col className="strategies-col-origin" />
            <col className="strategies-col-version" />
            <col className="strategies-col-file" />
            <col className="strategies-col-source" />
          </colgroup>
          <thead>
            <tr>
              <th>名称</th>
              <th>状态</th>
              <th>Timeframe</th>
              <th>来源</th>
              <th>版本</th>
              <th>文件</th>
              <th>数据来源</th>
            </tr>
          </thead>
          <tbody>
            {data.strategies.map((strategy) => (
              <tr key={strategy.id}>
                <td>
                  <Link className="table-link" to={`/strategies/${strategy.id}`}>
                    {strategy.name}
                  </Link>
                </td>
                <td>{displayStatus(strategy.status)}</td>
                <td>{displayValue(strategy.timeframe)}</td>
                <td>{strategy.source === "ai_generated" ? "AI 生成" : displayValue(strategy.source)}</td>
                <td>{strategy.currentVersion?.versionNumber ?? EMPTY_TEXT}</td>
                <td className="strategy-path-cell" title={strategy.currentVersion?.filePath ?? EMPTY_TEXT}>
                  {compactPath(strategy.currentVersion?.filePath)}
                </td>
                <td className="strategy-source-cell">
                  <SourceTraceSummary source={strategy.dataSource} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {data.strategies.length === 0 ? <div className="empty-state">暂无策略。</div> : null}
    </section>
  );
}
