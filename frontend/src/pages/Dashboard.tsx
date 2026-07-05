import { useMvpData } from "../api/useMvpData";
import { FallbackNotice } from "./FallbackNotice";
import { displayLoadState } from "./uiCopy";

export function Dashboard() {
  const { data, source, isLoading, error } = useMvpData();
  const succeededBacktests = data.backtestRuns.filter((run) => run.status === "succeeded").length;
  const flowItems = [
    `${data.generationRuns.length} 个生成批次`,
    `${data.backtestTasks.length} 个回测任务`,
    `${data.hyperoptRuns.length} 个 Hyperopt 参数优化批次`,
  ];
  const summary = [
    { label: "策略", value: data.strategies.length },
    { label: "生成批次", value: data.generationRuns.length },
    { label: "回测批次", value: data.backtestRuns.length },
    { label: "Hyperopt 参数优化", value: data.hyperoptRuns.length },
    { label: "已评分策略", value: data.ranking.length },
  ];

  return (
    <section className="page">
      <header className="page-header">
        <h1>总览</h1>
        <span className="status-pill">{displayLoadState(isLoading, source)}</span>
      </header>
      <FallbackNotice
        context="Dashboard 总览指标、MVP 数据流和排行榜摘要。"
        error={error}
        isLoading={isLoading}
        source={source}
      />
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
            当前有 {flowItems.join("、")}，以及 {succeededBacktests} 个成功回测批次可供复核。
          </p>
        </article>
        <article className="overview-panel">
          <h2>排行榜领先策略</h2>
          {data.ranking[0] ? (
            <p>
              {data.ranking[0].strategyName} 当前总分最高：{data.ranking[0].totalScore.toFixed(1)}。
            </p>
          ) : (
            <p>暂无已评分策略。</p>
          )}
        </article>
      </div>
    </section>
  );
}
