from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

from app.schemas.data_source import api_aggregate_source
from app.schemas.dry_run_readiness import DryRunReadinessReport, DryRunReadinessRequest
from app.schemas.dry_run_status import DryRunStatusSnapshot
from app.schemas.operation_evidence import OperationEvidence


DryRunControlStatus = Literal["SUCCESS", "FAILED", "BLOCKED", "SKIPPED", "STOPPED"]


class DryRunControlStartRequest(DryRunReadinessRequest):
    manual_approval: bool = False
    timeout_seconds: int = Field(default=30, ge=5, le=300)


class DryRunControlStopRequest(BaseModel):
    reason: str = Field(default="manual stop requested", min_length=1, max_length=500)

    model_config = {"extra": "forbid"}


class DryRunControlReport(BaseModel):
    status: DryRunControlStatus
    generated_at: datetime
    manifest_path: Optional[str] = None
    config_path: Optional[str] = None
    status_snapshot_path: str
    readiness: Optional[DryRunReadinessReport] = None
    status_snapshot: DryRunStatusSnapshot
    blocked_reasons: list[str] = Field(default_factory=list)
    failed_reason: Optional[str] = None
    skipped_reason: Optional[str] = None
    safety: dict[str, bool]
    evidence: Optional[OperationEvidence] = None

    @model_validator(mode="after")
    def attach_operation_evidence(self) -> "DryRunControlReport":
        ids = {}
        if self.readiness is not None:
            ids["strategy_version_id"] = self.readiness.strategy_version_id
        elif self.status_snapshot.strategy_version_id is not None:
            ids["strategy_version_id"] = self.status_snapshot.strategy_version_id
        artifact_refs = {"dry_run_status_snapshot_path": self.status_snapshot_path}
        if self.manifest_path:
            artifact_refs["artifact_manifest_path"] = self.manifest_path
        if self.config_path:
            artifact_refs["dry_run_config_path"] = self.config_path
        source = api_aggregate_source(
            "controlled_dry_run",
            ids,
            artifact_refs=artifact_refs,
            freshness=self.generated_at,
        )
        blocked_reason = "; ".join(self.blocked_reasons) or None
        failed_reason = self.failed_reason
        evidence_status = (
            "FAILED"
            if self.status == "FAILED"
            else "BLOCKED"
            if self.status in {"BLOCKED", "SKIPPED"}
            else "SUCCESS"
        )
        if evidence_status == "BLOCKED" and not blocked_reason:
            blocked_reason = self.skipped_reason or "controlled dry-run operation was skipped"
        self.evidence = OperationEvidence(
            status=evidence_status,  # type: ignore[arg-type]
            ids=ids,
            artifact_refs=artifact_refs,
            data_source=source,
            blocked_reason=blocked_reason if evidence_status == "BLOCKED" else None,
            failed_reason=(
                failed_reason or "controlled dry-run operation failed"
                if evidence_status == "FAILED"
                else None
            ),
            next_action=(
                "Review the persisted dry-run status and artifact manifest."
                if evidence_status == "SUCCESS"
                else "Resolve the reported dry-run condition before retrying the controlled operation."
            ),
            acceptance_ready=evidence_status == "SUCCESS" and bool(ids),
        )
        return self
