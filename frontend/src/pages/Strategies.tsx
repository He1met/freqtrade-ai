import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import type { DataSourceTraceSummary } from "../api/types";
import {
  fetchFormalResearchRun,
  fetchStrategyResearchBatches,
  startFormalResearchRun,
  type FormalResearchRun,
  type StrategyResearchBatch,
} from "../api/strategyResearchApi";
import { useFormalCatalogData } from "../api/useFormalCatalogData";
import { useFormalReadModels } from "../api/useFormalReadModels";
import {
  CompactText,
  CopyableValue,
  EmptyState,
  ExpandableText,
  FormalLoadingState,
  PageHeader,
  StatusBadge,
} from "../components/DisplayPrimitives";
import "../styles/strategies.css";
import { FallbackNotice } from "./FallbackNotice";
import { formatSourceTrace, formatTraceRecord, strategyAvailability } from "./strategyDisplay";
import {
  candidateLifecycleDisplay,
  candidateLifecycleFor,
  canStartFormalResearch,
  lifecycleSummaryLabel,
  lifecycleSummaryText,
  researchQualityContractText,
  validatedCandidateCount,
} from "./strategyFactoryModel";
import { displayDateTime, displayStatus, displayValue } from "./uiCopy";

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
  const { workspace, refresh: refreshReadModels } = useFormalReadModels();
  const catalog = useFormalCatalogData();
  const data = {
    strategies: catalog.strategies.data ?? [],
    ranking: catalog.ranking.data ?? [],
  };
  const isLoading = catalog.strategies.loading || catalog.ranking.loading;
  const error = catalog.strategies.error ?? catalog.ranking.error;
  const source = error ? "failed" : "api";
  const [researchBatches, setResearchBatches] = useState<StrategyResearchBatch[]>([]);
  const [researchLoading, setResearchLoading] = useState(true);
  const [researchError, setResearchError] = useState<string | null>(null);
  const [formalRun, setFormalRun] = useState<FormalResearchRun | null>(null);
  const [formalRunError, setFormalRunError] = useState<string | null>(null);
  const [startingResearch, setStartingResearch] = useState(false);
  const [researchRevision, setResearchRevision] = useState(0);

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
  }, [researchRevision]);

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
    } catch (reason) {
      setFormalRunError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setStartingResearch(false);
    }
  }

  const latestResearch = researchBatches[0] ?? null;
  const latestLifecycleSummary = workspace.data && workspace.data.latest_batch?.id === latestResearch?.id
    ? workspace.data.lifecycle_summary
    : null;
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
        : displayStatus(formalRun?.status ?? latestResearch?.status ?? "NOT_RUN");

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
            {startingResearch ? "正在提交…" : "手动运行一轮研究"}
          </button>
        )}
        description="正式候选从生成、验证、入库到部署评审的唯一入口；Local Strategy Lab 不计入此生命周期。"
        eyebrow="正式工作台"
        status={<StatusBadge label={currentResearchLabel} status={currentResearchStatus} />}
        title="策略工厂"
      />

      <nav className="formal-tabs" aria-label="策略工厂页内导航">
        <a aria-controls="latest-research" className="formal-tab-link" href="#latest-research">最新研究</a>
        <a aria-controls="research-candidates" className="formal-tab-link" href="#research-candidates">候选</a>
        <a aria-controls="strategy-library" className="formal-tab-link" href="#strategy-library">策略库</a>
        <a aria-controls="strategy-ranking" className="formal-tab-link" href="#strategy-ranking">排行榜</a>
      </nav>

      <section className="formal-panel" id="latest-research" aria-labelledby="latest-research-title">
        <div className="formal-control-row">
          <div>
            <span className="formal-kicker">正式研究门禁</span>
            <h2 id="latest-research-title">{formalRun ? displayStatus(formalRun.status) : (formalRunError ? "状态未知" : "正在读取")}</h2>
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
            {researchQualityContractText(formalRun)}
          </p>
        </details>
        {workspace.loading ? <p className="formal-muted">正在读取研究尝试与分钟数据质量证据…</p>
          : workspace.error ? <p className="formal-problem">研究生命周期投影读取失败，attempt 与质量状态未知。</p>
            : workspace.data ? (
              <p className="formal-evidence-summary-line">
                <span>最近尝试：<strong>{workspace.data.sections.attempts.status === "UNKNOWN" ? "未知" : workspace.data.attempts[0] ? displayStatus(workspace.data.attempts[0].latest_outcome) : "尚未尝试"}</strong></span>
                <span>分钟数据：<strong>{workspace.data.sections.quality.status === "UNKNOWN" ? "未知" : workspace.data.latest_quality_receipt ? displayStatus(workspace.data.latest_quality_receipt.status) : "尚无 receipt"}</strong></span>
                <span>生命周期衔接：<strong>{workspace.data.sections.bridge?.status === "AVAILABLE" ? lifecycleSummaryLabel(workspace.data.lifecycle_summary) : "未知"}</strong></span>
              </p>
            ) : null}
        {workspace.data?.sections.attempts.status === "AVAILABLE" && workspace.data.attempts[0] ? (
          <details className="formal-disclosure">
            <summary>查看最近研究尝试完整事件</summary>
            {workspace.data.attempts[0].events.map((event) => (
              <article className="strategy-history-batch" key={event.id}>
                <div><strong>{event.phase} · {event.reason_code}</strong><StatusBadge status={event.outcome} /></div>
                <p>{event.redacted_reason}</p>
                <p>请求 {event.requested_count} · 生成 {event.generated_count} · 验证 {event.validated_count} · 入库 {event.persisted_count} · 合格 {event.qualified_count} · 拒绝 {event.rejected_count}</p>
              </article>
            ))}
          </details>
        ) : null}
        {workspace.data?.sections.quality.status === "UNKNOWN" ? (
          <p className="formal-problem">分钟数据质量投影不可用；新鲜度、连续性、重复与时区对齐均保持未知。</p>
        ) : workspace.data?.latest_quality_receipt ? (
          <details className="formal-disclosure">
            <summary>查看分钟数据质量证据</summary>
            <dl className="formal-summary-list">
              <div><dt>结论</dt><dd><StatusBadge status={workspace.data.latest_quality_receipt.status} /></dd></div>
              <div><dt>市场 / 周期</dt><dd>{workspace.data.latest_quality_receipt.exchange} · {workspace.data.latest_quality_receipt.pair} · {workspace.data.latest_quality_receipt.timeframe}</dd></div>
              <div><dt>检查时间</dt><dd>{displayDateTime(workspace.data.latest_quality_receipt.inspected_at)}</dd></div>
              <div><dt>覆盖窗口</dt><dd>{displayDateTime(workspace.data.latest_quality_receipt.first_open_at)} → {displayDateTime(workspace.data.latest_quality_receipt.last_open_at)}</dd></div>
              <div><dt>行数 / 间隔</dt><dd>{workspace.data.latest_quality_receipt.row_count} 行 · {workspace.data.latest_quality_receipt.expected_interval_seconds} 秒</dd></div>
              <div><dt>数据新鲜度</dt><dd>{workspace.data.latest_quality_receipt.freshness_seconds === null ? "未知" : `${Math.round(workspace.data.latest_quality_receipt.freshness_seconds / 60)} 分钟`}</dd></div>
              <div><dt>连续性</dt><dd>缺口 {workspace.data.latest_quality_receipt.missing_interval_count} · 错位 {workspace.data.latest_quality_receipt.misaligned_timestamp_count} · 乱序 {workspace.data.latest_quality_receipt.out_of_order_count}</dd></div>
              <div><dt>重复 / OHLCV</dt><dd>重复 {workspace.data.latest_quality_receipt.duplicate_timestamp_count} · 空值 {workspace.data.latest_quality_receipt.null_ohlcv_count} · 无效 OHLC {workspace.data.latest_quality_receipt.invalid_ohlc_count} · 负成交量 {workspace.data.latest_quality_receipt.negative_volume_count}</dd></div>
              <div><dt>原因代码</dt><dd>{workspace.data.latest_quality_receipt.reason_codes.length ? workspace.data.latest_quality_receipt.reason_codes.join("、") : "无阻断原因"}</dd></div>
            </dl>
          </details>
        ) : workspace.data?.sections.quality.status === "AVAILABLE" ? (
          <p className="formal-muted">尚无分钟数据质量 receipt；这不等于数据已通过检查。</p>
        ) : null}
      </section>

      <FallbackNotice
        context="策略目录、版本、状态和排行榜。"
        error={error}
        isLoading={isLoading}
        source={source}
      />

      <section className="formal-panel" aria-labelledby="research-progress-title">
        <div className="formal-section-heading">
          <div><span className="formal-kicker">最新批次</span><h2 id="research-progress-title">生成、验证与入库</h2></div>
          <span className="formal-section-note">{latestResearch ? displayDateTime(latestResearch.completed_at ?? latestResearch.created_at) : "尚无批次"}</span>
        </div>
        {researchLoading ? (
          <FormalLoadingState className="formal-lifecycle" label="正在读取研究批次" />
        ) : researchError ? (
          <><EmptyState title="研究批次状态未知" description="候选批次 API 读取失败；这不表示没有生成，也不表示候选已被拒绝。" /><button className="secondary-button" onClick={() => { setResearchLoading(true); setResearchError(null); setResearchRevision((value) => value + 1); refreshReadModels(); }} type="button">重新读取研究证据</button></>
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
              <span>{lifecycleSummaryText(latestLifecycleSummary, workspace.data?.sections.bridge?.status === "AVAILABLE")}</span>
              <StatusBadge status={latestResearch.status} />
            </div>
            {latestResearch.failure_reason ? <p className="formal-problem">失败原因：{latestResearch.failure_reason}</p> : null}
          </>
        ) : (
          <EmptyState title="尚无持久化研究批次" description="数据库中没有正式批次，表示尚未完成生成与入库；不能解释为已有候选已验证不合格。" />
        )}
      </section>

      <section className="formal-panel" id="research-candidates" aria-labelledby="research-candidates-title">
        <div className="formal-section-heading">
          <div><span className="formal-kicker">正式候选</span><h2 id="research-candidates-title">合格、拒绝与验证失败</h2></div>
          <span className="formal-section-note">QUALIFIED 不等于已部署</span>
        </div>
        <div className="formal-bridge-path" aria-label="候选到模拟盘的四步正式生命周期">
          {["候选质量门", "Blueprint v2 bridge", "canonical 验证与批准", "OKX_DEMO ACTIVE"].map((label, index) => (
            <div key={label}><span>{index + 1}</span><strong>{label}</strong></div>
          ))}
        </div>
        {workspace.loading ? (
          <p className="formal-muted">正在读取 candidate → canonical 生命周期证据…</p>
        ) : workspace.error || workspace.data?.sections.bridge?.status !== "AVAILABLE" ? (
          <p className="formal-problem">生命周期未知：权威 bridge 投影不可用；不会从 QUALIFIED、策略名称或摘要推断。</p>
        ) : null}
        {researchLoading ? (
          <FormalLoadingState label="正在读取候选" />
        ) : researchError ? (
          <EmptyState title="候选状态未知" description="候选 API 读取失败；未知不能显示为没有候选。" />
        ) : latestResearch?.candidates.length ? (
          <ul className="formal-candidate-list">
            {latestResearch.candidates.map((candidate) => {
              const primaryReason = candidate.rejection_reasons[0];
              const qualified = candidate.status === "QUALIFIED"
                && candidate.validation_passed
                && candidate.deployable_candidate;
              const lifecycle = candidateLifecycleFor(workspace.data, candidate.id);
              const lifecycleDisplay = candidateLifecycleDisplay(lifecycle?.lifecycle_status);
              return (
                <li className="formal-candidate-card" key={candidate.id}>
                  <header><h3>{candidate.candidate_name}</h3><div><StatusBadge status={candidate.status} /> <StatusBadge label={lifecycleDisplay.label} status={lifecycleDisplay.status} /></div></header>
                  <p>Score {candidate.score ?? "暂无"} · Lookahead {candidate.lookahead_status} · Static {candidate.static_check}</p>
                  <p className="formal-candidate-reason">
                    {primaryReason
                      ? `${primaryReason.code}：${primaryReason.message}`
                      : qualified
                        ? "研究质量门已通过；是否进入部署评审以权威交接状态为准。"
                        : "拒绝或验证证据缺失，不能视为质量门已通过。"}
                  </p>
                  <p className={lifecycleDisplay.status === "UNKNOWN" ? "formal-problem" : "formal-muted"}>
                    正式生命周期：{lifecycleDisplay.detail}
                  </p>
                  <details className="formal-disclosure">
                    <summary>查看完整拒绝原因与技术证据</summary>
                    {candidate.rejection_reasons.length ? (
                      <ol>{candidate.rejection_reasons.map((reason, index) => <li key={`${reason.code}-${index}`}>{reason.code}：{reason.message}</li>)}</ol>
                    ) : <p className="formal-muted">没有拒绝原因记录。</p>}
                    <CopyableValue label="候选来源路径" value={candidate.source_path} />
                    <CopyableValue label="候选摘要" value={candidate.code_digest} />
                    <CopyableValue label="Bridge 原因码" value={lifecycle?.reason_code} />
                    <CopyableValue label="Blueprint 摘要" value={lifecycle?.blueprint_digest} />
                    <CopyableValue label="Canonical Strategy ID" value={lifecycle?.canonical_strategy_id} />
                    <CopyableValue label="Canonical Version ID" value={lifecycle?.canonical_strategy_version_id} />
                    <ExpandableText summary="完整结构化证据" value={JSON.stringify(candidate.evidence_snapshot, null, 2)} />
                  </details>
                </li>
              );
            })}
          </ul>
        ) : (
          <EmptyState title="当前没有候选行" description="无候选可能表示尚未生成或在生成前失败；请结合最新批次状态判断。" />
        )}
        {!researchLoading && !researchError && researchBatches.length ? (
          <details className="formal-disclosure">
            <summary>查看历史批次与报告证据</summary>
            {researchBatches.map((batch) => (
              <article className="strategy-history-batch" key={batch.id}>
                <div><strong>{batch.run_id}</strong><StatusBadge status={batch.status} /></div>
                <p>请求 {batch.requested_count} · 生成 {batch.generated_count} · 入库 {batch.persisted_count} · 合格 {batch.qualified_count}</p>
                <CopyableValue label="研究 Run ID" value={batch.run_id} />
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
        {catalog.strategies.loading ? <FormalLoadingState label="正在读取正式策略库" /> : catalog.strategies.error ? (
          <><EmptyState title="正式策略库状态未知" description="策略目录或版本 API 读取失败；未知不能显示为暂无策略。" /><button className="secondary-button" onClick={catalog.refresh} type="button">重新读取策略库</button></>
        ) : source !== "api" ? (
          <EmptyState title="正式策略库来源不可验收" description="fixture 或未知来源不计入正式策略数字。" />
        ) : null}
        {!catalog.strategies.loading && !catalog.strategies.error && data.strategies.length ? (
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
        {!catalog.strategies.loading && !catalog.strategies.error && data.strategies.length === 0 ? <EmptyState title="暂无真实策略" description="当前没有可展示的真实核心策略记录；空结果不代表策略生成成功。" /> : null}
      </section>

      <section className="formal-panel" id="strategy-ranking" aria-labelledby="strategy-ranking-title">
        <div className="formal-section-heading">
          <div><span className="formal-kicker">排行榜</span><h2 id="strategy-ranking-title">真实评分策略</h2></div>
          <Link className="formal-text-link" to="/ranking">查看完整证据</Link>
        </div>
        {catalog.ranking.loading ? <FormalLoadingState label="正在读取排行榜" /> : catalog.ranking.error ? (
          <><EmptyState title="排行榜状态未知" description="排名 API 读取失败；未知不能显示为空榜。" /><button className="secondary-button" onClick={catalog.refresh} type="button">重新读取排行榜</button></>
        ) : source !== "api" ? (
          <EmptyState title="排行榜来源不可验收" description="fixture 或未知来源不计入正式排名。" />
        ) : data.ranking.length ? (
          <ol className="formal-ranking-list">
            {data.ranking.slice(0, 5).map((entry) => (
              <li key={entry.scoreId}>
                <span>#{entry.rank}</span>
                <strong>{entry.strategyName} · v{entry.versionNumber}</strong>
                <span>{entry.totalScore === null ? "评分未知" : `${entry.totalScore.toFixed(1)} 分`}</span>
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
