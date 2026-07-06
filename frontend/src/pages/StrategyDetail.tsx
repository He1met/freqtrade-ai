import { useParams } from "react-router-dom";

import { combineDataSources } from "../api/sourceState";
import { useMvpData } from "../api/useMvpData";
import { FallbackNotice } from "./FallbackNotice";
import { SourceMarker } from "./SourceMarker";
import { EMPTY_TEXT, displayLoadState, displayStatus } from "./uiCopy";

function formatDiffLabel(label: string) {
  return label.split("_").join(" ");
}

function formatDiffValue(value: unknown) {
  if (Array.isArray(value)) {
    return value.length > 0 ? value.map((item) => String(item)).join(", ") : EMPTY_TEXT;
  }

  if (value === null || value === undefined || value === "") {
    return EMPTY_TEXT;
  }

  if (typeof value === "object") {
    return JSON.stringify(value);
  }

  return String(value);
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
      <section className="page">
        <header className="page-header">
          <h1>策略详情</h1>
          <span className="status-pill">{displayLoadState(isLoading, source)}</span>
        </header>
        <FallbackNotice
          context="策略详情查找、当前版本和失败原因。"
          error={error}
          isLoading={isLoading}
          source={source}
        />
        <div className="empty-state">未找到策略。</div>
      </section>
    );
  }

  return (
    <section className="page">
      <header className="page-header">
        <h1>{strategy.name}</h1>
        <span className="status-pill">{displayLoadState(isLoading, source)}</span>
      </header>
      <FallbackNotice
        context="策略详情、版本谱系、当前版本 diff 和失败原因。"
        error={error}
        isLoading={isLoading}
        source={source}
      />
      <dl className="detail-list">
        <div>
          <dt>ID</dt>
          <dd>{strategy.id}</dd>
        </div>
        <div>
          <dt>状态</dt>
          <dd>{displayStatus(strategy.status)}</dd>
        </div>
        <div>
          <dt>Timeframe</dt>
          <dd>{strategy.timeframe}</dd>
        </div>
        <div>
          <dt>当前版本</dt>
          <dd>{strategy.currentVersion?.versionNumber ?? EMPTY_TEXT}</dd>
        </div>
        <div>
          <dt>策略文件</dt>
          <dd>
            <code>{strategy.currentVersion?.filePath ?? EMPTY_TEXT}</code>
          </dd>
        </div>
        <div>
          <dt>说明</dt>
          <dd>{strategy.description}</dd>
        </div>
        <div>
          <dt>标签</dt>
          <dd>{strategy.tags.join(", ") || EMPTY_TEXT}</dd>
        </div>
      </dl>
      <section className="detail-section">
        <div className="section-header">
          <h2>数据来源</h2>
          <span>DB trace</span>
        </div>
        <div className="source-grid">
          <SourceMarker label="strategy" source={strategy.dataSource} />
          <SourceMarker label="current version" source={currentVersionTrace} />
        </div>
      </section>
      <section className="detail-section">
        <div className="section-header">
          <h2>版本谱系</h2>
          <span>{versionLineage.length}</span>
        </div>
        {versionLineage.length > 0 ? (
          <ol className="lineage-list">
            {versionLineage.map((entry) => (
              <li
                className={
                  entry.id === currentVersionId
                    ? "lineage-item lineage-item-current"
                    : "lineage-item"
                }
                key={entry.id}
              >
                <div className="lineage-heading">
                  <strong>版本 {entry.versionNumber}</strong>
                  {entry.id === currentVersionId ? (
                    <span className="status-pill">当前</span>
                  ) : null}
                </div>
                <dl className="lineage-meta">
                  <div>
                    <dt>父版本</dt>
                    <dd>{entry.parentVersionId ?? EMPTY_TEXT}</dd>
                  </div>
                  <div>
                    <dt>变更</dt>
                    <dd>{entry.changeSummary ?? "暂无变更摘要。"}</dd>
                  </div>
                </dl>
              </li>
            ))}
          </ol>
        ) : (
          <div className="empty-state">该策略暂无版本谱系记录。</div>
        )}
      </section>
      <section className="detail-section">
        <div className="section-header">
          <h2>当前版本 Diff</h2>
          <span>{currentDiffStatus}</span>
        </div>
        {currentLineage ? (
          <div className="diff-panel">
            <dl className="lineage-meta">
              <div>
                <dt>父版本</dt>
                <dd>{currentLineage.parentVersionId ?? EMPTY_TEXT}</dd>
              </div>
              <div>
                <dt>摘要</dt>
                <dd>{currentLineage.changeSummary ?? "暂无 Diff 摘要。"}</dd>
              </div>
            </dl>
            {currentDiffEntries.length > 0 ? (
              <dl className="diff-grid">
                {currentDiffEntries.map(([key, value]) => (
                  <div key={key}>
                    <dt>{formatDiffLabel(key)}</dt>
                    <dd>{formatDiffValue(value)}</dd>
                  </div>
                ))}
              </dl>
            ) : (
              <div className="empty-state">该版本暂无 Diff 快照。</div>
            )}
          </div>
        ) : (
          <div className="empty-state">当前版本暂无 Diff 数据。</div>
        )}
      </section>
      <section className="detail-section">
        <div className="section-header">
          <h2>校验错误</h2>
          <span>{validationErrors.length}</span>
        </div>
        {validationErrors.length > 0 ? (
          <ul className="issue-list">
            {validationErrors.map((error) => (
              <li key={`${error.field ?? "strategy"}-${error.code ?? error.message}`}>
                <strong>{error.field ?? "策略"}</strong>
                <span>{error.message}</span>
                {error.code ? <code>{error.code}</code> : null}
              </li>
            ))}
          </ul>
        ) : (
          <div className="empty-state">该版本暂无校验错误。</div>
        )}
      </section>
      <section className="detail-section">
        <div className="section-header">
          <h2>失败原因</h2>
          <span>{failureReasons.length}</span>
        </div>
        {failureReasons.length > 0 ? (
          <ul className="issue-list">
            {failureReasons.map((reason) => (
              <li key={reason.id}>
                <div className="reason-heading">
                  <strong>{reason.stage}</strong>
                  <span className={`severity severity-${reason.severity}`}>{displayStatus(reason.severity)}</span>
                </div>
                <span>{reason.message}</span>
                <code>{reason.reasonType}</code>
              </li>
            ))}
          </ul>
        ) : (
          <div className="empty-state">该版本暂无失败原因记录。</div>
        )}
      </section>
    </section>
  );
}
