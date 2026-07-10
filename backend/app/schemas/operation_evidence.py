from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

from app.schemas.data_source import DataSourceTrace, unknown_source


OperationStatus = Literal["SUCCESS", "FAILED", "BLOCKED"]


class OperationEvidence(BaseModel):
    """Stable evidence contract for state-changing API operations."""

    status: OperationStatus
    ids: dict[str, int] = Field(default_factory=dict)
    artifact_refs: dict[str, str] = Field(default_factory=dict)
    data_source: DataSourceTrace = Field(
        default_factory=lambda: unknown_source("operation evidence source was not established")
    )
    blocked_reason: Optional[str] = None
    failed_reason: Optional[str] = None
    next_action: str = Field(min_length=1, max_length=500)
    acceptance_ready: bool = False

    @model_validator(mode="after")
    def enforce_status_contract(self) -> "OperationEvidence":
        if self.status == "SUCCESS":
            if self.blocked_reason or self.failed_reason:
                raise ValueError("SUCCESS evidence cannot include blocked_reason or failed_reason")
            if self.acceptance_ready and not self.ids:
                raise ValueError("acceptance-ready SUCCESS evidence requires database ids")
            if self.acceptance_ready and not self.data_source.core_data:
                raise ValueError("acceptance-ready SUCCESS evidence requires a core data source")
        elif self.status == "BLOCKED":
            if not self.blocked_reason:
                raise ValueError("BLOCKED evidence requires blocked_reason")
        elif not self.failed_reason:
            raise ValueError("FAILED evidence requires failed_reason")
        if self.status != "SUCCESS" and self.acceptance_ready:
            raise ValueError(f"{self.status} evidence cannot be acceptance ready")
        return self


def operation_error_evidence(
    *,
    status: Literal["FAILED", "BLOCKED"],
    reason: str,
    next_action: str,
    ids: Optional[dict[str, int]] = None,
    artifact_refs: Optional[dict[str, str]] = None,
    data_source: Optional[DataSourceTrace] = None,
) -> OperationEvidence:
    return OperationEvidence(
        status=status,
        ids=ids or {},
        artifact_refs=artifact_refs or {},
        data_source=data_source or unknown_source(reason, blocked_reason=reason),
        blocked_reason=reason if status == "BLOCKED" else None,
        failed_reason=reason if status == "FAILED" else None,
        next_action=next_action,
        acceptance_ready=False,
    )
