import { useMvpData } from "../api/useMvpData";
import { sourceLabel } from "./display";

export function Dashboard() {
  const { data, source, isLoading, error } = useMvpData();
  const succeededBacktests = data.backtestRuns.filter((run) => run.status === "succeeded").length;
  const summary = [
    { label: "策略", value: data.strategies.length },
    { label: "生成批次", value: data.generationRuns.length },
    { label: "回测批次", value: data.backtestRuns.length },
    { label: "入榜策略", value: data.ranking.length },
  ];

  return (
    <section className="page">
      <header className="page-header">
        <h1>总览</h1>
        <span className="status-pill">{sourceLabel(source, isLoading)}</span>
      </header>
      {error ? <div className="notice">本地运行数据不可用：{error}</div> : null}
      <div className="metric-grid">
        {summary.map((item) => (
          <article className="metric" key={item.label}>
            <span>{item.label}</span>
            <strong>{item.value}</strong>
          </article>
        ))}
      </div>
      <div className="overview-grid">
        <article className="overview-panel">
          <h2>MVP 数据流</h2>
          <p>
            当前有 {data.generationRuns.length} 个生成批次、{data.backtestTasks.length} 个回测任务，
            以及 {succeededBacktests} 个成功回测批次可供检查。
          </p>
        </article>
        <article className="overview-panel">
          <h2>排行榜第一</h2>
          {data.ranking[0] ? (
            <p>
              {data.ranking[0].strategyName} 当前领先，总分 {data.ranking[0].totalScore.toFixed(1)}。
            </p>
          ) : (
            <p>暂无入榜策略。</p>
          )}
        </article>
      </div>
    </section>
  );
}
