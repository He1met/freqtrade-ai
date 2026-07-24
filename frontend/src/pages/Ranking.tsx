import { combineDataSources } from "../api/sourceState";
import { useMvpData } from "../api/useMvpData";
import type { DataSourceTraceSummary, RankingEntry } from "../api/types";
import {
  CompactText,
  CopyableValue,
  EmptyState,
  ExpandableText,
  PageHeader,
  StatusBadge,
} from "../components/DisplayPrimitives";
import "../styles/ranking.css";
import { FallbackNotice } from "./FallbackNotice";
import {
  buildRankingViewModel,
  formatAuditRecord,
  rankingConclusion,
  rankingScoreLabel,
  scoreEvidenceStage,
} from "./rankingDisplay";
import { EMPTY_TEXT, displayLoadState, displayNumber } from "./uiCopy";

function RankingAuditDetails({ entry }: { entry: RankingEntry }) {
  const source: DataSourceTraceSummary = entry.dataSource;

  return (
    <details className="ranking-audit">
      <summary>查看 ID、路径和来源</summary>
      <dl>
        <div>
          <dt>评分 ID</dt>
          <dd><CopyableValue label="评分 ID" value={entry.scoreId} /></dd>
        </div>
        <div>
          <dt>策略 / 版本 ID</dt>
          <dd>
            <CopyableValue
              label="策略和版本 ID"
              value={`${entry.strategyId} / ${entry.strategyVersionId}`}
            />
          </dd>
        </div>
        <div>
          <dt>回测结果 ID</dt>
          <dd><CopyableValue label="回测结果 ID" value={entry.backtestResultId ?? EMPTY_TEXT} /></dd>
        </div>
        <div>
          <dt>策略文件</dt>
          <dd><CopyableValue label="策略文件路径" value={entry.filePath} /></dd>
        </div>
        <div>
          <dt>source_type / core_data</dt>
          <dd>
            <CopyableValue
              label="来源分类"
              value={`${source.sourceType} / core_data=${String(source.coreData)}`}
            />
          </dd>
        </div>
        <div>
          <dt>database_ids</dt>
          <dd><CopyableValue label="database_ids" value={formatAuditRecord(source.databaseIds)} /></dd>
        </div>
        <div>
          <dt>artifact_refs</dt>
          <dd><CopyableValue label="artifact_refs" value={formatAuditRecord(source.artifactRefs)} /></dd>
        </div>
        <div>
          <dt>来源说明</dt>
          <dd><ExpandableText value={source.sourceDetail} /></dd>
        </div>
      </dl>
    </details>
  );
}

export function Ranking() {
  const { data, sources, isLoading, error } = useMvpData();
  const source = combineDataSources(sources, ["ranking"]);
  const scoreStage = scoreEvidenceStage(data.localStrategyLabEvidence?.stages);
  const view = buildRankingViewModel({
    entries: data.ranking,
    error,
    scoreStage,
    source,
  });

  return (
    <section className="page ranking-page">
      <PageHeader
        description="仅展示具有真实 BacktestResult 和 StrategyScore 持久证据的策略排名。"
        status={
          <StatusBadge
            label={isLoading ? displayLoadState(true, source) : view.label}
            status={isLoading ? "pending" : view.kind}
            tone={isLoading ? "info" : view.tone}
          />
        }
        title="策略排行榜"
      />
      <FallbackNotice
        context="策略排行榜、评分拆解、淘汰原因和策略文件路径。"
        error={error}
        isLoading={isLoading}
        source={source}
      />

      {!isLoading ? (
        <section className="ranking-view-state" data-state={view.kind} aria-label="排行榜结论">
          <StatusBadge label={view.label} status={view.kind} tone={view.tone} />
          <span>{view.summary}</span>
          {view.nextAction ? <ExpandableText summary="查看下一步" value={view.nextAction} /> : null}
        </section>
      ) : null}

      {view.entries.length > 0 ? (
        <div className="table-shell ranking-primary-table">
          <table>
            <colgroup>
              <col className="ranking-primary-col-rank" />
              <col className="ranking-primary-col-strategy" />
              <col className="ranking-primary-col-score" />
              <col className="ranking-primary-col-breakdown" />
              <col className="ranking-primary-col-conclusion" />
            </colgroup>
            <thead>
              <tr>
                <th>排名</th>
                <th>策略</th>
                <th>总分</th>
                <th>关键分项</th>
                <th>结论</th>
              </tr>
            </thead>
            <tbody>
              {view.entries.map((entry) => {
                const conclusion = rankingConclusion(entry);
                return (
                  <tr key={`${entry.scoreId}-${entry.strategyVersionId}`}>
                    <td>
                      <strong className="ranking-position">#{entry.rank}</strong>
                    </td>
                    <td>
                      <div className="ranking-strategy">
                        <strong><CompactText label="策略名称" value={entry.strategyName} /></strong>
                        <span>
                          版本 {entry.versionNumber}
                          {entry.scoringVersion ? ` · ${entry.scoringVersion}` : ""}
                        </span>
                        <RankingAuditDetails entry={entry} />
                      </div>
                    </td>
                    <td>
                      <div className="ranking-total-score">
                        <strong>{displayNumber(entry.totalScore, { minimumFractionDigits: 1, maximumFractionDigits: 1 })}</strong>
                        {entry.rawTotalScore !== null && entry.rawTotalScore !== entry.totalScore ? (
                          <span>原始分 {displayNumber(entry.rawTotalScore, { maximumFractionDigits: 1 })}</span>
                        ) : null}
                      </div>
                    </td>
                    <td>
                      <div className="ranking-score-grid">
                        {entry.scoreBreakdown.slice(0, 4).map((item) => (
                          <div key={item.name}>
                            <span>{rankingScoreLabel(item)}</span>
                            <strong>{displayNumber(item.score, { maximumFractionDigits: 1 })}</strong>
                            <em>贡献 {displayNumber(item.contribution, { maximumFractionDigits: 1 })}</em>
                          </div>
                        ))}
                      </div>
                      <ExpandableText
                        className="ranking-score-details"
                        summary="查看权重与完整分项"
                        value={entry.scoreBreakdown
                          .map(
                            (item) =>
                              `${rankingScoreLabel(item)}：得分 ${displayNumber(item.score, { maximumFractionDigits: 2 })}，权重 ${displayNumber(item.weight * 100, { maximumFractionDigits: 1 })}%，贡献 ${displayNumber(item.contribution, { maximumFractionDigits: 2 })}`,
                          )
                          .join("\n")}
                      />
                    </td>
                    <td>
                      <div className="ranking-conclusion">
                        <StatusBadge
                          label={conclusion.label}
                          status={entry.elimination.eliminated ? "failed" : conclusion.tone}
                          tone={conclusion.tone}
                        />
                        <CompactText label="评分结论" value={conclusion.summary} />
                        {conclusion.details ? (
                          <ExpandableText summary="查看完整原因" value={conclusion.details} />
                        ) : null}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : !isLoading ? (
        <EmptyState
          description={`${view.summary}${view.nextAction ? ` ${view.nextAction}` : ""}`}
          title={
            view.kind === "filtered"
              ? "没有可验收的评分记录"
              : view.kind === "failed"
                ? "排行榜加载失败"
                : view.kind === "blocked"
                  ? "评分链路不可验收"
                  : "暂无真实评分记录"
          }
        />
      ) : null}
    </section>
  );
}
