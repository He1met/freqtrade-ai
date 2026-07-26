import { useMemo } from "react";

import type { MvpData } from "../../api/types";
import { EmptyState, StatusBadge } from "../../components/DisplayPrimitives";
import type { LabSelection } from "./candidateWorkbenchModel";
import {
  buildEvidenceRecords,
  EVIDENCE_TABS,
  EVIDENCE_TAB_LABELS,
  evidenceBrowserEmptyState,
  partitionBrowserRecords,
  type EvidenceListRecord,
} from "./evidenceBrowserModel";
import { EvidenceRecordDetail } from "./EvidenceRecordDetail";
import { useEvidenceLocation } from "./useEvidenceLocation";
import "../../styles/local-strategy-lab-evidence-browser.css";

type SelectLabEntity = (key: keyof LabSelection, value: string | null) => void;

function syncCurrentRecord(record: EvidenceListRecord, select: SelectLabEntity) {
  if (record.related.strategyVersionId) select("strategyVersionId", record.related.strategyVersionId);
  if (record.related.backtestRunId) select("backtestRunId", record.related.backtestRunId);
  if (record.related.backtestTaskId) select("backtestTaskId", record.related.backtestTaskId);
  if (record.related.backtestResultId) select("backtestResultId", record.related.backtestResultId);
  if (record.related.scoreId) select("scoreId", record.related.scoreId);
}

export function EvidenceBrowser({
  data,
  select,
}: {
  data: MvpData;
  select: SelectLabEntity;
}) {
  const { location, setLocation } = useEvidenceLocation();
  const allRecords = useMemo(() => buildEvidenceRecords(data, location.tab), [data, location.tab]);
  const partitions = useMemo(() => partitionBrowserRecords(allRecords), [allRecords]);
  const records = partitions[location.scope];
  const selected = location.recordId ? records.find((record) => record.id === location.recordId) : undefined;
  const staleSelection = Boolean(location.recordId && !selected);
  const empty = evidenceBrowserEmptyState(data.localStrategyLabEvidence, location.tab, location.scope, allRecords);

  function changeTab(tab: typeof location.tab) {
    setLocation({ tab, scope: location.scope, recordId: null });
  }

  function changeScope(scope: typeof location.scope) {
    setLocation({ tab: location.tab, scope, recordId: null });
  }

  function choose(record: EvidenceListRecord) {
    setLocation({ ...location, recordId: record.id });
    if (location.scope === "current") syncCurrentRecord(record, select);
  }

  return (
    <section className="lab-evidence-browser" aria-label="持久证据浏览器" data-testid="lab-evidence-browser">
      <header className="lab-evidence-browser__header">
        <div>
          <span className="lab-evidence-browser__eyebrow">持久证据浏览</span>
          <h2>先比较决策数据，再按需查看完整审计字段</h2>
        </div>
        <div className="lab-evidence-browser__scope" aria-label="证据范围">
          <button aria-pressed={location.scope === "current"} onClick={() => changeScope("current")} type="button">
            当前核心 <strong>{partitions.current.length}</strong>
          </button>
          <button aria-pressed={location.scope === "diagnostic"} onClick={() => changeScope("diagnostic")} type="button">
            诊断 <strong>{partitions.diagnostic.length}</strong>
          </button>
        </div>
      </header>
      <div className="lab-evidence-browser__tabs" role="tablist" aria-label="证据类型">
        {EVIDENCE_TABS.map((tab) => (
          <button
            aria-selected={location.tab === tab}
            id={`lab-tab-${tab}`}
            key={tab}
            onClick={() => changeTab(tab)}
            role="tab"
            type="button"
          >
            {EVIDENCE_TAB_LABELS[tab]}
          </button>
        ))}
      </div>
      {location.scope === "diagnostic" ? (
        <p className="lab-evidence-browser__diagnostic-note">
          historical、fixture 和来源不完整记录仅供只读排查；选择它们不会更新候选工作台。
        </p>
      ) : null}
      <div className="lab-evidence-browser__workspace">
        <div className="lab-evidence-browser__list" role="tabpanel" aria-labelledby={`lab-tab-${location.tab}`}>
          {records.map((record) => (
            <button
              aria-pressed={selected?.id === record.id}
              className="lab-evidence-card"
              key={record.id}
              onClick={() => choose(record)}
              type="button"
            >
              <span className="lab-evidence-card__heading">
                <span><strong>{record.title}</strong><small>{record.subtitle}</small></span>
                <StatusBadge showRaw status={record.status} />
              </span>
              <span className="lab-evidence-card__metrics">
                {record.decisionFields.map((field) => (
                  <span key={field.label}><small>{field.label}</small><strong>{field.value}</strong></span>
                ))}
              </span>
              <span className="lab-evidence-card__id">ID {record.id}</span>
            </button>
          ))}
          {!records.length ? (
            <EmptyState description={empty.detail} title={`${empty.title}（${empty.state}）`} />
          ) : null}
        </div>
        <div className="lab-evidence-browser__detail">
          {selected ? <EvidenceRecordDetail record={selected} /> : staleSelection ? (
            <EmptyState
              description={`URL 中的 lab_record=${location.recordId} 不存在于当前标签和范围；为避免误判，未自动改选其他记录。`}
              title="记录已过期或被过滤"
            />
          ) : (
            <EmptyState description="从左侧选择一条记录；完整 IDs、artifact、来源、环境和错误会显示在这里。" title="尚未选择记录" />
          )}
        </div>
      </div>
    </section>
  );
}
