import { useMvpData } from "../api/useMvpData";
import { combineDataSources } from "../api/sourceState";
import type { DataSourceTraceSummary, RankingScoreBreakdownItem } from "../api/types";
import { FallbackNotice } from "./FallbackNotice";
import { EMPTY_TEXT, displayLoadState } from "./uiCopy";

const SCORE_LABELS: Record<string, string> = {
  profit_score: "收益",
  risk_score: "风险",
  stability_score: "稳定性",
  quality_score: "质量",
};

function formatScore(value: number | null) {
  return value === null ? EMPTY_TEXT : value.toFixed(1);
}

function formatBreakdownName(item: RankingScoreBreakdownItem) {
  return SCORE_LABELS[item.name] ?? item.name.replace(/_/g, " ");
}

function formatRecord(record: Record<string, number | string>): string {
  const entries = Object.entries(record);
  return entries.length > 0 ? entries.map(([key, value]) => `${key}: ${value}`).join(", ") : EMPTY_TEXT;
}

function describeSourceTrace(source: DataSourceTraceSummary | undefined): string {
  if (!source) {
    return "source_type: unknown";
  }
  return [
    `source_type: ${source.sourceType}`,
    `core_data: ${source.coreData}`,
    `database_ids: ${formatRecord(source.databaseIds)}`,
    `artifact_refs: ${formatRecord(source.artifactRefs)}`,
    `detail: ${source.sourceDetail}`,
    source.blockedReason ? `blocked: ${source.blockedReason}` : null,
  ]
    .filter(Boolean)
    .join(" | ");
}

function compactPath(path: string | null | undefined): string {
  if (!path) {
    return EMPTY_TEXT;
  }
  const normalized = path.replace(/\\/g, "/");
  const segments = normalized.split("/").filter(Boolean);
  if (segments.length <= 3) {
    return normalized;
  }
  return `.../${segments.slice(-3).join("/")}`;
}

function RankingSourceSummary({ source }: { source: DataSourceTraceSummary | undefined }) {
  const sourceType = source?.sourceType ?? "unknown";
  const idSummary = source ? formatRecord(source.databaseIds) : EMPTY_TEXT;
  const detail = source?.blockedReason ?? source?.sourceDetail ?? "Source metadata was not provided.";

  return (
    <div
      className="ranking-source-summary"
      data-core-source={source?.coreData === true ? "true" : "false"}
      title={describeSourceTrace(source)}
    >
      <div className="ranking-source-heading">
        <strong>{sourceType}</strong>
        <span>{source?.coreData ? "core" : "non-core"}</span>
      </div>
      <span>{idSummary}</span>
      <em>{detail}</em>
    </div>
  );
}

export function Ranking() {
  const { data, sources, isLoading, error } = useMvpData();
  const source = combineDataSources(sources, ["ranking"]);

  return (
    <section className="page">
      <header className="page-header">
        <h1>策略排行榜</h1>
        <span className="status-pill">{displayLoadState(isLoading, source)}</span>
      </header>
      <FallbackNotice
        context="策略排行榜、评分拆解、淘汰原因和策略文件路径。"
        error={error}
        isLoading={isLoading}
        source={source}
      />
      <div className="table-shell ranking-table-shell">
        <table>
          <colgroup>
            <col className="ranking-col-rank" />
            <col className="ranking-col-strategy" />
            <col className="ranking-col-version" />
            <col className="ranking-col-score" />
            <col className="ranking-col-breakdown" />
            <col className="ranking-col-result" />
            <col className="ranking-col-reason" />
            <col className="ranking-col-source" />
            <col className="ranking-col-file" />
          </colgroup>
          <thead>
            <tr>
              <th>排名</th>
              <th>策略</th>
              <th>版本</th>
              <th>总分</th>
              <th>拆解</th>
              <th>结果</th>
              <th>原因</th>
              <th>数据来源</th>
              <th>文件</th>
            </tr>
          </thead>
          <tbody>
            {data.ranking.map((entry) => (
              <tr key={`${entry.strategyId}-${entry.versionNumber}`}>
                <td>{entry.rank}</td>
                <td>
                  <div className="primary-cell">{entry.strategyName}</div>
                  {entry.scoringVersion ? (
                    <div className="secondary-cell">{entry.scoringVersion}</div>
                  ) : null}
                </td>
                <td>{entry.versionNumber}</td>
                <td>
                  <div className="score-cell">{entry.totalScore.toFixed(1)}</div>
                  {entry.rawTotalScore !== null && entry.rawTotalScore !== entry.totalScore ? (
                    <div className="secondary-cell">原始 {entry.rawTotalScore.toFixed(1)}</div>
                  ) : null}
                </td>
                <td className="breakdown-cell">
                  {entry.scoreBreakdown.map((item) => (
                    <div className="score-breakdown" key={item.name}>
                      <span>{formatBreakdownName(item)}</span>
                      <strong>{formatScore(item.score)}</strong>
                      <em>{item.contribution.toFixed(1)}</em>
                    </div>
                  ))}
                </td>
                <td>
                  <span
                    className={
                      entry.elimination.eliminated ? "outcome-pill eliminated" : "outcome-pill passed"
                    }
                  >
                    {entry.elimination.eliminated ? "已淘汰" : "已入榜"}
                  </span>
                </td>
                <td className="reason-cell">
                  {[...entry.elimination.reasons, ...entry.warnings].length > 0 ? (
                    [...entry.elimination.reasons, ...entry.warnings].map((reason) => (
                      <div
                        className={`reason-line ${reason.severity}`}
                        key={`${reason.code}-${reason.message}`}
                        title={reason.message}
                      >
                        {reason.message}
                      </div>
                    ))
                  ) : (
                    <span className="secondary-cell">{EMPTY_TEXT}</span>
                  )}
                </td>
                <td className="source-cell">
                  <RankingSourceSummary source={entry.dataSource} />
                </td>
                <td className="path-cell" title={entry.filePath}>
                  {compactPath(entry.filePath)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {data.ranking.length === 0 ? (
        <div className="empty-state">暂无 database-backed 评分；fixture/fallback 排行榜不能作为真实验收。</div>
      ) : null}
    </section>
  );
}
