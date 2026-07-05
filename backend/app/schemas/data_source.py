from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator


DataSourceType = Literal["database", "api_aggregate", "fixture", "fallback", "unknown"]


class DataSourceTrace(BaseModel):
    """Describes where a response came from and whether it can prove core success."""

    source_type: DataSourceType
    source_detail: str = Field(min_length=1, max_length=240)
    core_data: bool = False
    database_ids: dict[str, int] = Field(default_factory=dict)
    artifact_refs: dict[str, str] = Field(default_factory=dict)
    freshness: Optional[datetime] = None
    blocked_reason: Optional[str] = None

    @model_validator(mode="after")
    def reject_non_core_sources_claiming_core_success(self) -> "DataSourceTrace":
        if self.source_type in {"fixture", "fallback", "unknown"} and self.core_data:
            raise ValueError(f"{self.source_type} data cannot satisfy core success")
        if self.source_type in {"database", "api_aggregate"} and self.core_data and not self.database_ids:
            raise ValueError(f"{self.source_type} source requires database ids for core success")
        return self


def database_record_source(
    record_type: str,
    database_ids: dict[str, int],
    *,
    artifact_refs: Optional[dict[str, str]] = None,
    freshness: Optional[datetime] = None,
) -> DataSourceTrace:
    return DataSourceTrace(
        source_type="database",
        source_detail=f"{record_type} record loaded from application database",
        core_data=True,
        database_ids=database_ids,
        artifact_refs=artifact_refs or {},
        freshness=freshness,
    )


def api_aggregate_source(
    aggregate_name: str,
    database_ids: dict[str, int],
    *,
    artifact_refs: Optional[dict[str, str]] = None,
    freshness: Optional[datetime] = None,
) -> DataSourceTrace:
    return DataSourceTrace(
        source_type="api_aggregate",
        source_detail=f"{aggregate_name} assembled from backend API and database records",
        core_data=True,
        database_ids=database_ids,
        artifact_refs=artifact_refs or {},
        freshness=freshness,
    )


def fixture_source(source_detail: str, *, blocked_reason: Optional[str] = None) -> DataSourceTrace:
    return DataSourceTrace(
        source_type="fixture",
        source_detail=source_detail,
        core_data=False,
        blocked_reason=blocked_reason,
    )


def fallback_source(source_detail: str, *, blocked_reason: Optional[str] = None) -> DataSourceTrace:
    return DataSourceTrace(
        source_type="fallback",
        source_detail=source_detail,
        core_data=False,
        blocked_reason=blocked_reason,
    )


def unknown_source(source_detail: str, *, blocked_reason: Optional[str] = None) -> DataSourceTrace:
    return DataSourceTrace(
        source_type="unknown",
        source_detail=source_detail,
        core_data=False,
        blocked_reason=blocked_reason,
    )


def phase8_local_test_metadata_from_tags(tags: list[str]) -> Optional[dict[str, Any]]:
    if "phase8-local-test" not in tags:
        return None

    parsed: dict[str, str] = {}
    for tag in tags:
        if ":" not in tag:
            continue
        key, value = tag.split(":", 1)
        parsed[key] = value

    return {
        "phase8_local_test": True,
        "test_batch": {
            "batch_key": parsed.get("test-batch", "unknown"),
            "scenario": parsed.get("scenario", "unknown"),
            "source_kind": parsed.get("source-kind", "seed_generated"),
        },
        "not_core_success": True,
    }


def phase8_local_test_metadata_from_payload(*payloads: Any) -> Optional[dict[str, Any]]:
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        direct = payload.get("phase8_local_test")
        if isinstance(direct, dict) and direct.get("phase8_local_test") is True:
            return direct
        if payload.get("phase8_local_test") is True:
            return payload
    return None


def phase8_local_test_source(
    record_type: str,
    metadata: Optional[dict[str, Any]],
    database_ids: dict[str, int],
    *,
    artifact_refs: Optional[dict[str, str]] = None,
    freshness: Optional[datetime] = None,
) -> Optional[DataSourceTrace]:
    if not metadata or metadata.get("phase8_local_test") is not True:
        return None

    batch = metadata.get("test_batch") if isinstance(metadata.get("test_batch"), dict) else {}
    scenario = batch.get("scenario", "unknown")
    source_kind = batch.get("source_kind", "seed_generated")
    batch_key = batch.get("batch_key", "unknown")
    blocked_reason = metadata.get("blocked_reason")
    source_payload = metadata.get("data_source") if isinstance(metadata.get("data_source"), dict) else {}
    source_type = source_payload.get("source_type", "fixture")
    if source_type not in {"fixture", "fallback", "unknown"}:
        source_type = "fixture"

    return DataSourceTrace(
        source_type=source_type,
        source_detail=(
            f"Phase 8 local-test {record_type} row from {source_kind} "
            f"scenario={scenario} batch={batch_key}; not core success."
        ),
        core_data=False,
        database_ids=database_ids,
        artifact_refs=artifact_refs or {},
        freshness=freshness,
        blocked_reason=blocked_reason,
    )


def attach_data_source_to_payload(payload: Any, data_source: DataSourceTrace) -> Any:
    """Return a copy of a JSON-like payload with source metadata on object nodes."""

    source_payload = data_source.model_dump(mode="json")
    copied = deepcopy(payload)
    if isinstance(copied, list):
        return [
            {**item, "data_source": source_payload} if isinstance(item, dict) else item
            for item in copied
        ]
    if isinstance(copied, dict):
        copied["data_source"] = source_payload
    return copied
