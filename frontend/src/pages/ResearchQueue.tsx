import { useEffect, useMemo, useState } from "react";

import { fetchCandidateResearchQueue, triggerBihourlyResearchGeneration, type BihourlyResearchTriggerResult, type CandidateResearchQueueItem, type CandidateResearchQueueRead, type CandidateResearchQueueStatus } from "../api/candidateResearchQueueApi";
import { fetchStrategyResearchWorkspace, type StrategyResearchWorkspace } from "../api/strategyResearchApi";
import { CompactText, EmptyState, ExpandableText, FormalLoadingState, PageHeader, StatusBadge } from "../components/DisplayPrimitives";
import "../styles/research-queue.css";
import { displayDuration, filterAndSortQueueItems, projectResearchQueue, researchGenerationStatusLabel, researchQueueActionAdvice, researchQueueStatusLabel, researchQueueStatusTone, safeEvidenceHref, terminalGroups, type QueueFilters, type ResearchQueueSort } from "./researchQueueModel";
import { displayDateTime } from "./uiCopy";

const EMPTY_FILTERS: QueueFilters = { query: "", status: "ALL", pair: "", timeframe: "", batch: "" };

function QueueProgress({ item }: { item: CandidateResearchQueueItem }) {
  if (item.progress_percent === null) return <span className="research-queue-unavailable">数据暂不可用</span>;
  const value = Math.max(0, Math.min(100, item.progress_percent));
  return <span className="research-queue-progress"><progress aria-label={`${item.candidate_name} 进度`} max="100" value={value} /><span>{value}%</span></span>;
}

function EvidenceDetails({ item }: { item: CandidateResearchQueueItem }) {
  return (
    <details className="research-queue-evidence-details">
      <summary>高级证据与内部引用（{item.evidence.length}）</summary>
      {item.evidence.length ? <ul className="research-queue-evidence">{item.evidence.map((evidence, index) => {
        const href = safeEvidenceHref(evidence.href);
        return <li key={`${evidence.reference}-${index}`}>{href ? <a href={href}>{evidence.label}</a> : <span>{evidence.label}</span>}<CompactText label="证据引用" mono value={evidence.reference} /></li>;
      })}</ul> : <p className="research-queue-unavailable">暂无可打开的证据引用。</p>}
      <dl className="research-queue-raw"><div><dt>原始状态</dt><dd>{item.status}</dd></div><div><dt>原因代码</dt><dd>{item.reason_code ?? "未提供"}</dd></div><div><dt>尝试次数</dt><dd>{item.attempt ?? "未提供"}</dd></div></dl>
    </details>
  );
}

function CandidateCard({ item, current = false }: { item: CandidateResearchQueueItem; current?: boolean }) {
  const reason = item.reason_message || item.reason_code;
  const advice = researchQueueActionAdvice(item.status);
  return (
    <article className={`research-queue-item${current ? " is-current" : ""}`}>
      <header><div><strong>{item.candidate_name}</strong><span>{item.pair ?? "品种暂不可用"} · {item.timeframe ?? "周期暂不可用"}</span></div><StatusBadge label={researchQueueStatusLabel(item.status)} status={item.status} tone={researchQueueStatusTone(item.status)} /></header>
      <dl className="research-queue-item-grid">
        <div><dt>当前阶段</dt><dd>{item.current_step ?? "数据暂不可用"}</dd></div>
        <div><dt>队列位置</dt><dd>{item.queue_position === null ? "数据暂不可用" : `第 ${item.queue_position} 位`}</dd></div>
        <div><dt>生成时间</dt><dd>{displayDateTime(item.generated_at)}</dd></div>
        <div><dt>已耗时</dt><dd>{displayDuration(item.elapsed_seconds)}</dd></div>
        <div><dt>最近进度</dt><dd><QueueProgress item={item} /></dd></div>
        <div><dt>下一步</dt><dd>{item.next_step ?? "数据暂不可用"}</dd></div>
      </dl>
      {current ? <div className="research-queue-steps"><strong>已完成步骤</strong>{item.completed_steps.length ? <ol>{item.completed_steps.map((step) => <li key={step}>{step}</li>)}</ol> : <span>数据暂不可用</span>}</div> : null}
      {reason ? <p className="research-queue-reason"><strong>原因：</strong>{reason}</p> : null}
      {advice ? <p className="research-queue-advice"><strong>建议：</strong>{advice}</p> : null}
      <div className="research-queue-id"><span>候选标识</span><CompactText label="候选标识" mono value={item.candidate_id} /></div>
      <EvidenceDetails item={item} />
    </article>
  );
}

