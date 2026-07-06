import { Link } from "react-router-dom";

import { combineDataSources } from "../api/sourceState";
import { useMvpData } from "../api/useMvpData";
import { FallbackNotice } from "./FallbackNotice";
import { EMPTY_TEXT, displayLoadState, displayStatus } from "./uiCopy";

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
                  <Link className="table-link" to={`/strategies/${strategy.id}`}>
                    {strategy.name}
                  </Link>
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
