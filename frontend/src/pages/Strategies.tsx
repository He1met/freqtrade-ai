import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { combineDataSources } from "../api/sourceState";
import type { DataSourceTraceSummary } from "../api/types";
import {
  fetchStrategyResearchBatches,
  type StrategyResearchBatch,
} from "../api/strategyResearchApi";
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
  const [researchBatches, setResearchBatches] = useState<StrategyResearchBatch[]>([]);
  const [researchLoading, setResearchLoading] = useState(true);
  const [researchError, setResearchError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    fetchStrategyResearchBatches(controller.signal)
      .then((batches) => {
        setResearchBatches(batches);
        setResearchError(null);
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) {
          setResearchError(reason instanceof Error ? reason.message : String(reason));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setResearchLoading(false);
      });
    return () => controller.abort();
  }, []);

  const latestResearch = researchBatches[0];

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
      <section className="strategy-research-summary" aria-label="研究候选批次">
        <div className="strategy-research-summary__heading">
          <div>
            <h2>研究候选（不计入正式策略数）</h2>
            <p>候选生成、验证和拒绝证据独立持久化；只有统一质量门全部通过才进入部署评审。</p>
          </div>
          <StatusBadge
            label={researchLoading ? "读取中" : latestResearch?.status ?? "尚未生成"}
            status={researchLoading ? "RUNNING" : latestResearch?.status ?? "MISSING"}
          />
        </div>
        {researchError ? (
          <EmptyState
            title="研究批次状态未知"
            description={`候选批次 API 读取失败：${researchError}。这不表示没有生成，也不表示候选已被拒绝。`}
          />
        ) : null}
        {!researchLoading && !researchError && !latestResearch ? (
          <EmptyState
            title="尚无持久化研究批次"
            description="数据库中没有研究批次，表示尚未完成生成与入库；不能解释为 10 条候选已验证不合格。"
          />
        ) : null}
        {latestResearch ? (
          <>
            <dl className="strategy-research-counts">
              <div><dt>本轮生成</dt><dd>{latestResearch.generated_count}</dd></div>
              <div><dt>持久化入库</dt><dd>{latestResearch.persisted_count}</dd></div>
              <div><dt>合格</dt><dd>{latestResearch.qualified_count}</dd></div>
              <div><dt>拒绝</dt><dd>{latestResearch.rejected_count}</dd></div>
            </dl>
            <p className="strategy-research-freshness">
              最近持久化批次：{latestResearch.completed_at ?? latestResearch.created_at}。该时间之后若自动化因所有权门禁停止，必须查运行记忆，不能把旧批次当成本小时已生成。
            </p>
            {latestResearch.failure_reason ? (
              <p className="strategy-inline-problem">
                失败原因：{latestResearch.failure_reason}
              </p>
            ) : null}
            <details className="strategy-technical-details">
              <summary>查看批次 {latestResearch.run_id} 的候选与拒绝原因</summary>
              <ul className="strategy-research-candidates">
                {latestResearch.candidates.map((candidate) => (
                  <li key={candidate.id}>
                    <div>
                      <strong>{candidate.candidate_name}</strong>
                      <StatusBadge showRaw status={candidate.status} />
                    </div>
                    {candidate.rejection_reasons.length ? (
                      <ul>
                        {candidate.rejection_reasons.map((reason, index) => (
                          <li key={`${reason.code}-${index}`}>{reason.code}：{reason.message}</li>
                        ))}
                      </ul>
                    ) : <p>全部研究质量门通过，等待容量与唯一主任务评审。</p>}
                  </li>
                ))}
              </ul>
              <CopyableValue label="研究报告路径" value={latestResearch.report_path} />
              <CopyableValue label="研究报告摘要" value={latestResearch.report_digest} />
            </details>
          </>
        ) : null}
      </section>
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
