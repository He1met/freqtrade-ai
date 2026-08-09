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
  hasOfficialAggressiveContract,
  validatedCandidateCount,
} from "./strategyFactoryModel";
import { displayDateTime, displayValue } from "./uiCopy";

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
        <span>来源与路径</span>
        <StatusBadge label={source?.coreData ? "核心数据" : sourceType} status={source?.coreData ? "ACCEPTABLE" : "UNKNOWN"} />
      </summary>
      <dl>
        <div><dt>策略 ID</dt><dd><CopyableValue label="策略 ID" value={id} /></dd></div>
        <div><dt>策略文件</dt><dd><CopyableValue label="策略文件路径" value={path} /></dd></div>
        <div><dt>数据库 ID</dt><dd><CopyableValue label="数据库 ID" value={formatTraceRecord(source?.databaseIds)} /></dd></div>
        <div><dt>来源详情</dt><dd><ExpandableText summary="查看完整来源" value={formatSourceTrace(source)} /></dd></div>
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

  const latestResearch = researchBatches[0] ?? null;
  const latestResearchRun = formalRun?.run_id === latestResearch?.run_id ? formalRun : null;
  const currentResearchStatus = researchLoading
    ? "RUNNING"
    : researchError || formalRunError
      ? "UNKNOWN"
      : formalRun?.status ?? latestResearch?.status ?? "NOT_RUN";
  const currentResearchLabel = researchLoading
    ? "读取中"
    : researchError || formalRunError
      ? "状态未知"
      : formalRun?.status === "READY"
        ? "可以运行"
        : formalRun?.status ?? latestResearch?.status ?? "尚未开始";

  return (
    <section className="page strategy-page formal-page">
      <PageHeader
        actions={(
          <button
            aria-describedby="formal-research-safety"
            className="formal-primary-button"
            disabled={!canStartFormalResearch(formalRun, startingResearch)}
            onClick={() => void runFormalResearch()}
            type="button"
          >
            {startingResearch ? "正在提交…" : "手动运行一轮研究（10 条）"}
          </button>
        )}
        description="正式候选从生成、验证、入库到部署评审的唯一入口；Local Strategy Lab 不计入此生命周期。"
        eyebrow="正式工作台"
        status={<StatusBadge label={currentResearchLabel} status={currentResearchStatus} />}
        title="策略工厂"
      />

      <nav className="formal-tabs" aria-label="策略工厂分区">
        <a className="formal-tab-link" href="#latest-research">最新研究</a>
        <a className="formal-tab-link" href="#research-candidates">候选</a>
        <a className="formal-tab-link" href="#strategy-library">策略库</a>
        <a className="formal-tab-link" href="#strategy-ranking">排行榜</a>
      </nav>

      <section className="formal-panel" id="latest-research" aria-labelledby="latest-research-title">
        <div className="formal-control-row">
          <div>
            <span className="formal-kicker">正式研究门禁</span>
            <h2 id="latest-research-title">{formalRun?.reason_code ?? (formalRunError ? "状态未知" : "正在读取")}</h2>
            <p>{formalRunError ?? formalRun?.reason ?? "正在读取正式研究 coordinator 状态。"}</p>
          </div>
          <StatusBadge label={currentResearchLabel} status={currentResearchStatus} />
        </div>
        <p className="formal-muted" id="formal-research-safety">
          固定 OKX_DEMO；不收集凭据，不授权 Dry-run、grant、手动下单或真实资金。提交超时后结果未知，页面先核对状态，不自动重试。
        </p>
        <details className="formal-disclosure">
          <summary>查看正式质量契约</summary>
          <p className="formal-muted">
            {formalRun?.quality_contract.profile_label ?? "质量契约尚未读取"}；要求独立窗口成本后净收益为正、lookahead 检查、费用 0.05%/侧、滑点 0.02%/侧，最大回撤门保持 15%。契约校验：{hasOfficialAggressiveContract(formalRun) ? "匹配" : "未确认"}。
          </p>
        </details>
      </section>

      <FallbackNotice
        context="策略目录、版本、状态和排行榜。"
        error={error}
        isLoading={isLoading}
        source={source}
      />

      <section className="formal-panel" aria-labelledby="research-progress-title">
        <div className="formal-section-heading">
          <div><span className="formal-kicker">最新批次</span><h2 id="research-progress-title">生成、验证与全量入库</h2></div>
          <span className="formal-section-note">{latestResearch ? displayDateTime(latestResearch.completed_at ?? latestResearch.created_at) : "尚无批次"}</span>
        </div>
        {researchLoading ? (
          <div className="formal-lifecycle formal-skeleton" aria-label="正在读取研究批次" />
        ) : researchError ? (
          <EmptyState title="研究批次状态未知" description="候选批次 API 读取失败；这不表示没有生成，也不表示候选已被拒绝。" />
        ) : latestResearch ? (
          <>
            <div className="formal-lifecycle">
              {[
                ["请求", latestResearch.requested_count],
                ["生成", latestResearch.generated_count],
                ["验证", validatedCandidateCount(latestResearch)],
                ["入库", latestResearch.persisted_count],
                ["合格", latestResearch.qualified_count],
                ["拒绝", latestResearch.rejected_count],
              ].map(([label, value], index) => (
                <div className="formal-lifecycle-step" key={String(label)}>
                  <span>{index + 1}</span><div><small>{label}</small><strong>{value}</strong></div>
                </div>
              ))}
            </div>
            <div className="formal-panel-footer">
              <span>{deploymentHandoffText(latestResearchRun)}</span>
              <StatusBadge status={latestResearch.status} />
            </div>
            {latestResearch.failure_reason ? <p className="formal-problem">失败原因：{latestResearch.failure_reason}</p> : null}
          </>
        ) : (
          <EmptyState title="尚无持久化研究批次" description="数据库中没有正式批次，表示尚未完成生成与入库；不能解释为 10 条候选已验证不合格。" />
        )}
      </section>

      <section className="formal-panel" id="research-candidates" aria-labelledby="research-candidates-title">
        <div className="formal-section-heading">
          <div><span className="formal-kicker">正式候选</span><h2 id="research-candidates-title">合格、拒绝与验证失败</h2></div>
          <span className="formal-section-note">QUALIFIED 不等于已部署</span>
        </div>
        {latestResearch?.candidates.length ? (
          <ul className="formal-candidate-list">
            {latestResearch.candidates.map((candidate) => {
              const primaryReason = candidate.rejection_reasons[0];
              const qualified = candidate.status === "QUALIFIED"
                && candidate.validation_passed
                && candidate.deployable_candidate;
              return (
                <li className="formal-candidate-card" key={candidate.id}>
                  <header><h3>{candidate.candidate_name}</h3><StatusBadge status={candidate.status} /></header>
                  <p>Score {candidate.score ?? "暂无"} · Lookahead {candidate.lookahead_status} · Static {candidate.static_check}</p>
                  <p className="formal-candidate-reason">
                    {primaryReason
                      ? `${primaryReason.code}：${primaryReason.message}`
                      : qualified
                        ? "研究质量门已通过；是否进入部署评审以权威交接状态为准。"
                        : "拒绝或验证证据缺失，不能视为质量门已通过。"}
                  </p>
                  <details className="formal-disclosure">
                    <summary>查看技术证据</summary>
                    <CopyableValue label="候选摘要" value={candidate.code_digest} />
                    <ExpandableText summary="完整结构化证据" value={JSON.stringify(candidate.evidence_snapshot, null, 2)} />
                  </details>
                </li>
              );
            })}
          </ul>
        ) : researchLoading ? (
          <div className="formal-compact-skeleton formal-skeleton" />
        ) : (
          <EmptyState title="当前没有候选行" description="无候选可能表示尚未生成或在生成前失败；请结合最新批次状态判断。" />
        )}
        {researchBatches.length ? (
          <details className="formal-disclosure">
            <summary>查看历史批次与报告证据</summary>
            {researchBatches.map((batch) => (
              <article className="strategy-history-batch" key={batch.id}>
                <div><strong>{batch.run_id}</strong><StatusBadge status={batch.status} /></div>
                <p>请求 {batch.requested_count} · 生成 {batch.generated_count} · 入库 {batch.persisted_count} · 合格 {batch.qualified_count}</p>
                <CopyableValue label="研究报告路径" value={batch.report_path} />
                <CopyableValue label="研究报告摘要" value={batch.report_digest} />
              </article>
            ))}
          </details>
        ) : null}
      </section>

      <section className="formal-panel" id="strategy-library" aria-labelledby="strategy-library-title">
        <div className="formal-section-heading">
          <div><span className="formal-kicker">正式策略库</span><h2 id="strategy-library-title">目录状态与当前版本</h2></div>
          <span className="formal-section-note">目录 active 不等于部署 ACTIVE</span>
        </div>
        {isLoading ? <div className="formal-compact-skeleton formal-skeleton" /> : null}
        {!isLoading && data.strategies.length ? (
          <div className="table-shell strategy-list-table-shell">
            <table className="strategy-list-table">
              <colgroup>
                <col className="strategies-col-name" /><col className="strategies-col-status" />
                <col className="strategies-col-version" /><col className="strategies-col-timeframe" />
                <col className="strategies-col-technical" />
              </colgroup>
              <thead><tr><th>策略</th><th>目录状态</th><th>当前版本</th><th>周期</th><th>证据</th></tr></thead>
              <tbody>
                {data.strategies.map((strategy) => {
                  const availability = strategyAvailability(strategy);
                  const firstValidationError = strategy.currentVersion?.validationErrors[0]?.message;
                  return (
                    <tr data-problem={availability.isProblem ? "true" : "false"} key={strategy.id}>
                      <td>
                        <Link aria-label={`查看策略：${strategy.name}`} className="table-link strategy-name-link" to={`/strategies/${strategy.id}`}>{strategy.name}</Link>
                        <CompactText className="strategy-name-secondary" label="策略说明" value={strategy.description} />
                      </td>
                      <td><div className="strategy-status-stack"><StatusBadge status={strategy.status} />{availability.reason ? <CompactText className="strategy-inline-problem" label="当前不可用原因" value={availability.reason} /> : null}</div></td>
                      <td>
                        {strategy.currentVersion ? (
                          <div className="strategy-version-stack"><strong>v{strategy.currentVersion.versionNumber}</strong><StatusBadge status={strategy.currentVersion.validationStatus} />{firstValidationError ? <CompactText className="strategy-inline-problem" label="校验错误" value={firstValidationError} /> : null}</div>
                        ) : <StatusBadge label="无当前版本" status="MISSING" />}
                      </td>
                      <td><strong>{displayValue(strategy.timeframe)}</strong></td>
                      <td><StrategyTechnicalDetails id={strategy.id} path={strategy.currentVersion?.filePath} source={strategy.dataSource} /></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : null}
        {!isLoading && data.strategies.length === 0 ? <EmptyState title="暂无真实策略" description="当前没有可展示的真实核心策略记录；空结果不代表策略生成成功。" /> : null}
      </section>

      <section className="formal-panel" id="strategy-ranking" aria-labelledby="strategy-ranking-title">
        <div className="formal-section-heading">
          <div><span className="formal-kicker">排行榜</span><h2 id="strategy-ranking-title">真实评分策略</h2></div>
          <Link className="formal-text-link" to="/ranking">查看完整证据</Link>
        </div>
        {data.ranking.length ? (
          <ol className="formal-ranking-list">
            {data.ranking.slice(0, 5).map((entry) => (
              <li key={entry.scoreId}>
                <span>#{entry.rank}</span>
                <strong>{entry.strategyName} · v{entry.versionNumber}</strong>
                <span>{entry.totalScore.toFixed(1)} 分</span>
              </li>
            ))}
          </ol>
        ) : (
          <EmptyState title="暂无真实排名" description="没有与真实 BacktestResult/StrategyScore 关联的核心排名；研究候选 score 不会自动进入这里。" />
        )}
      </section>
    </section>
  );
}
