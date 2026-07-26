import { CopyableValue, ExpandableText, StatusBadge } from "../../components/DisplayPrimitives";
import "../../styles/local-strategy-lab-action-timeline.css";
import { resolveLatestActionFeedback } from "./actionEvidence";
import type {
  ActionEvidence,
  ActionEvidenceEnvironmentScope,
  ActionEvidenceHistoryState,
  ActionEvidencePhase,
} from "./actionEvidence";

const phaseLabels: Record<ActionEvidencePhase, string> = {
  generation: "策略生成",
  backtest: "回测验证",
  score: "评分选择",
  "dry-run": "受控 Dry-run",
  system: "页面 / 数据刷新",
};

function timestamp(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? value
    : new Intl.DateTimeFormat("zh-CN", {
        dateStyle: "short",
        timeStyle: "medium",
      }).format(parsed);
}

function entitySummary(entry: ActionEvidence): string {
  const ids = Object.entries(entry.entityIds);
  return ids.length > 0
    ? ids.map(([key, value]) => `${key}=${value}`).join(" · ")
    : "未返回可关联的 database ID";
}

function historyStateText(state: ActionEvidenceHistoryState, empty: boolean): string {
  if (state === "migrated-v1") return "已从本浏览器 v1 辅助历史迁移；API/DB 证据仍是唯一业务依据。";
  if (state === "restored-v2") return "已恢复本浏览器辅助历史；它不会推进或完成任何业务阶段。";
  if (state === "invalid") return "本浏览器历史损坏或格式不完整，已忽略；请直接核对 API/DB。";
  if (state === "unavailable") return "浏览器存储不可用；本地反馈无法跨刷新保留，但不影响 API/DB 持久记录。";
  return empty
    ? "本浏览器辅助历史为空或已丢失；这不等于后端没有持久记录。"
    : "本浏览器辅助历史不会推进或完成任何业务阶段。";
}

function TimelineEntry({ entry }: { entry: ActionEvidence }) {
  return (
    <li className="lab-action-timeline__entry" data-status={entry.status}>
      <div className="lab-action-timeline__entry-main">
        <span className="lab-action-timeline__dot" aria-hidden="true" />
        <div>
          <div className="lab-action-timeline__entry-title">
            <strong>{entry.action}</strong>
            {entry.repeatCount > 1 ? <span>重复 {entry.repeatCount} 次</span> : null}
          </div>
          <small>{phaseLabels[entry.phase]} · {timestamp(entry.updatedAt)}</small>
          <p>{entitySummary(entry)}</p>
        </div>
        <StatusBadge label={entry.status} showRaw status={entry.status} />
      </div>
      <details>
        <summary>查看完整原因、artifact 与下一步</summary>
        <dl>
          <div>
            <dt>原因 / 结果</dt>
            <dd><ExpandableText value={entry.message} /></dd>
          </div>
          <div>
            <dt>下一步</dt>
            <dd><ExpandableText value={entry.nextAction} /></dd>
          </div>
          <div>
            <dt>关联 database IDs</dt>
            <dd className="lab-action-timeline__values">
              {Object.entries(entry.entityIds).length > 0
                ? Object.entries(entry.entityIds).map(([key, value]) => (
                    <CopyableValue key={key} label={key} value={`${key}: ${value}`} />
                  ))
                : "无；该反馈不能证明业务成功。"}
            </dd>
          </div>
          <div>
            <dt>artifact paths</dt>
            <dd className="lab-action-timeline__values">
              {entry.artifactPaths.length > 0
                ? entry.artifactPaths.map((path) => (
                    <CopyableValue key={path} label="artifact path" value={path} />
                  ))
                : "无"}
            </dd>
          </div>
          <div>
            <dt>Bug 建议</dt>
            <dd>{entry.recommendBug ? "建议创建 Bug Issue" : "否"}</dd>
          </div>
        </dl>
      </details>
    </li>
  );
}

export function ActionTimeline({
  history,
  historyState,
}: {
  history: ActionEvidence[];
  historyState: ActionEvidenceHistoryState;
}) {
  return (
    <section className="lab-action-timeline" aria-label="本浏览器操作反馈">
      <p className="lab-action-timeline__notice" role="note">
        {historyStateText(historyState, history.length === 0)}
      </p>

      <details className="lab-action-timeline__history">
        <summary>操作历史（{history.length}）</summary>
        {history.length > 0 ? (
          <ol>
            {history.map((entry) => <TimelineEntry entry={entry} key={entry.eventId} />)}
          </ol>
        ) : (
          <p>没有可恢复的本浏览器操作反馈。请勿据此判断后端没有 run、task、result 或 score。</p>
        )}
      </details>
    </section>
  );
}

export function LatestActionFeedback({
  actions,
  environmentScope,
  expectedEntityIds,
  history,
  phase,
}: {
  actions?: string[];
  environmentScope: ActionEvidenceEnvironmentScope;
  expectedEntityIds?: Record<string, number | string | null | undefined>;
  history: ActionEvidence[];
  phase: ActionEvidencePhase;
}) {
  const { applicability, entry: latest } = resolveLatestActionFeedback({
    actions,
    environmentScope,
    expectedEntityIds,
    history,
    phase,
  });
  const isCurrent = applicability === "current";
  const contextLabel =
    applicability === "historical"
      ? "历史辅助反馈"
      : applicability === "mismatch"
        ? "不适用于当前对象"
        : applicability === "unknown"
          ? "环境未确认"
          : applicability === "empty"
            ? "尚无本浏览器操作"
            : latest?.action ?? "最近操作反馈";
  const contextHelp =
    applicability === "historical"
      ? "当前查看的是历史环境；此反馈不能作为当前环境成功。"
      : applicability === "mismatch"
        ? "反馈关联 ID 与当前候选或任务不一致；请切换对象或重新执行。"
        : applicability === "unknown"
          ? "无法确认当前环境归属；请先核对 API/DB 环境证据。"
          : latest?.nextAction ?? "执行上方操作后显示辅助反馈；业务状态仍以 API/DB 为准。";

  return (
    <section
      aria-label={`${phaseLabels[phase]}最近操作反馈`}
      className="lab-latest-action-feedback"
      data-phase={phase}
    >
      <div>
        <span>最近操作反馈</span>
        <strong>{contextLabel}</strong>
        <small>{latest ? timestamp(latest.updatedAt) : phaseLabels[phase]}</small>
      </div>
      {latest
        ? isCurrent
          ? <StatusBadge label={latest.status} showRaw status={latest.status} />
          : <StatusBadge label={contextLabel} status="NOT_CURRENT" />
        : null}
      <div>
        <span>关联对象</span>
        <strong>{latest ? entitySummary(latest) : "无"}</strong>
      </div>
      <div>
        <span>下一步</span>
        <strong>{contextHelp}</strong>
      </div>
    </section>
  );
}
