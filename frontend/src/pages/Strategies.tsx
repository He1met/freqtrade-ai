import { Link } from "react-router-dom";

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
import { formatSourceTrace, formatTraceRecord, strategyAvailability } from "./strategyDisplay";
import { EMPTY_TEXT, displayLoadState, displayValue } from "./uiCopy";

function StrategyTechnicalDetails({
  id,
  path,
  source,
}: {
  id: string;
  path: string | null | undefined;
  source: DataSourceTraceSummary | undefined;
}) {
  const sourceType = source?.sourceType ?? "unknown";

  return (
    <details className="strategy-technical-details">
      <summary>
        <span>{sourceType}</span>
        <StatusBadge
          label={source?.coreData ? "核心数据" : "非核心数据"}
          status={source?.coreData ? "ACCEPTABLE" : "NOT_ACCEPTABLE"}
        />
      </summary>
      <dl>
        <div>
          <dt>策略 ID</dt>
          <dd><CopyableValue label="策略 ID" value={id} /></dd>
        </div>
        <div>
          <dt>策略文件</dt>
          <dd><CopyableValue label="策略文件路径" value={path} /></dd>
        </div>
        <div>
          <dt>数据库 ID</dt>
          <dd><CopyableValue label="数据库 ID" value={formatTraceRecord(source?.databaseIds)} /></dd>
        </div>
        <div>
          <dt>来源详情</dt>
          <dd><ExpandableText summary="查看完整来源" value={formatSourceTrace(source)} /></dd>
        </div>
      </dl>
    </details>
  );
}

export function Strategies() {
  const { data, sources, isLoading, error } = useMvpData();
  const source = combineDataSources(sources, ["strategies", "strategyVersions"]);

  return (
    <section className="page strategy-page">
      <PageHeader
        title="策略"
        description="优先查看策略状态、当前版本和 Timeframe；路径与来源追踪按需展开。"
        status={<StatusBadge label={displayLoadState(isLoading, source)} status={isLoading ? "RUNNING" : source} />}
      />
      <FallbackNotice
        context="策略列表、状态、timeframe、来源和版本文件路径。"
        error={error}
        isLoading={isLoading}
        source={source}
      />
      <div className="table-shell strategy-list-table-shell">
        <table className="strategy-list-table">
          <colgroup>
            <col className="strategies-col-name" />
            <col className="strategies-col-status" />
            <col className="strategies-col-version" />
            <col className="strategies-col-timeframe" />
            <col className="strategies-col-technical" />
          </colgroup>
          <thead>
            <tr>
              <th>名称</th>
              <th>状态</th>
              <th>当前版本</th>
              <th>Timeframe</th>
              <th>路径与来源</th>
            </tr>
          </thead>
          <tbody>
            {data.strategies.map((strategy) => {
              const availability = strategyAvailability(strategy);
              const firstValidationError = strategy.currentVersion?.validationErrors[0]?.message;
              return (
                <tr data-problem={availability.isProblem ? "true" : "false"} key={strategy.id}>
                  <td>
                    <Link
                      aria-label={`查看策略：${strategy.name}`}
                      className="table-link strategy-name-link"
                      to={`/strategies/${strategy.id}`}
                    >
                      {strategy.name}
                    </Link>
                    <CompactText
                      className="strategy-name-secondary"
                      label="策略说明"
                      value={strategy.description}
                    />
                    <CompactText
                      className="strategy-name-secondary"
                      label="策略标签"
                      value={strategy.tags.join(", ") || EMPTY_TEXT}
                    />
                  </td>
                  <td>
                    <div className="strategy-status-stack">
                      <StatusBadge showRaw status={strategy.status} />
                      {availability.isProblem ? (
                        <CompactText
                          className="strategy-inline-problem"
                          label="当前不可用原因"
                          value={availability.reason}
                        />
                      ) : null}
                    </div>
                  </td>
                  <td>
                    {strategy.currentVersion ? (
                      <div className="strategy-version-stack">
                        <strong>v{strategy.currentVersion.versionNumber}</strong>
                        <StatusBadge showRaw status={strategy.currentVersion.validationStatus} />
                        {firstValidationError ? (
                          <CompactText
                            className="strategy-inline-problem"
                            label="校验错误"
                            value={firstValidationError}
                          />
                        ) : null}
                      </div>
                    ) : (
                      <StatusBadge label="无当前版本" status="MISSING" />
                    )}
                  </td>
                  <td><strong>{displayValue(strategy.timeframe)}</strong></td>
                  <td>
                    <StrategyTechnicalDetails
                      id={strategy.id}
                      path={strategy.currentVersion?.filePath}
                      source={strategy.dataSource}
                    />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {!isLoading && data.strategies.length === 0 ? (
        <EmptyState
          description="当前没有可展示的真实核心策略记录；空结果不代表策略生成成功。"
          title="暂无真实策略"
        />
      ) : null}
    </section>
  );
}
