import { Link } from "react-router-dom";

import { useMvpData } from "../api/useMvpData";
import { NONE_TEXT, sourceLabel, statusLabel, strategySourceLabel } from "./display";

export function Strategies() {
  const { data, source, isLoading } = useMvpData();

  return (
    <section className="page">
      <header className="page-header">
        <h1>策略</h1>
        <span className="status-pill">{sourceLabel(source, isLoading)}</span>
      </header>
      <div className="table-shell">
        <table>
          <thead>
            <tr>
              <th>名称</th>
              <th>状态</th>
              <th>周期</th>
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
                <td>{statusLabel(strategy.status)}</td>
                <td>{strategy.timeframe}</td>
                <td>{strategySourceLabel(strategy.source)}</td>
                <td>{strategy.currentVersion?.versionNumber ?? NONE_TEXT}</td>
                <td className="path-cell">{strategy.currentVersion?.filePath ?? NONE_TEXT}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {data.strategies.length === 0 ? <div className="empty-state">暂无策略。</div> : null}
    </section>
  );
}
