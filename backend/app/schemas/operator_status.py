from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.dry_run_status import redact_dry_run_status_payload
from app.schemas.runtime_contract import RuntimeReadOnlyStatus


OPERATOR_STATUS_SCHEMA_VERSION = "1"

OperatorReadinessStatus = Literal["READY", "BLOCKED", "UNAVAILABLE"]
OperatorDiagnosticArea = Literal[
    "repo",
    "config",
    "artifact",
    "smoke",
    "runtime_contract",
    "security",
    "env",
]
OperatorDiagnosticSource = Literal[
    "filesystem",
    "settings",
    "env",
    "artifact",
    "derived",
    "runtime-contract",
]


class OperatorSafetyBoundary(BaseModel):
    read_only: Literal[True] = True
    reports_env_values: Literal[False] = False
    allow_live_trading: Literal[False] = False
    allow_real_orders: Literal[False] = False
    allow_exchange_connection: Literal[False] = False
    allow_deploy_control: Literal[False] = False
    can_start_stop_bot: Literal[False] = False
    boundary: str = (
        "Operator status is local read-only diagnostics only; it cannot start bots, "
        "deploy runtime services, connect to exchanges, or reveal ENV values."
    )

    model_config = {"extra": "forbid"}


class OperatorDiagnosticCheck(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    area: OperatorDiagnosticArea
    status: OperatorReadinessStatus
    source: OperatorDiagnosticSource
    summary: str = Field(min_length=1, max_length=1000)
    path: Optional[str] = Field(default=None, max_length=1000)
    exists: Optional[bool] = None
    required: bool = True
    blocked_reason: Optional[str] = Field(default=None, max_length=1000)
    unavailable_reason: Optional[str] = Field(default=None, max_length=1000)
    warnings: list[str] = Field(default_factory=list, max_length=50)

    model_config = {"extra": "forbid"}

    @model_validator(mode="before")
    @classmethod
    def redact_secret_values(cls, value: Any) -> Any:
        return redact_dry_run_status_payload(value)

    @field_validator("path")
    @classmethod
    def reject_remote_path(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        path = value.strip()
        if path.startswith(("http://", "https://")):
            raise ValueError("operator diagnostic paths must be local filesystem paths")
        return path

    @field_validator("warnings")
    @classmethod
    def normalize_warnings(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item.strip()]


class OperatorArtifactStatus(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    path: str = Field(min_length=1, max_length=1000)
    status: OperatorReadinessStatus
    source: OperatorDiagnosticSource
    exists: bool

    model_config = {"extra": "forbid"}

    @field_validator("path")
    @classmethod
    def reject_remote_path(cls, value: str) -> str:
        path = value.strip()
        if path.startswith(("http://", "https://")):
            raise ValueError("operator artifact paths must be local filesystem paths")
        return path


class OperatorEnvPresence(BaseModel):
    name: str = Field(min_length=1, max_length=120, pattern=r"^[A-Z0-9_]+$")
    present: bool
    required: bool = False
    source: Literal["env"] = "env"
    value_rendered: Literal[False] = False

    model_config = {"extra": "forbid"}


class OperatorRuntimeContractSummary(BaseModel):
    status: RuntimeReadOnlyStatus
    runtime_readiness_status: RuntimeReadOnlyStatus
    fallback_active: bool
    smoke_status: RuntimeReadOnlyStatus
    artifact_count: int = Field(ge=0)
    blocked_reasons: list[str] = Field(default_factory=list, max_length=50)
    unavailable_reasons: list[str] = Field(default_factory=list, max_length=50)

    model_config = {"extra": "forbid"}

    @model_validator(mode="before")
    @classmethod
    def redact_secret_values(cls, value: Any) -> Any:
        return redact_dry_run_status_payload(value)


class OperatorStatusReport(BaseModel):
    schema_version: Literal["1"] = OPERATOR_STATUS_SCHEMA_VERSION
    status: OperatorReadinessStatus
    generated_at: datetime
    repo_root: str = Field(min_length=1, max_length=1000)
    checks: list[OperatorDiagnosticCheck] = Field(default_factory=list, max_length=100)
    artifacts: list[OperatorArtifactStatus] = Field(default_factory=list, max_length=50)
    env_presence: list[OperatorEnvPresence] = Field(default_factory=list, max_length=50)
    runtime_contract: OperatorRuntimeContractSummary
    blocked_reasons: list[str] = Field(default_factory=list, max_length=50)
    unavailable_reasons: list[str] = Field(default_factory=list, max_length=50)
    warnings: list[str] = Field(default_factory=list, max_length=100)
    safety: OperatorSafetyBoundary = Field(default_factory=OperatorSafetyBoundary)

    model_config = {"extra": "forbid"}

    @model_validator(mode="before")
    @classmethod
    def redact_secret_values(cls, value: Any) -> Any:
        return redact_dry_run_status_payload(value)

    @field_validator("repo_root")
    @classmethod
    def reject_remote_repo_root(cls, value: str) -> str:
        path = value.strip()
        if path.startswith(("http://", "https://")):
            raise ValueError("operator repo_root must be a local filesystem path")
        return path

    @field_validator("blocked_reasons", "unavailable_reasons", "warnings")
    @classmethod
    def normalize_text_list(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            clean = item.strip()
            if clean and clean not in seen:
                normalized.append(clean)
                seen.add(clean)
        return normalized
