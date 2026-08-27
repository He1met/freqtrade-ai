"""Minimal catalog for historical or offline research results.

The catalog is descriptive only.  It does not create formal validation lineage,
qualification decisions, deployment authority, or trading permission.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import Connection, func, inspect, select
from sqlalchemy.exc import IntegrityError

from app.canonical_v13.manifest import CANONICAL_BUSINESS_SCHEMA
from app.canonical_v13.models import RESEARCH_RUN_CATALOG_TABLE


class ResearchCatalogBlocked(RuntimeError):
    """The input or persisted catalog conflicts with the minimal contract."""


class ResearchResult(BaseModel):
    model_config = {"extra": "forbid", "frozen": True}

    run_id: str = Field(min_length=1, max_length=160)
    name: str = Field(min_length=1, max_length=240)
    hypothesis: str = Field(min_length=1)
    universe: list[str] = Field(min_length=1)
    timeframe: str = Field(min_length=1, max_length=32)
    status: str = Field(min_length=1, max_length=48)
    reason_code: str = Field(min_length=1, max_length=160)
    dataset_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_path: str = Field(min_length=1)
    result_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    train_validation_holdout_summary: dict[str, Any]
    metrics_summary: dict[str, Any]
    created_at: datetime

    @field_validator(
        "run_id",
        "name",
        "hypothesis",
        "timeframe",
        "status",
        "reason_code",
        "artifact_path",
    )
    @classmethod
    def _trimmed(cls, value: str) -> str:
        if value.strip() != value:
            raise ValueError("text fields must be non-empty and trimmed")
        return value

    @field_validator("universe")
    @classmethod
    def _canonical_universe(cls, value: list[str]) -> list[str]:
        if any(not item or item.strip() != item for item in value):
            raise ValueError("universe members must be non-empty and trimmed")
        if len(set(value)) != len(value):
            raise ValueError("universe members must be unique")
        return value

    @model_validator(mode="after")
    def _offline_only(self) -> "ResearchResult":
        if self.status == "QUALIFIED":
            raise ValueError(
                "formal QUALIFIED results belong in qualification_decisions"
            )
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must include a timezone")
        return self


@dataclass(frozen=True)
class ResearchImportPlan:
    action: Literal["INSERT", "NO_OP", "CREATE_TABLE_AND_INSERT"]
    run_id: str
    result_digest: str
    catalog_row_count: int
    schema_change_required: bool
    row_inserts_required: int


@dataclass(frozen=True)
class ResearchImportResult:
    action: Literal["INSERTED", "NO_OP"]
    run_id: str
    result_digest: str
    catalog_row_count: int


def load_research_result(path: Path) -> ResearchResult:
    if path.name != "research_result.json":
        raise ResearchCatalogBlocked(
            "BLOCKED_RESEARCH_RESULT_FILENAME: input must be named research_result.json"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return ResearchResult.model_validate(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ResearchCatalogBlocked(f"BLOCKED_RESEARCH_RESULT_INVALID: {exc}") from exc


def catalog_table_exists(connection: Connection) -> bool:
    schema = None if connection.dialect.name == "sqlite" else CANONICAL_BUSINESS_SCHEMA
    return inspect(connection).has_table(RESEARCH_RUN_CATALOG_TABLE.name, schema=schema)


def _effective(connection: Connection) -> Connection:
    if connection.dialect.name == "sqlite":
        return connection.execution_options(
            schema_translate_map={CANONICAL_BUSINESS_SCHEMA: None}
        )
    return connection


def _payload(result: ResearchResult) -> dict[str, Any]:
    return result.model_dump(mode="python")


def _persisted_result(row: dict[str, Any]) -> ResearchResult:
    created_at = row.get("created_at")
    if isinstance(created_at, datetime) and created_at.tzinfo is None:
        # SQLite drops timezone metadata; PostgreSQL TIMESTAMPTZ does not.
        row = {**row, "created_at": created_at.replace(tzinfo=timezone.utc)}
    return ResearchResult.model_validate(row)


def plan_research_import(
    connection: Connection, result: ResearchResult
) -> ResearchImportPlan:
    effective = _effective(connection)
    if not catalog_table_exists(effective):
        return ResearchImportPlan(
            action="CREATE_TABLE_AND_INSERT",
            run_id=result.run_id,
            result_digest=result.result_digest,
            catalog_row_count=0,
            schema_change_required=True,
            row_inserts_required=1,
        )
    rows = (
        effective.execute(
            select(RESEARCH_RUN_CATALOG_TABLE).where(
                (RESEARCH_RUN_CATALOG_TABLE.c.run_id == result.run_id)
                | (RESEARCH_RUN_CATALOG_TABLE.c.result_digest == result.result_digest)
            )
        )
        .mappings()
        .all()
    )
    count = int(
        effective.execute(
            select(func.count()).select_from(RESEARCH_RUN_CATALOG_TABLE)
        ).scalar_one()
    )
    if not rows:
        return ResearchImportPlan(
            action="INSERT",
            run_id=result.run_id,
            result_digest=result.result_digest,
            catalog_row_count=count,
            schema_change_required=False,
            row_inserts_required=1,
        )
    if len(rows) == 1 and _persisted_result(dict(rows[0])) == result:
        return ResearchImportPlan(
            action="NO_OP",
            run_id=result.run_id,
            result_digest=result.result_digest,
            catalog_row_count=count,
            schema_change_required=False,
            row_inserts_required=0,
        )
    if any(str(row["run_id"]) == result.run_id for row in rows):
        raise ResearchCatalogBlocked("BLOCKED_RESEARCH_RUN_ID_CONFLICT")
    raise ResearchCatalogBlocked("BLOCKED_RESEARCH_RESULT_DIGEST_CONFLICT")


def apply_research_import(
    connection: Connection, result: ResearchResult
) -> ResearchImportResult:
    effective = _effective(connection)
    plan = plan_research_import(effective, result)
    if plan.action == "CREATE_TABLE_AND_INSERT":
        raise ResearchCatalogBlocked("BLOCKED_RESEARCH_CATALOG_SCHEMA_MISSING")
    if plan.action == "NO_OP":
        return ResearchImportResult(
            action="NO_OP",
            run_id=result.run_id,
            result_digest=result.result_digest,
            catalog_row_count=plan.catalog_row_count,
        )
    try:
        with effective.begin_nested():
            effective.execute(
                RESEARCH_RUN_CATALOG_TABLE.insert().values(**_payload(result))
            )
    except IntegrityError:
        raced = plan_research_import(effective, result)
        if raced.action == "NO_OP":
            return ResearchImportResult(
                action="NO_OP",
                run_id=result.run_id,
                result_digest=result.result_digest,
                catalog_row_count=raced.catalog_row_count,
            )
        raise
    return ResearchImportResult(
        action="INSERTED",
        run_id=result.run_id,
        result_digest=result.result_digest,
        catalog_row_count=plan.catalog_row_count + 1,
    )


def list_research_results(
    connection: Connection, *, limit: int = 100
) -> list[ResearchResult]:
    effective = _effective(connection)
    rows = (
        effective.execute(
            select(RESEARCH_RUN_CATALOG_TABLE)
            .order_by(
                RESEARCH_RUN_CATALOG_TABLE.c.created_at.desc(),
                RESEARCH_RUN_CATALOG_TABLE.c.run_id,
            )
            .limit(limit)
        )
        .mappings()
        .all()
    )
    return [_persisted_result(dict(row)) for row in rows]


def get_research_result(
    connection: Connection, *, run_id: str
) -> ResearchResult | None:
    effective = _effective(connection)
    row = (
        effective.execute(
            select(RESEARCH_RUN_CATALOG_TABLE).where(
                RESEARCH_RUN_CATALOG_TABLE.c.run_id == run_id
            )
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else _persisted_result(dict(row))


__all__ = [
    "ResearchCatalogBlocked",
    "ResearchImportPlan",
    "ResearchImportResult",
    "ResearchResult",
    "apply_research_import",
    "catalog_table_exists",
    "get_research_result",
    "list_research_results",
    "load_research_result",
    "plan_research_import",
]
