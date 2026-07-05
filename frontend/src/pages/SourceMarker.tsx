import type { DataSourceTraceSummary } from "../api/types";
import { EMPTY_TEXT, displayBoolean } from "./uiCopy";

function formatRecord(record: Record<string, number | string>): string {
  const entries = Object.entries(record);
  return entries.length > 0 ? entries.map(([key, value]) => `${key}: ${value}`).join(", ") : EMPTY_TEXT;
}

function markerClass(source: DataSourceTraceSummary | undefined): string {
  const sourceType = source?.sourceType ?? "unknown";
  if (source?.coreData && (sourceType === "database" || sourceType === "api_aggregate")) {
    return "source-marker source-marker-core";
  }
  if (sourceType === "fixture" || sourceType === "fallback") {
    return "source-marker source-marker-non-core";
  }
  return "source-marker source-marker-unknown";
}

export function isCoreDataSource(source: DataSourceTraceSummary | undefined): boolean {
  return Boolean(
    source?.coreData &&
      (source.sourceType === "database" || source.sourceType === "api_aggregate") &&
      Object.keys(source.databaseIds).length > 0,
  );
}

export function SourceMarker({
  label,
  source,
}: {
  label?: string;
  source: DataSourceTraceSummary | undefined;
}) {
  const sourceType = source?.sourceType ?? "unknown";
  const sourceDetail = source?.sourceDetail ?? "Source metadata was not provided.";

  return (
    <div className={markerClass(source)} data-core-source={source?.coreData === true ? "true" : "false"}>
      <div className="source-marker-heading">
        {label ? <span>{label}</span> : null}
        <strong>{sourceType}</strong>
        <em>{source?.coreData ? "core" : "non-core"}</em>
      </div>
      <dl>
        <div>
          <dt>core_data</dt>
          <dd>{displayBoolean(source?.coreData === true)}</dd>
        </div>
        <div>
          <dt>database_ids</dt>
          <dd>{source ? formatRecord(source.databaseIds) : EMPTY_TEXT}</dd>
        </div>
        <div>
          <dt>artifact_refs</dt>
          <dd>{source ? formatRecord(source.artifactRefs) : EMPTY_TEXT}</dd>
        </div>
        <div>
          <dt>detail</dt>
          <dd>{sourceDetail}</dd>
        </div>
        {source?.blockedReason ? (
          <div>
            <dt>blocked</dt>
            <dd>{source.blockedReason}</dd>
          </div>
        ) : null}
      </dl>
    </div>
  );
}
