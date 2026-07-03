import { useMvpData } from "../api/useMvpData";
import type { RankingScoreBreakdownItem } from "../api/types";
import { NONE_TEXT, sourceLabel } from "./display";

const SCORE_LABELS: Record<string, string> = {
  profit_score: "收益",
  risk_score: "风险",
  stability_score: "稳定性",
  quality_score: "质量",
};

function formatScore(value: number | null) {
  return value === null ? NONE_TEXT : value.toFixed(1);
}

function formatBreakdownName(item: RankingScoreBreakdownItem) {
  return SCORE_LABELS[item.name] ?? item.name.replace(/_/g, " ");
}

export function Ranking() {
  const { data, source, isLoading } = useMvpData();

  return (
    <section className="page">
      <header className="page-header">
        <h1>排行榜</h1>
        <span className="status-pill">{sourceLabel(source, isLoading)}</span>
      </header>
      <div className="table-shell">
        <table>
          <thead>
            <tr>
              <th>排名</th>
              <th>策略</th>
              <th>版本</th>
              <th>总分</th>
              <th>拆分</th>
              <th>结果</th>
              <th>原因</th>
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
                    <div className="secondary-cell">原始分 {entry.rawTotalScore.toFixed(1)}</div>
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
                      <div className={`reason-line ${reason.severity}`} key={`${reason.code}-${reason.message}`}>
                        {reason.message}
                      </div>
                    ))
                  ) : (
                    <span className="secondary-cell">{NONE_TEXT}</span>
                  )}
                </td>
                <td className="path-cell">{entry.filePath}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {data.ranking.length === 0 ? <div className="empty-state">暂无评分策略。</div> : null}
    </section>
  );
}
