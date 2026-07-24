import { combineDataSources } from "../api/sourceState";
import { useMvpData } from "../api/useMvpData";
import { EmptyState, PageHeader, StatusBadge } from "../components/DisplayPrimitives";
import { FallbackNotice } from "./FallbackNotice";
import { dashboardViewState } from "./dashboardState";
import { displayLoadState } from "./uiCopy";

export function Dashboard() {
  const { data, sources, isLoading, error } = useMvpData();
  const source = combineDataSources(sources, [
    "strategies",
    "generationRuns",
    "backtestRuns",
    "backtestTasks",
    "hyperoptRuns",
    "ranking",
  ]);
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
  const visibleRecordCount = summary.reduce((total, item) => total + item.value, 0);
  const viewState = dashboardViewState({
    error,
    isLoading,
    source,
    visibleRecordCount,
  });
  const loadLabel = displayLoadState(isLoading, source);
  const status =
    viewState === "loading"
      ? "running"
      : viewState === "failed"
        ? "failed"
        : viewState === "empty"
          ? "not_run"
          : "ready";

  return (
    <section className="page dashboard-page">
      <PageHeader
        description="集中查看策略研究、回测验证和参数优化的真实进展。"
        eyebrow="Freqtrade AI 工作台"
        status={<StatusBadge label={loadLabel} status={status} />}
        title="总览"
      />
      <FallbackNotice
        context="Dashboard 总览指标、MVP 数据流和排行榜摘要。"
        error={error}
        isLoading={isLoading}
        source={source}
      />
      {viewState === "loading" ? (
        <EmptyState
          className="dashboard-state-panel dashboard-loading-state"
          description="正在请求后端 API，在数据返回前不会显示 0 值指标。"
          title="正在加载总览数据"
        />
      ) : null}
      {viewState === "failed" ? (
        <EmptyState
          className="dashboard-state-panel dashboard-failed-state"
          description="请先恢复后端 API，再刷新页面。当前不展示空快照或模拟指标。"
          title="总览数据暂不可用"
        />
      ) : null}
      {viewState === "empty" ? (
        <EmptyState
          className="dashboard-state-panel"
          description="后端 API 已连接，但尚无可显示的真实核心记录。请先运行本地策略研究和回测流程。"
          title="暂无真实运行记录"
        />
      ) : null}
      {viewState === "ready" ? (
        <>
          <section aria-labelledby="dashboard-metrics-title">
            <div className="dashboard-section-heading">
              <div>
                <span>核心指标</span>
                <h2 id="dashboard-metrics-title">当前真实数据</h2>
              </div>
              <p>以下数字仅统计当前页面已加载的记录。</p>
            </div>
            <div className="metric-grid dashboard-metric-grid">
              {summary.map((item) => (
                <article className="metric" key={item.label}>
                  <span>{item.label}</span>
                  <strong>{item.value}</strong>
                </article>
              ))}
            </div>
          </section>
          <div className="overview-grid dashboard-overview-grid">
            <article className="overview-panel dashboard-primary-panel">
              <span className="dashboard-panel-label">流程进展</span>
              <h2>研究与验证</h2>
              <p>
                当前有 {flowItems.join("、")}，以及 {succeededBacktests} 个成功回测批次可供复核。
              </p>
            </article>
            <article className="overview-panel">
              <span className="dashboard-panel-label">领先结果</span>
              <h2>策略排行榜</h2>
              {data.ranking[0] ? (
                <p>
                  <strong>{data.ranking[0].strategyName}</strong> 当前总分最高：
                  {data.ranking[0].totalScore.toFixed(1)}。
                </p>
              ) : (
                <p>当前真实记录中暂无已评分策略。</p>
              )}
            </article>
          </div>
        </>
      ) : null}
    </section>
  );
}
