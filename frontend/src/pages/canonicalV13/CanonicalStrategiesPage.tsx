import { useSearchParams } from "react-router-dom";

import { fetchCanonicalStrategies, fetchCanonicalStrategy } from "../../api/canonicalV13Client";
import type { StrategyProjection } from "../../api/canonicalV13Types";
import { CopyableValue, PageHeader } from "../../components/DisplayPrimitives";
import { CanonicalQueryError, CanonicalStatePanel, CanonicalStatus, useCanonicalQuery } from "./CanonicalStatePanel";
import { canonicalStatusesKnown, parseCanonicalUrlState, withCanonicalUrlValue } from "./canonicalV13Model";

function StrategyDetail({ strategyId }: { strategyId: string }) {
  const query = useCanonicalQuery((signal) => fetchCanonicalStrategy(strategyId, signal), [strategyId]);
  if (query.loading) return <CanonicalStatePanel description="正在读取所选 canonical strategy。" kind="loading" title="加载策略详情" />;
  if (query.error) return <CanonicalQueryError error={query.error} title="所选策略不存在或不可读取" />;
  const strategy = query.data as StrategyProjection;
  if (!canonicalStatusesKnown(strategy.catalog_status, strategy.intake_status, strategy.validation_status, strategy.qualification_status)) {
    return <CanonicalStatePanel description="策略详情返回未知 enum；详情与成功状态保持隐藏。" kind="unknown" reasonCodes={["UNKNOWN_CONTRACT_VALUE"]} title="Strategy detail 合同漂移" />;
  }
  return (
    <section className="canonical-v13-panel" aria-label="Canonical strategy detail">
      <div className="canonical-v13-heading-row"><h2>{strategy.display_name}</h2><CanonicalStatus status={strategy.catalog_status} /></div>
      <div className="canonical-v13-status-grid">
        <div><span>Intake</span><CanonicalStatus status={strategy.intake_status} /></div>
        <div><span>Validation</span><CanonicalStatus status={strategy.validation_status} /></div>
        <div><span>Qualification</span><CanonicalStatus status={strategy.qualification_status} /></div>
        <div><span>Execution authorized</span><strong>{strategy.execution_authorized ? "是" : "否"}</strong></div>
      </div>
      <dl className="canonical-v13-definition-list">
        <div><dt>Strategy ID</dt><dd><CopyableValue value={strategy.strategy_id} /></dd></div>
        <div><dt>Version</dt><dd>{strategy.version_number}</dd></div>
        <div><dt>Artifact digest</dt><dd><CopyableValue value={strategy.artifact_digest} /></dd></div>
      </dl>
    </section>
  );
}

export function CanonicalStrategiesPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const url = parseCanonicalUrlState("strategies", searchParams);
  const catalog = useCanonicalQuery(fetchCanonicalStrategies, [], url.valid);
  const selected = url.values.strategy ?? null;
  const catalogContractKnown = catalog.data
    ? canonicalStatusesKnown(catalog.data.status, ...catalog.data.items.flatMap((item) => [item.catalog_status, item.intake_status, item.validation_status, item.qualification_status]))
    : true;

  function selectStrategy(strategyId: string | null) {
    setSearchParams(withCanonicalUrlValue("strategies", url.values, "strategy", strategyId));
  }

  return (
    <div className="canonical-v13-page">
      <PageHeader description="目录、validation 与 qualification 是独立状态；列表不会自动选择第一项。" eyebrow="V1.3 canonical-only" title="Strategy Catalog" />
      {!url.valid ? <CanonicalStatePanel description="URL selection 无效；未发送详情请求。" kind="unknown" reasonCodes={url.problems} title="INVALID_URL_STATE" /> : null}
      {catalog.loading ? <CanonicalStatePanel description="正在读取 canonical catalog。" kind="loading" title="加载策略目录" /> : null}
      {catalog.error ? <CanonicalQueryError error={catalog.error} title="策略目录状态未知" /> : null}
      {catalog.data && !catalogContractKnown ? <CanonicalStatePanel description="目录包含未知 enum；selection 与详情请求保持禁用。" kind="unknown" reasonCodes={["UNKNOWN_CONTRACT_VALUE"]} title="Strategy projection 合同漂移" /> : null}
      {catalogContractKnown && catalog.data?.status === "EMPTY" ? <CanonicalStatePanel description="API 明确返回 EMPTY；这不是加载失败或 legacy fallback。" kind="empty" title="Canonical strategy 目录为空" /> : null}
      {catalogContractKnown && catalog.data?.status === "AVAILABLE" ? (
        <section className="canonical-v13-panel">
          <div className="canonical-v13-heading-row"><h2>Strategies</h2><CanonicalStatus status={catalog.data.status} /></div>
          <div className="canonical-v13-card-list">
            {catalog.data.items.map((strategy) => (
              <button
                aria-pressed={selected === strategy.strategy_id}
                className="canonical-v13-select-card"
                key={strategy.strategy_id}
                onClick={() => selectStrategy(strategy.strategy_id)}
                type="button"
              >
                <strong>{strategy.display_name}</strong>
                <span>{strategy.strategy_id}</span>
                <span>{strategy.validation_status} · {strategy.qualification_status}</span>
              </button>
            ))}
          </div>
        </section>
      ) : null}
      {selected && url.valid && catalogContractKnown ? (
        <><button className="canonical-v13-text-button" onClick={() => selectStrategy(null)} type="button">清除所选策略</button><StrategyDetail strategyId={selected} /></>
      ) : (
        <CanonicalStatePanel description="请显式选择 strategy；UI 不会自动选择第一项。" kind="empty" title="尚未选择策略" />
      )}
    </div>
  );
}
