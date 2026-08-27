import { fetchCanonicalResearchRuns } from "../../api/canonicalV13Client";
import type { ResearchRunProjection } from "../../api/canonicalV13Types";
import { CopyableValue, StatusBadge } from "../../components/DisplayPrimitives";
import { CanonicalQueryError, CanonicalStatePanel, useCanonicalQuery } from "./CanonicalStatePanel";

const STATUS_COPY: Record<string, { explanation: string; label: string; tone: "danger" | "info" | "warning" }> = {
  BOUNDED_NEGATIVE: {
    explanation: "实验完成，但没有通过冻结的成本后稳定性门槛。",
    label: "有界负结果",
    tone: "danger",
  },
  DATA_ACQUISITION_BLOCKED_PRE_VALUE: {
    explanation: "有效市场数据尚未进入经济评估，不能据此判断策略有效或无效。",
    label: "采集前阻塞",
    tone: "warning",
  },
  FINALISTS_FROZEN: {
    explanation: "候选仅完成前策略筛选，尚未获得 QUALIFIED 或部署资格。",
    label: "候选已冻结",
    tone: "info",
  },
};

function statusCopy(status: string) {
  return STATUS_COPY[status] ?? {
    explanation: "这是离线研究记录，不代表资格、部署许可或交易授权。",
    label: status,
    tone: "info" as const,
  };
}

function Summary({ title, value }: { title: string; value: Record<string, unknown> }) {
  return (
    <section>
      <h3>{title}</h3>
      <pre className="canonical-v13-research-history-json">{JSON.stringify(value, null, 2)}</pre>
    </section>
  );
}

function ResearchRunCard({ run }: { run: ResearchRunProjection }) {
  const presentation = statusCopy(run.status);
  return (
    <article className="canonical-v13-panel canonical-v13-research-history-card" data-research-status={run.status}>
      <div className="canonical-v13-heading-row">
        <div><small>{run.created_at} · {run.timeframe}</small><h3>{run.name}</h3></div>
        <StatusBadge label={presentation.label} status={run.status} tone={presentation.tone} />
      </div>
      <p className="canonical-v13-research-history-explanation">{presentation.explanation}</p>
      <p>{run.hypothesis}</p>
      <div className="canonical-v13-research-history-meta">
        <span>{run.universe.join(" · ")}</span>
        <code>{run.reason_code}</code>
      </div>
      <details className="canonical-v13-advanced-evidence">
        <summary>查看完整研究结果</summary>
        <div className="canonical-v13-research-history-details">
          <Summary title="TRAIN / VALIDATION / HOLDOUT" value={run.train_validation_holdout_summary} />
          <Summary title="指标摘要" value={run.metrics_summary} />
          <dl className="canonical-v13-definition-list">
            <div><dt>原始证据路径</dt><dd><code>{run.artifact_path}</code></dd></div>
            <div><dt>Dataset digest</dt><dd><CopyableValue value={run.dataset_digest} /></dd></div>
            <div><dt>Result digest</dt><dd><CopyableValue value={run.result_digest} /></dd></div>
            <div><dt>Run ID</dt><dd><code>{run.run_id}</code></dd></div>
          </dl>
        </div>
      </details>
    </article>
  );
}

export function CanonicalResearchHistory({ enabled = true }: { enabled?: boolean }) {
  const query = useCanonicalQuery(fetchCanonicalResearchRuns, [], enabled);
  return (
    <section aria-label="历史研究运行" className="canonical-v13-panel">
      <div className="canonical-v13-heading-row">
        <div><small>只读离线目录</small><h2>历史研究运行</h2></div>
        {query.data ? <strong>{query.data.items.length} 条</strong> : null}
      </div>
      <p className="canonical-v13-results-boundary">这里统一展示未进入正式策略流水线的历史研究；负结果、数据阻塞和候选冻结不会被当成 QUALIFIED。</p>
      {query.loading ? <CanonicalStatePanel description="正在读取历史研究目录。" kind="loading" title="加载研究历史" /> : null}
      {query.error ? <CanonicalQueryError error={query.error} title="研究历史暂不可用" /> : null}
      {query.data?.status === "EMPTY" ? <CanonicalStatePanel description="目录表已就绪，但尚未导入研究结果。" kind="empty" title="暂无研究历史" /> : null}
      {query.data?.status === "AVAILABLE" ? (
        <div className="canonical-v13-research-history-list">
          {query.data.items.map((run) => <ResearchRunCard key={run.run_id} run={run} />)}
        </div>
      ) : null}
    </section>
  );
}
