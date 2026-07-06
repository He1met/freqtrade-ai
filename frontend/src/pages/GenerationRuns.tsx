import { combineDataSources } from "../api/sourceState";
import { useMvpData } from "../api/useMvpData";
import { FallbackNotice } from "./FallbackNotice";
import { EMPTY_TEXT, displayLoadState, displayStatus } from "./uiCopy";

export function GenerationRuns() {
  const { data, sources, isLoading, error } = useMvpData();
  const source = combineDataSources(sources, ["generationRuns"]);

  return (
    <section className="page">
      <header className="page-header">
        <h1>生成批次</h1>
        <span className="status-pill">{displayLoadState(isLoading, source)}</span>
      </header>
      <FallbackNotice
        context="AI 生成批次、provider/model、请求数量和错误摘要。"
        error={error}
        isLoading={isLoading}
        source={source}
      />
      <div className="table-shell">
        <table>
          <thead>
            <tr>
              <th>批次</th>
              <th>状态</th>
              <th>Provider</th>
              <th>Model</th>
              <th>请求数</th>
              <th>生成数</th>
              <th>接受数</th>
              <th>失败数</th>
              <th>错误</th>
            </tr>
          </thead>
          <tbody>
            {data.generationRuns.map((run) => (
              <tr key={run.id}>
                <td>{run.id}</td>
                <td>{displayStatus(run.status)}</td>
                <td>{run.provider}</td>
                <td>{run.model}</td>
                <td>{run.requestedCount}</td>
                <td>{run.generatedCount}</td>
                <td>{run.acceptedCount}</td>
                <td>{run.failedCount}</td>
                <td className="path-cell">{run.errorMessage ?? EMPTY_TEXT}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {data.generationRuns.length === 0 ? (
        <div className="empty-state">暂无生成批次。</div>
      ) : null}
    </section>
  );
}
