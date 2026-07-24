import { useParams } from "react-router-dom";

import { combineDataSources } from "../api/sourceState";
import type { DataSourceTraceSummary } from "../api/types";
import { useMvpData } from "../api/useMvpData";
import {
  CompactText,
  CopyableValue,
  EmptyState,
  ExpandableText,
  PageHeader,
  StatusBadge,
} from "../components/DisplayPrimitives";
import "../styles/strategies.css";
import { FallbackNotice } from "./FallbackNotice";
import {
  formatDiffLabel,
  formatDiffValue,
  formatSourceTrace,
  formatTraceRecord,
  strategyAvailability,
} from "./strategyDisplay";
import { EMPTY_TEXT, displayLoadState, displayValue } from "./uiCopy";

function SourceTraceCard({
  label,
  source,
}: {
  label: string;
  source: DataSourceTraceSummary | undefined;
}) {
  return (
    <article className="strategy-trace-card" data-core-source={source?.coreData === true ? "true" : "false"}>
      <div className="strategy-card-heading">
        <div>
          <span>{label}</span>
          <strong>{source?.sourceType ?? "unknown"}</strong>
        </div>
        <StatusBadge
          label={source?.coreData ? "核心数据" : "非核心数据"}
          status={source?.coreData ? "ACCEPTABLE" : "NOT_ACCEPTABLE"}
        />
      </div>
      {source?.blockedReason ? (
        <div className="strategy-problem-line" role="alert">
          <StatusBadge status="BLOCKED" />
          <CompactText label="来源阻塞原因" value={source.blockedReason} />
        </div>
      ) : null}
      <dl className="strategy-trace-list">
        <div>
          <dt>数据库 ID</dt>
          <dd><CopyableValue label="数据库 ID" value={formatTraceRecord(source?.databaseIds)} /></dd>
        </div>
        <div>
          <dt>Artifact 引用</dt>
          <dd><CopyableValue label="Artifact 引用" value={formatTraceRecord(source?.artifactRefs)} /></dd>
        </div>
      </dl>
      <ExpandableText summary="查看完整来源追踪" value={formatSourceTrace(source)} />
    </article>
  );
}

