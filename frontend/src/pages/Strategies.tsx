import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { combineDataSources } from "../api/sourceState";
import type { DataSourceTraceSummary } from "../api/types";
import {
  fetchFormalResearchRun,
  fetchStrategyResearchBatches,
  startFormalResearchRun,
  type FormalResearchRun,
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
import {
  canStartFormalResearch,
  deploymentHandoffText,
  validatedCandidateCount,
} from "./strategyFactoryModel";
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
  const [formalRun, setFormalRun] = useState<FormalResearchRun | null>(null);
  const [formalRunError, setFormalRunError] = useState<string | null>(null);
  const [startingResearch, setStartingResearch] = useState(false);

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

  useEffect(() => {
    const controller = new AbortController();
    const refresh = () => {
      fetchFormalResearchRun(controller.signal)
        .then((run) => {
          setFormalRun(run);
          setFormalRunError(null);
          if (run.status === "COMPLETED" || run.status === "FAILED") {
            return fetchStrategyResearchBatches(controller.signal).then(setResearchBatches);
          }
          return undefined;
        })
        .catch((reason: unknown) => {
          if (!controller.signal.aborted) {
            setFormalRunError(reason instanceof Error ? reason.message : String(reason));
          }
        });
    };
    refresh();
    const timer = window.setInterval(refresh, 3000);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, []);

  async function runFormalResearch() {
    setStartingResearch(true);
    setFormalRunError(null);
    try {
      const run = await startFormalResearchRun();
      setFormalRun(run);
      if (run.status === "BLOCKED") setFormalRunError(`${run.reason_code}：${run.reason}`);
    } catch (reason) {
      setFormalRunError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setStartingResearch(false);
    }
  }

  const latestResearch = researchBatches[0];

  return (
    <section className="page strategy-page">
      <PageHeader
        title="策略工厂"
        description="正式候选研究、完整验证、全量持久化与自动部署评审交接；Local Strategy Lab 不属于此生命周期。"
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
            <h2>正式研究控制</h2>
            <p>手动运行与每 15 分钟定时器共用同一正式入口；固定请求 10 条，并执行完整验证、全量入库和既有自动部署评审交接。</p>
          </div>
          <StatusBadge
            label={researchLoading ? "读取中" : latestResearch?.status ?? "尚未生成"}
            status={researchLoading ? "RUNNING" : latestResearch?.status ?? "MISSING"}
          />
        </div>
        <div className="strategy-factory-control">
          <div>
            <StatusBadge
              label={formalRun?.reason_code ?? (formalRunError ? "STATUS_UNKNOWN" : "读取门禁")}
              status={formalRun?.status ?? (formalRunError ? "UNKNOWN" : "RUNNING")}
              showRaw
            />
            <p>{formalRunError ?? formalRun?.reason ?? "正在读取正式研究门禁。"}</p>
          </div>
          <button
            className="strategy-factory-run-button"
            disabled={!canStartFormalResearch(formalRun, startingResearch)}
            onClick={() => void runFormalResearch()}
            type="button"
          >
            {startingResearch ? "正在提交…" : "手动运行一轮研究（10 条）"}
          </button>
        </div>
        <p className="strategy-factory-safety">
          本页不收集 operator token、Provider token 或任何凭据；不授权实盘、Dry-run 交易、grant 或手动下单。固定 OKX_DEMO、allow_real_funds=false、real_orders=false。
        </p>
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
              <div><dt>请求</dt><dd>{latestResearch.requested_count}</dd></div>
              <div><dt>本轮生成</dt><dd>{latestResearch.generated_count}</dd></div>
              <div><dt>完成验证</dt><dd>{validatedCandidateCount(latestResearch)}</dd></div>
              <div><dt>持久化入库</dt><dd>{latestResearch.persisted_count}</dd></div>
              <div><dt>合格</dt><dd>{latestResearch.qualified_count}</dd></div>
              <div><dt>拒绝</dt><dd>{latestResearch.rejected_count}</dd></div>
            </dl>
            <p className="strategy-research-freshness">
              自动部署任务：{deploymentHandoffText(latestResearch)}。
            </p>
            <p className="strategy-research-freshness">
              最近持久化批次：{latestResearch.completed_at ?? latestResearch.created_at}。该时间之后若自动化因所有权门禁停止，必须查运行记忆，不能把旧批次当成本小时已生成。
            </p>
            {latestResearch.failure_reason ? (
              <p className="strategy-inline-problem">
                失败原因：{latestResearch.failure_reason}
              </p>
            ) : null}
            {researchBatches.map((batch) => (
              <details className="strategy-technical-details" key={batch.id}>
              <summary>批次 {batch.run_id} · requested {batch.requested_count} · generated {batch.generated_count} · persisted {batch.persisted_count}</summary>
              {batch.failure_reason ? <p className="strategy-inline-problem">失败原因：{batch.failure_reason}</p> : null}
              <ul className="strategy-research-candidates">
                {batch.candidates.map((candidate) => (
                  <li key={candidate.id}>
                    <div>
                      <strong>{candidate.candidate_name}</strong>
                      <StatusBadge showRaw status={candidate.status} />
                    </div>
                    <p>
                      loadable={String(candidate.loadable)} · static={candidate.static_check} · lookahead={candidate.lookahead_status} · validation={String(candidate.validation_passed)} · score={candidate.score ?? "MISSING"}
                    </p>
                    <ExpandableText
                      summary="查看结构化候选证据"
                      value={JSON.stringify(candidate.evidence_snapshot, null, 2)}
                    />
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
              <CopyableValue label="研究报告路径" value={batch.report_path} />
              <CopyableValue label="研究报告摘要" value={batch.report_digest} />
            </details>
            ))}
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
