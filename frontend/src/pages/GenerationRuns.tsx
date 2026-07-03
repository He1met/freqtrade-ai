import { useMvpData } from "../api/useMvpData";
import { optionalText, sourceLabel, statusLabel } from "./display";

export function GenerationRuns() {
  const { data, source, isLoading } = useMvpData();

  return (
    <section className="page">
      <header className="page-header">
        <h1>生成批次</h1>
        <span className="status-pill">{sourceLabel(source, isLoading)}</span>
      </header>
      <div className="table-shell">
        <table>
          <thead>
            <tr>
              <th>批次</th>
              <th>状态</th>
              <th>提供方</th>
              <th>模型</th>
              <th>请求数量</th>
              <th>生成数量</th>
              <th>接受数量</th>
              <th>失败数量</th>
              <th>错误</th>
            </tr>
          </thead>
          <tbody>
            {data.generationRuns.map((run) => (
              <tr key={run.id}>
                <td>{run.id}</td>
                <td>{statusLabel(run.status)}</td>
                <td>{run.provider}</td>
                <td>{run.model}</td>
                <td>{run.requestedCount}</td>
                <td>{run.generatedCount}</td>
                <td>{run.acceptedCount}</td>
                <td>{run.failedCount}</td>
                <td className="path-cell">{optionalText(run.errorMessage)}</td>
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