export function ResearchQueue() {
  const [queue, setQueue] = useState<CandidateResearchQueueRead | null>(null);
  const [workspace, setWorkspace] = useState<StrategyResearchWorkspace | null>(null);
  const [queueError, setQueueError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  const [triggering, setTriggering] = useState(false);
  const [triggerResult, setTriggerResult] = useState<BihourlyResearchTriggerResult | null>(null);
  const [triggerError, setTriggerError] = useState<string | null>(null);
  const [operatorToken, setOperatorToken] = useState("");
  const [filters, setFilters] = useState<QueueFilters>(EMPTY_FILTERS);
  const [sort, setSort] = useState<ResearchQueueSort>("queue");

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    Promise.allSettled([fetchCandidateResearchQueue(controller.signal), fetchStrategyResearchWorkspace(controller.signal)]).then(([queueResult, workspaceResult]) => {
      if (controller.signal.aborted) return;
      if (queueResult.status === "fulfilled") { setQueue(queueResult.value); setQueueError(null); }
      else { setQueue(null); setQueueError(queueResult.reason instanceof Error ? queueResult.reason.message : String(queueResult.reason)); }
      setWorkspace(workspaceResult.status === "fulfilled" ? workspaceResult.value : null);
      setLoading(false);
    });
    return () => controller.abort();
  }, [revision]);

  const projection = useMemo(() => projectResearchQueue(queue, workspace, queueError), [queue, workspace, queueError]);
  const batchId = projection.batch?.run_id ?? null;
  const waiting = useMemo(() => filterAndSortQueueItems(projection.waiting, filters, sort, batchId), [projection.waiting, filters, sort, batchId]);
  const completed = useMemo(() => filterAndSortQueueItems(projection.completed, filters, sort, batchId), [projection.completed, filters, sort, batchId]);
  const pairs = [...new Set([...projection.waiting, ...projection.completed].map((item) => item.pair).filter((value): value is string => Boolean(value)))].sort();
  const timeframes = [...new Set([...projection.waiting, ...projection.completed].map((item) => item.timeframe).filter((value): value is string => Boolean(value)))].sort();
  const setFilter = <K extends keyof QueueFilters>(key: K, value: QueueFilters[K]) => setFilters((current) => ({ ...current, [key]: value }));

  const triggerGeneration = async () => {
    if (!operatorToken.trim()) {
      setTriggerError("需要本地 operator token；凭据仅保留在当前页面内存中。");
      return;
    }
    if (!window.confirm("只刷新公开行情并按后端当前配置持久化本批候选；不会在本次请求中启动回测、部署、信号或订单。继续吗？")) return;
    const controller = new AbortController();
    setTriggering(true);
    setTriggerResult(null);
    setTriggerError(null);
    try {
      const result = await triggerBihourlyResearchGeneration(operatorToken, controller.signal);
      setTriggerResult(result);
      setRevision((value) => value + 1);
    } catch (error) {
      setTriggerError(error instanceof Error ? error.message : String(error));
    } finally {
      setTriggering(false);
    }
  };

  return <section className="page research-queue-page">
    <PageHeader title="策略研究队列" description="候选先持久化，再由 lease 保护的独立串行消费者逐条回测；生成入口不会在同一请求中启动回测或部署。" actions={<><label className="research-queue-operator-token"><span>本地 operator token</span><input autoComplete="off" onChange={(event) => setOperatorToken(event.target.value)} placeholder="仅当前页面内存" type="password" value={operatorToken} /></label><button className="primary-button" disabled={triggering || !operatorToken.trim()} onClick={triggerGeneration} type="button">{triggering ? "正在安全生成…" : "刷新行情并生成本批"}</button><button className="secondary-button" onClick={() => setRevision((value) => value + 1)} type="button">刷新只读数据</button></>} status={<StatusBadge label={triggering ? "生成门禁检查中" : loading ? "读取中" : projection.available ? "队列数据已连接" : "等待后端队列"} status={triggering || loading ? "RUNNING" : projection.available ? "AVAILABLE" : "UNKNOWN"} tone={triggering || loading ? "info" : projection.available ? "success" : "warning"} />} />
    {triggerResult ? <aside className={`research-queue-trigger-result is-${triggerResult.status.toLowerCase()}`} role="status"><strong>{triggerResult.status}</strong><span>批次 {triggerResult.run_id} · 持久化 {triggerResult.persisted_count} 条 · {triggerResult.reason_code}</span><small>runtime {triggerResult.runtime_status} / opening guard {triggerResult.opening_guard}；本次未启动回测、部署、信号或订单。</small></aside> : null}
    {triggerError ? <aside className="research-queue-trigger-result is-failed" role="alert"><strong>触发失败</strong><span>{triggerError}</span><small>未推断或伪造任何队列进度，请以重新读取的后端状态为准。</small></aside> : null}
    {loading ? <FormalLoadingState label="正在读取策略研究队列" /> : <>
      {!projection.available ? <aside className="research-queue-fallback" role="status"><strong>队列实时数据暂不可用</strong><p>等待后端提供 <code>formal-candidate-validation-queue-read-v1</code>。旧批次只能显示历史终态，不能推断当前回测项、lease、位置或百分比。</p><ExpandableText summary="查看接口状态" value={projection.fallbackReason} /></aside> : null}
      <section className="research-queue-focus" aria-labelledby="research-queue-focus-title">
        <div className="research-queue-section-heading"><div><span>现在正在做什么 · 串行最多 1 条</span><h2 id="research-queue-focus-title">当前正在回测</h2></div><StatusBadge label={projection.active ? "已由 lease 领取" : projection.available ? "当前空闲" : "数据暂不可用"} status={projection.active?.status ?? "UNKNOWN"} tone={projection.active ? "info" : "neutral"} /></div>
        {projection.active ? <CandidateCard current item={projection.active} /> : <EmptyState title={projection.available ? "当前没有候选被领取" : "当前回测项数据暂不可用"} description={projection.available ? "队列可能为空、等待 worker，或本批次已经完成。" : "不能从旧批次状态推断某条策略正在回测。"} />}
        <div className="research-queue-safe-actions"><button disabled type="button">取消不可用</button><button disabled type="button">重试不可用</button><span>候选执行状态只读；生成由后端 runtime/data/ownership 门禁授权。</span></div>
      </section>

      <section aria-label="本批次辅助汇总" className="research-queue-summary">{[["本批目标", projection.batch?.expected_count ?? "UNKNOWN"], ["生成状态", researchGenerationStatusLabel(projection.available ? projection.batch?.generation_status ?? null : null)], ["等待中", projection.available ? projection.batch?.waiting_count ?? 0 : "暂不可用"], ["已完成", projection.batch?.completed_count ?? projection.completed.length], ["剩余", projection.available ? projection.batch?.remaining_count ?? 0 : "暂不可用"], ["队列健康", projection.health?.status ?? "UNKNOWN"]].map(([label, value]) => <div key={label}><span>{label}</span><strong>{value}</strong></div>)}</section>

      <div className="research-queue-controls" role="search">
        <label><span>搜索</span><input onChange={(event) => setFilter("query", event.target.value)} placeholder="名称或候选标识" type="search" value={filters.query} /></label>
        <label><span>状态</span><select onChange={(event) => setFilter("status", event.target.value as CandidateResearchQueueStatus | "ALL")} value={filters.status}><option value="ALL">全部（默认不隐藏失败/等待）</option>{["PENDING", "CLAIMED", "RUNNING", "VALIDATED", "REJECTED", "FAILED", "QUALIFIED_PENDING_DEPLOYMENT", "DEPLOYING", "DEPLOYED", "UNKNOWN"].map((value) => <option key={value} value={value}>{researchQueueStatusLabel(value as CandidateResearchQueueStatus)}</option>)}</select></label>
        <label><span>品种</span><select onChange={(event) => setFilter("pair", event.target.value)} value={filters.pair}><option value="">全部品种</option>{pairs.map((pair) => <option key={pair}>{pair}</option>)}</select></label>
        <label><span>周期</span><select onChange={(event) => setFilter("timeframe", event.target.value)} value={filters.timeframe}><option value="">全部周期</option>{timeframes.map((timeframe) => <option key={timeframe}>{timeframe}</option>)}</select></label>
        <label><span>批次</span><select onChange={(event) => setFilter("batch", event.target.value)} value={filters.batch}><option value="">全部批次</option>{batchId ? <option value={batchId}>{batchId}</option> : null}</select></label>
        <label><span>排序</span><select onChange={(event) => setSort(event.target.value as ResearchQueueSort)} value={sort}><option value="queue">队列顺序</option><option value="generated-newest">生成时间（新到旧）</option><option value="generated-oldest">生成时间（旧到新）</option><option value="name">候选名称</option></select></label>
      </div>

      <section className="research-queue-waiting" aria-labelledby="research-queue-waiting-title"><div className="research-queue-section-heading"><div><span>按位置顺序 · 显示前序数量</span><h2 id="research-queue-waiting-title">等待中的候选</h2></div><strong>{waiting.length} 条</strong></div>{waiting.length ? <ol>{waiting.map((item) => <li key={item.candidate_id}><span className="research-queue-position">#{item.queue_position ?? "—"}<small>前序 {item.preceding_count ?? "—"} 条</small></span><CandidateCard item={item} /></li>)}</ol> : <EmptyState title={projection.available ? "没有符合筛选条件的等待项" : "等待队列数据暂不可用"} description={projection.available ? "可清除筛选条件查看全部候选。" : "后端只读投影上线后才显示真实位置。"} />}</section>

      <section className="research-queue-completed" aria-labelledby="research-queue-completed-title"><div className="research-queue-section-heading"><div><span>逐条终态 · 原因 · 证据</span><h2 id="research-queue-completed-title">已完成</h2></div><strong>{completed.length} 条</strong></div>{completed.length ? terminalGroups(completed).map((group) => <details key={group.status} open={group.items.length > 0}><summary><StatusBadge label={researchQueueStatusLabel(group.status)} status={group.status} tone={researchQueueStatusTone(group.status)} /><strong>{group.items.length} 条</strong></summary><div className="research-queue-terminal-list">{group.items.length ? group.items.map((item) => <CandidateCard item={item} key={item.candidate_id} />) : <span>本组暂无记录</span>}</div></details>) : <EmptyState title="暂无已完成候选" description="未生成、排队、无动作、失败和拒绝是不同状态；这里为空不等于失败。" />}</section>
    </>}
  </section>;
}
