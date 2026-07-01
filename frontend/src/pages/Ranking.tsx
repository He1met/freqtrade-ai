import { useMvpData } from "../api/useMvpData";
import type { RankingScoreBreakdownItem } from "../api/types";

const SCORE_LABELS: Record<string, string> = {
  profit_score: "Profit",
  risk_score: "Risk",
  stability_score: "Stability",
  quality_score: "Quality",
};

function formatScore(value: number | null) {
  return value === null ? "none" : value.toFixed(1);
}

function formatBreakdownName(item: RankingScoreBreakdownItem) {
  return SCORE_LABELS[item.name] ?? item.name.replace(/_/g, " ");
}

export function Ranking() {
  const { data, source, isLoading } = useMvpData();

  return (
    <section className="page">
      <header className="page-header">
        <h1>Ranking</h1>
        <span className="status-pill">{isLoading ? "Loading" : source}</span>
      </header>
      <div className="table-shell">
        <table>
          <thead>
            <tr>
              <th>Rank</th>
              <th>Strategy</th>
              <th>Version</th>
              <th>Total</th>
              <th>Breakdown</th>
              <th>Outcome</th>
              <th>Reasons</th>
              <th>File</th>
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
                    <div className="secondary-cell">raw {entry.rawTotalScore.toFixed(1)}</div>
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
                    {entry.elimination.eliminated ? "Eliminated" : "Ranked"}
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
                    <span className="secondary-cell">none</span>
                  )}
                </td>
                <td className="path-cell">{entry.filePath}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {data.ranking.length === 0 ? <div className="empty-state">No scored strategies.</div> : null}
    </section>
  );
}
