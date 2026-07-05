from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.dry_run_status import redact_dry_run_status_payload


AUDIT_LOG_SCHEMA_VERSION = "1"

GovernanceEventStatus = Literal["ACCEPTED", "BLOCKED", "FAILED", "WARNING", "INFO"]
GovernanceEventType = Literal[
    "operator_action",
    "blocked_decision",
    "smoke_run",
    "runtime_contract",
    "review_evidence",
    "artifact_update",
    "security_check",
    "deployment_governance",
]
GovernanceEventSourceType = Literal[
    "api",
    "service",
    "operator",
    "automation",
    "smoke",
    "fixture",
    "artifact",
    "runtime-contract",
    "manual",
]
GovernanceEventActorRole = Literal[
    "system",
    "codex",
    "operator",
    "maintainer",
    "reviewer",
    "risk-owner",
    "automation",
]
GovernanceArtifactKind = Literal["artifact", "manifest", "report", "summary", "log"]

SECRET_TOKEN_VALUE_PATTERN = re.compile(
    r"(?i)\b("
    r"gho_[A-Za-z0-9_]{8,}|"
    r"ghp_[A-Za-z0-9_]{8,}|"
    r"github_pat_[A-Za-z0-9_]{8,}|"
    r"sk-[A-Za-z0-9_-]{8,}"
    r")"
)


class GovernanceEventActor(BaseModel):
    actor_id: str = Field(min_length=1, max_length=120)
    role: GovernanceEventActorRole
    display_name: Optional[str] = Field(default=None, min_length=1, max_length=120)

    model_config = {"extra": "forbid"}

    @model_validator(mode="before")
    @classmethod
    def redact_secret_values(cls, value: Any) -> Any:
        return redact_governance_event_payload(value)

    @field_validator("actor_id", "display_name")
    @classmethod
    def normalize_text(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        clean = value.strip()
        if not clean:
            raise ValueError("governance actor text fields must not be blank")
        return clean


class GovernanceEventSource(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    source_type: GovernanceEventSourceType
    ref: Optional[str] = Field(default=None, min_length=1, max_length=1000)

    model_config = {"extra": "forbid"}

    @model_validator(mode="before")
    @classmethod
    def redact_secret_values(cls, value: Any) -> Any:
        return redact_governance_event_payload(value)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        clean = value.strip()
        if not clean:
            raise ValueError("governance event source name must not be blank")
        return clean

    @field_validator("ref")
    @classmethod
    def validate_ref(cls, value: Optional[str]) -> Optional[str]:
        return _validate_optional_local_ref(value, "governance event source ref")


class GovernanceArtifactLink(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    path: str = Field(min_length=1, max_length=1000)
    kind: GovernanceArtifactKind = "artifact"
    required: bool = False

    model_config = {"extra": "forbid"}

    @model_validator(mode="before")
    @classmethod
    def redact_secret_values(cls, value: Any) -> Any:
        return redact_governance_event_payload(value)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        clean = value.strip()
        if not clean:
            raise ValueError("governance artifact link name must not be blank")
        return clean

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _validate_local_ref(value, "governance artifact link path")


class GovernanceEvent(BaseModel):
    schema_version: Literal["1"] = AUDIT_LOG_SCHEMA_VERSION
    event_id: str = Field(min_length=1, max_length=160)
    event_type: GovernanceEventType
    status: GovernanceEventStatus
    actor: GovernanceEventActor
    source: GovernanceEventSource
    created_at: datetime
    summary: str = Field(min_length=1, max_length=1000)
    reason: Optional[str] = Field(default=None, min_length=1, max_length=1000)
    artifact_links: list[GovernanceArtifactLink] = Field(default_factory=list, max_length=50)
    payload: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list, max_length=50)
    safety_boundary: str = (
        "Governance event archive only; it records read-only audit evidence and does not "
        "authorize live trading, real orders, exchange connections, or deployment control."
    )

    model_config = {"extra": "forbid"}

    @model_validator(mode="before")
    @classmethod
    def redact_secret_values(cls, value: Any) -> Any:
        return redact_governance_event_payload(value)

    @field_validator("event_id", "summary", "reason")
    @classmethod
    def normalize_text(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        clean = value.strip()
        if not clean:
            raise ValueError("governance event text fields must not be blank")
        return clean

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            clean = item.strip()
            if not clean:
                raise ValueError("governance event tags must not contain blank values")
            if clean not in seen:
                normalized.append(clean)
                seen.add(clean)
        return normalized

    @model_validator(mode="after")
    def validate_status_reason(self) -> "GovernanceEvent":
        if self.status == "BLOCKED" and not self.reason:
            raise ValueError("blocked governance events require reason")
        if self.status == "FAILED" and not self.reason:
            raise ValueError("failed governance events require reason")
        return self

    @property
    def event_hash(self) -> str:
        payload = self.model_dump(mode="json", exclude_none=True)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def to_archive_record(self) -> dict[str, Any]:
        record = self.model_dump(mode="json", exclude_none=True)
        record["event_hash"] = self.event_hash
        return record


def redact_governance_event_payload(value: Any) -> Any:
    return _redact_secret_token_values(redact_dry_run_status_payload(value))


def _redact_secret_token_values(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _redact_secret_token_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_secret_token_values(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_secret_token_values(item) for item in value]
    if isinstance(value, str):
        return SECRET_TOKEN_VALUE_PATTERN.sub("[REDACTED]", value)
    return value


def _validate_optional_local_ref(value: Optional[str], field_name: str) -> Optional[str]:
    if value is None:
        return value
    return _validate_local_ref(value, field_name)


def _validate_local_ref(value: str, field_name: str) -> str:
    ref = value.strip()
    if not ref:
        raise ValueError(f"{field_name} must not be blank")
    if ref.startswith(("http://", "https://")):
        raise ValueError(f"{field_name} must not be a remote URL")
    if ".." in ref.split("/"):
        raise ValueError(f"{field_name} must not contain parent traversal")
    return ref