export function StrategyDetail() {
  const { strategyId } = useParams();
  const { data, sources, isLoading, error } = useMvpData();
  const source = combineDataSources(sources, ["strategies", "failureReasons", "versionLineage"]);
  const strategy = data.strategies.find((item) => item.id === strategyId);
  const currentVersionId = strategy?.currentVersion?.id;
  const currentVersionTrace =
    data.strategyVersions.find((version) => version.id === currentVersionId)?.dataSource ??
    strategy?.currentVersion?.dataSource;
  const versionLineage = data.versionLineage
    .filter((entry) => entry.strategyId === strategy?.id)
    .sort((left, right) => left.versionNumber - right.versionNumber);
  const currentLineage = versionLineage.find((entry) => entry.id === currentVersionId);
  const currentDiffStatus = currentLineage
    ? currentLineage.hasParent
      ? "有父版本"
      : "无父版本"
    : "缺失";
  const currentDiffEntries = Object.entries(currentLineage?.diffSnapshot ?? {});
  const validationErrors = strategy?.currentVersion?.validationErrors ?? [];
  const failureReasons = data.failureReasons.filter((reason) => {
    if (reason.strategyId !== strategy?.id) {
      return false;
    }

    return !currentVersionId || reason.strategyVersionId === currentVersionId;
  });

  if (!strategy) {
    return (
      <section className="page strategy-page">
        <PageHeader
          title="策略详情"
          description="查看策略概要、当前版本和可追溯技术证据。"
          status={<StatusBadge label={displayLoadState(isLoading, source)} status={isLoading ? "RUNNING" : source} />}
        />
        <FallbackNotice
          context="策略详情查找、当前版本和失败原因。"
          error={error}
          isLoading={isLoading}
          source={source}
        />
        {isLoading ? (
          <EmptyState description="正在读取策略与版本信息。" title="正在加载策略" />
        ) : error ? (
          <EmptyState description="数据加载失败，当前无法确认该策略是否存在。" title="无法确认策略" />
        ) : (
          <EmptyState
            description={`没有找到 ID 为 ${strategyId ?? EMPTY_TEXT} 的真实核心策略记录。`}
            title="未找到策略"
          />
        )}
      </section>
    );
  }

  const availability = strategyAvailability(strategy);
  const problemCount = validationErrors.length + failureReasons.length + (strategy.dataSource?.blockedReason ? 1 : 0);

  return (
    <section className="page strategy-page">
      <PageHeader
        eyebrow="策略详情"
        title={strategy.name}
        description="概要与当前版本优先；来源、谱系和 Diff 可按需审计。"
        status={
          <>
            <StatusBadge showRaw status={strategy.status} />
            <StatusBadge label={displayLoadState(isLoading, source)} status={isLoading ? "RUNNING" : source} />
          </>
        }
      />
      <FallbackNotice
        context="策略详情、版本谱系、当前版本 diff 和失败原因。"
        error={error}
        isLoading={isLoading}
        source={source}
      />
      <section className="strategy-detail-overview" aria-label="策略与当前版本概要">
        <article className={availability.isProblem ? "strategy-overview-card strategy-overview-problem" : "strategy-overview-card"}>
          <span>当前是否可用</span>
          <StatusBadge showRaw status={availability.status} />
          <p>{availability.reason ?? "当前版本未发现阻塞或校验失败。"}</p>
        </article>
        <article className="strategy-overview-card">
          <span>当前版本</span>
          {strategy.currentVersion ? (
            <>
              <strong>v{strategy.currentVersion.versionNumber}</strong>
              <StatusBadge showRaw status={strategy.currentVersion.validationStatus} />
              <CopyableValue label="策略文件路径" value={strategy.currentVersion.filePath} />
            </>
          ) : (
            <>
              <StatusBadge label="无当前版本" status="MISSING" />
              <p>尚无可审计的当前版本与策略文件。</p>
            </>
          )}
        </article>
        <article className="strategy-overview-card">
          <span>策略概要</span>
          <strong>{displayValue(strategy.timeframe)}</strong>
          <p>{strategy.source === "ai_generated" ? "AI 生成" : displayValue(strategy.source)}</p>
          <CompactText label="策略标签" value={strategy.tags.join(", ") || EMPTY_TEXT} />
        </article>
      </section>

      <section className="strategy-description-panel">
        <div>
          <span>策略说明</span>
          <p>{displayValue(strategy.description)}</p>
        </div>
        <CopyableValue label="策略 ID" value={strategy.id} />
      </section>

      {problemCount > 0 ? (
        <aside className="strategy-problem-banner" role="alert">
          <StatusBadge status={availability.isProblem ? availability.status : "FAILED"} />
          <div>
            <strong>当前版本存在 {problemCount} 项阻塞或失败证据</strong>
            <p>{availability.reason ?? validationErrors[0]?.message ?? failureReasons[0]?.message}</p>
          </div>
        </aside>
      ) : null}

      <section className="detail-section">
        <div className="section-header">
          <h2>数据来源</h2>
          <span>按需审计</span>
        </div>
        <details className="strategy-section-disclosure">
          <summary>查看策略与当前版本的来源追踪</summary>
          <div className="strategy-trace-grid">
            <SourceTraceCard label="策略" source={strategy.dataSource} />
            <SourceTraceCard label="当前版本" source={currentVersionTrace} />
          </div>
        </details>
      </section>
      <section className="detail-section">
        <div className="section-header">
          <h2>版本谱系</h2>
          <span>{versionLineage.length}</span>
        </div>
        {versionLineage.length > 0 ? (
          <ol className="strategy-lineage-list">
            {versionLineage.map((entry) => (
              <li
                className={entry.id === currentVersionId ? "strategy-lineage-current" : undefined}
                key={entry.id}
              >
                <details>
                  <summary>
                    <strong>版本 {entry.versionNumber}</strong>
                    {entry.id === currentVersionId ? <StatusBadge label="当前版本" status="READY" /> : null}
                    <CompactText label="版本变更摘要" value={entry.changeSummary ?? "暂无变更摘要。"} />
                  </summary>
                  <dl className="strategy-lineage-meta">
                    <div>
                      <dt>版本 ID</dt>
                      <dd><CopyableValue label="版本 ID" value={entry.id} /></dd>
                    </div>
                    <div>
                      <dt>父版本 ID</dt>
                      <dd><CopyableValue label="父版本 ID" value={entry.parentVersionId} /></dd>
                    </div>
                    <div>
                      <dt>完整变更摘要</dt>
                      <dd><ExpandableText value={entry.changeSummary ?? "暂无变更摘要。"} /></dd>
                    </div>
                  </dl>
                </details>
              </li>
            ))}
          </ol>
        ) : (
          <EmptyState description="该策略尚无可追溯的版本关系。" title="暂无版本谱系" />
        )}
      </section>
      <section className="detail-section">
        <div className="section-header">
          <h2>当前版本 Diff</h2>
          <span>{currentDiffStatus}</span>
        </div>
        {currentLineage ? (
          <details className="strategy-section-disclosure">
            <summary>查看当前版本 Diff 与完整变更值</summary>
            <div className="strategy-diff-panel">
              <dl className="strategy-lineage-meta">
                <div>
                  <dt>父版本 ID</dt>
                  <dd><CopyableValue label="父版本 ID" value={currentLineage.parentVersionId} /></dd>
                </div>
                <div>
                  <dt>摘要</dt>
                  <dd><ExpandableText value={currentLineage.changeSummary ?? "暂无 Diff 摘要。"} /></dd>
                </div>
              </dl>
              {currentDiffEntries.length > 0 ? (
                <dl className="strategy-diff-grid">
                  {currentDiffEntries.map(([key, value]) => (
                    <div key={key}>
                      <dt>{formatDiffLabel(key)}</dt>
                      <dd>
                        <ExpandableText
                          mono
                          summary="查看完整变更"
                          value={formatDiffValue(value)}
                        />
                      </dd>
                    </div>
                  ))}
                </dl>
              ) : (
                <EmptyState description="当前版本没有记录字段级 Diff。" title="暂无 Diff 快照" />
              )}
            </div>
          </details>
        ) : (
          <EmptyState description="当前版本没有对应的谱系记录，无法展示 Diff。" title="暂无 Diff 数据" />
        )}
      </section>
      <section className="detail-section">
        <div className="section-header">
          <h2>校验错误</h2>
          <span>{validationErrors.length}</span>
        </div>
        {validationErrors.length > 0 ? (
          <ul className="strategy-issue-list">
            {validationErrors.map((error) => (
              <li key={`${error.field ?? "strategy"}-${error.code ?? error.message}`}>
                <div className="strategy-card-heading">
                  <strong>{error.field ?? "策略"}</strong>
                  <StatusBadge status="FAILED" />
                </div>
                <CompactText label="校验错误" value={error.message} />
                {error.code ? <CopyableValue label="错误代码" value={error.code} /> : null}
                <ExpandableText summary="查看完整错误" value={error.message} />
              </li>
            ))}
          </ul>
        ) : (
          <EmptyState description="当前版本没有记录校验错误。" title="暂无校验错误" />
        )}
      </section>
      <section className="detail-section">
        <div className="section-header">
          <h2>失败原因</h2>
          <span>{failureReasons.length}</span>
        </div>
        {failureReasons.length > 0 ? (
          <ul className="strategy-issue-list">
            {failureReasons.map((reason) => (
              <li key={reason.id}>
                <div className="strategy-card-heading">
                  <strong>{reason.stage}</strong>
                  <StatusBadge showRaw status={reason.severity} />
                </div>
                <CompactText label="失败原因" value={reason.message} />
                <CopyableValue label="原因类型" value={reason.reasonType} />
                <ExpandableText summary="查看完整失败原因" value={reason.message} />
              </li>
            ))}
          </ul>
        ) : (
          <EmptyState description="当前版本没有持久化失败原因。" title="暂无失败原因" />
        )}
      </section>
    </section>
  );
}
