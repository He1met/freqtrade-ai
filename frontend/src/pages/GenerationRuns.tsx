import { combineDataSources } from "../api/sourceState";
import {
  CompactText,
  CopyableValue,
  EmptyState,
  ExpandableText,
  PageHeader,
  StatusBadge,
} from "../components/DisplayPrimitives";
import { useMvpData } from "../api/useMvpData";
import "../styles/generation-runs.css";
import { FallbackNotice } from "./FallbackNotice";
import {
  generationRunDisplayTime,
  generationRunOutcome,
  generationRunTimeLabel,
} from "./generationRunDisplay";
import { displayDateTime, displayLoadState } from "./uiCopy";

export function GenerationRuns() {
  const { data, sources, isLoading, error } = useMvpData();
  const source = combineDataSources(sources, ["generationRuns"]);
  const sourceTone = source === "api" ? "success" : source === "fixture" ? "warning" : "danger";

  return (
    <section className="page generation-runs-page">
      <PageHeader
        description="查看 Provider、Model、生成数量和失败结论；状态完成不等于一定存在有效产出。"
        status={
          <StatusBadge
            label={displayLoadState(isLoading, source)}
            status={isLoading ? "pending" : source}
            tone={isLoading ? "info" : sourceTone}
          />
        }
        title="生成批次"
      />
      <FallbackNotice
        context="AI 生成批次、Provider/Model、请求数量和错误摘要。"
        error={error}
        isLoading={isLoading}
        source={source}
      />
      {data.generationRuns.length > 0 ? (
        <div className="table-shell generation-runs-table-shell">
          <table>
            <colgroup>
              <col className="generation-runs-col-status" />
              <col className="generation-runs-col-model" />
              <col className="generation-runs-col-output" />
              <col className="generation-runs-col-time" />
              <col className="generation-runs-col-detail" />
            </colgroup>
            <thead>
              <tr>
                <th>状态与结论</th>
                <th>Provider / Model</th>
                <th>产出数量</th>
                <th>时间</th>
                <th>批次详情</th>
              </tr>
            </thead>
            <tbody>
              {data.generationRuns.map((run) => {
                const outcome = generationRunOutcome(run);
                const displayTime = generationRunDisplayTime(run);

                return (
                  <tr key={run.id}>
                    <td className="generation-runs-status-cell">
                      <StatusBadge
                        label={outcome.label}
                        showRaw
                        status={run.status}
                        tone={outcome.tone}
                      />
                      <CompactText
                        className="generation-runs-conclusion"
                        label="生成结论"
                        value={outcome.conclusion}
                      />
                    </td>
                    <td>
                      <div className="generation-runs-model">
                        <strong>
                          <CompactText label="Provider" value={run.provider} />
                        </strong>
                        <span>
                          <CompactText label="Model" value={run.model} />
                        </span>
                      </div>
                    </td>
                    <td>
                      <dl className="generation-runs-counts" aria-label="生成数量">
                        <div>
                          <dt>请求</dt>
                          <dd>{run.requestedCount}</dd>
                        </div>
                        <div>
                          <dt>生成</dt>
                          <dd>{run.generatedCount}</dd>
                        </div>
                        <div>
                          <dt>接受</dt>
                          <dd>{run.acceptedCount}</dd>
                        </div>
                        <div data-count-alert={run.failedCount > 0 ? "true" : undefined}>
                          <dt>失败</dt>
                          <dd>{run.failedCount}</dd>
                        </div>
                      </dl>
                    </td>
                    <td>
                      <div className="generation-runs-time">
                        <span>{generationRunTimeLabel(run)}</span>
                        <strong>{displayDateTime(displayTime)}</strong>
                      </div>
                    </td>
                    <td>
                      <div className="generation-runs-detail">
                        <CopyableValue label="批次 ID" value={run.id} />
                        {run.errorMessage ? (
                          <ExpandableText
                            className="generation-runs-error"
                            summary="查看完整错误"
                            value={run.errorMessage}
                          />
                        ) : (
                          <span className="generation-runs-no-error">未记录错误</span>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <EmptyState
          description={
            source === "api"
              ? "Backend API 已连接，但当前没有可显示的真实生成批次；这不代表生成流程成功。"
              : "当前没有可显示的生成批次。"
          }
          title={source === "api" ? "暂无真实生成批次" : "暂无生成批次"}
        />
      )}
    </section>
  );
}
