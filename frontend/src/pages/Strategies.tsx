import { Link } from "react-router-dom";

import { useMvpData } from "../api/useMvpData";
import { EMPTY_TEXT, displayLoadState, displayStatus } from "./uiCopy";

export function Strategies() {
  const { data, source, isLoading } = useMvpData();

  return (
    <section className="page">
      <header className="page-header">
        <h1>策略</h1>
        <span className="status-pill">{displayLoadState(isLoading, source)}</span>
      </header>
      <div className="table-shell">
        <table>
          <thead>
            <tr>
              <th>名称</th>
              <th>状态</th>
              <th>Timeframe</th>
              <th>来源</th>
              <th>版本</th>
              <th>文件</th>
            </tr>
          </thead>
          <tbody>
            {data.strategies.map((strategy) => (
              <tr key={strategy.id}>
                <td>
                  <Link to={`/strategies/${strategy.id}`}>{strategy.name}</Link>
                </td>
                <td>{displayStatus(strategy.status)}</td>
                <td>{strategy.timeframe}</td>
                <td>{strategy.source === "ai_generated" ? "AI 生成" : strategy.source}</td>
                <td>{strategy.currentVersion?.versionNumber ?? EMPTY_TEXT}</td>
                <td className="path-cell">{strategy.currentVersion?.filePath ?? EMPTY_TEXT}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {data.strategies.length === 0 ? <div className="empty-state">暂无策略。</div> : null}
    </section>
  );
}
