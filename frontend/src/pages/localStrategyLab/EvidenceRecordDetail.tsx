import type { EvidenceListRecord } from "./evidenceBrowserModel";
import { CopyableValue, ExpandableText, StatusBadge } from "../../components/DisplayPrimitives";
import { EMPTY_TEXT, displayBoolean } from "../uiCopy";

function CopyEntries({
  entries,
  label,
}: {
  entries: Record<string, number | string>;
  label: string;
}) {
  const rows = Object.entries(entries);
  return rows.length ? (
    <div className="lab-evidence-detail__entries">
      {rows.map(([key, value]) => (
        <span className="lab-evidence-detail__entry" key={key}>
          <code>{key}</code>
          <CopyableValue label={`${label} ${key}`} value={String(value)} />
        </span>
      ))}
    </div>
  ) : <span>{EMPTY_TEXT}</span>;
}

export function EvidenceRecordDetail({ record }: { record: EvidenceListRecord }) {
  const source = record.source;
  return (
    <article className="lab-evidence-detail" aria-label="证据记录详情" data-testid="lab-evidence-detail">
      <header>
        <div>
          <span>记录详情</span>
          <h3>{record.title}</h3>
          <p>{record.subtitle}</p>
        </div>
        <StatusBadge showRaw status={record.status} />
      </header>
      <dl>
        <div>
          <dt>record id</dt>
          <dd><CopyableValue label="记录 ID" value={record.id} /></dd>
        </div>
        <div>
          <dt>database IDs</dt>
          <dd><CopyEntries entries={record.databaseIds} label="database ID" /></dd>
        </div>
        <div>
          <dt>artifact refs</dt>
          <dd><CopyEntries entries={record.artifactRefs} label="artifact ref" /></dd>
        </div>
      </dl>
      <details className="lab-evidence-detail__technical">
        <summary>查看完整来源与环境证据</summary>
        <dl>
          <div><dt>source_type</dt><dd><CopyableValue label="source_type" value={source?.sourceType ?? "unknown"} /></dd></div>
          <div><dt>core_data</dt><dd>{displayBoolean(source?.coreData)}</dd></div>
          <div><dt>environment</dt><dd>{source?.environment.scope ?? "unknown"} / runnable={displayBoolean(source?.environment.runnable)}</dd></div>
          <div><dt>migration</dt><dd>{displayBoolean(source?.environment.migrationVerified)} · <ExpandableText value={source?.environment.reason ?? EMPTY_TEXT} /></dd></div>
          <div><dt>source detail</dt><dd><ExpandableText value={source?.sourceDetail ?? EMPTY_TEXT} /></dd></div>
          <div><dt>blocked reason</dt><dd><ExpandableText value={source?.blockedReason ?? EMPTY_TEXT} /></dd></div>
          <div><dt>error</dt><dd><ExpandableText value={record.error ?? EMPTY_TEXT} /></dd></div>
        </dl>
      </details>
    </article>
  );
}
