from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from app.schemas.dry_run_status import DryRunStatusSnapshot
from app.schemas.live_candidate import LiveCandidateMonitoringSnapshot


RUNTIME_READ_ONLY_CONTRACT_SCHEMA_VERSION = "2"

RuntimeReadOnlyStatus = Literal["READY", "WARNING", "STALE", "UNAVAILABLE", "BLOCKED"]
RuntimeReadOnlySource = Literal[
    "settings",
    "derived",
    "artifact",
    "controlled-local-json",
    "fixture",
    "missing",
]


class RuntimeStatusSummary(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    status: RuntimeReadOnlyStatus
    summary: str = Field(min_length=1, max_length=1000)
    source: RuntimeReadOnlySource
    source_ref: Optional[str] = Field(default=None, max_length=1000)
    last_updated: Optional[datetime] = None
    blocked_reason: Optional[str] = Field(default=None, max_length=1000)
    unavailable_reason: Optional[str] = Field(default=None, max_length=1000)
    stale_reason: Optional[str] = Field(default=None, max_length=1000)
    warnings: list[str] = Field(default_factory=list, max_length=50)

    model_config = {"extra": "forbid"}

    @field_validator("source_ref")
    @classmethod
    def reject_remote_refs(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        ref = value.strip()
        if ref.startswith(("http://", "https://")):
            raise ValueError("runtime contract source_ref must not be a remote URL")
        return ref

    @field_validator("warnings")
    @classmethod
    def normalize_warnings(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item.strip()]


class RuntimeArtifactLink(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    path: str = Field(min_length=1, max_length=1000)
    source: RuntimeReadOnlySource
    status: RuntimeReadOnlyStatus
    exists: bool = False

    model_config = {"extra": "forbid"}

    @field_validator("path")
    @classmethod
    def reject_remote_path(cls, value: str) -> str:
        path = value.strip()
        if path.startswith(("http://", "https://")):
            raise ValueError("runtime contract artifact links must not be remote URLs")
        return path


class RuntimeFallbackStatus(BaseModel):
    active: bool
    status: RuntimeReadOnlyStatus
    reason: Optional[str] = Field(default=None, max_length=1000)
    sources: list[str] = Field(default_factory=list, max_length=20)

    model_config = {"extra": "forbid"}


class RuntimeSafetyBoundary(BaseModel):
    read_only: Literal[True] = True
    allow_live_trading: Literal[False] = False
    allow_real_orders: Literal[False] = False
    allow_exchange_connection: Literal[False] = False
    allow_deploy_control: Literal[False] = False
    can_start_stop_bot: Literal[False] = False
    boundary: str = (
        "Runtime contract is read-only status evidence only; it cannot start, stop, deploy, "
        "connect to exchanges, or place orders."
    )

    model_config = {"extra": "forbid"}


class RuntimeReadOnlyContract(BaseModel):
    schema_version: Literal["2"] = RUNTIME_READ_ONLY_CONTRACT_SCHEMA_VERSION
    status: RuntimeReadOnlyStatus
    generated_at: datetime
    system_status: RuntimeStatusSummary
    runtime_readiness: RuntimeStatusSummary
    research_readiness: RuntimeStatusSummary
    dry_run_readiness: RuntimeStatusSummary
    live_readiness: RuntimeStatusSummary
    fallback_status: RuntimeFallbackStatus
    smoke_status: RuntimeStatusSummary
    dry_run_status: DryRunStatusSnapshot
    live_candidate_monitoring: LiveCandidateMonitoringSnapshot
    artifact_links: list[RuntimeArtifactLink] = Field(default_factory=list, max_length=20)
    blocked_reasons: list[str] = Field(default_factory=list, max_length=50)
    unavailable_reasons: list[str] = Field(default_factory=list, max_length=50)
    safety: RuntimeSafetyBoundary = Field(default_factory=RuntimeSafetyBoundary)

    model_config = {"extra": "forbid"}

    @field_validator("blocked_reasons", "unavailable_reasons")
    @classmethod
    def normalize_reasons(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            clean = item.strip()
            if clean and clean not in seen:
                normalized.append(clean)
                seen.add(clean)
        return normalized
