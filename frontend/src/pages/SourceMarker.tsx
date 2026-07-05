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
  if (sourceType === "fixture" || sourceType === "fallback" || sourceType === "mock") {
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

function requiredAction(source: DataSourceTraceSummary | undefined): string {
  if (isCoreDataSource(source)) {
    return "可用于核心验收；刷新后仍需保持相同 database_ids。";
  }
  if (source?.blockedReason) {
    return `解除 BLOCKED：${source.blockedReason}`;
  }

  const sourceType = source?.sourceType ?? "unknown";
  if (sourceType === "fixture" || sourceType === "fallback" || sourceType === "mock") {
    return "运行真实本地流程并确认 API 返回 database/api_aggregate、core_data=true 和 database_ids。";
  }
  return "修复 API data_source contract，返回 source_type、core_data、database_ids 和解除条件。";
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
  const canAccept = isCoreDataSource(source);

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
          <dt>can_accept</dt>
          <dd>{displayBoolean(canAccept)}</dd>
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
        <div>
          <dt>required</dt>
          <dd>{requiredAction(source)}</dd>
        </div>
      </dl>
    </div>
  );
}
