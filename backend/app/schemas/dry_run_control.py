from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

from app.schemas.dry_run_readiness import DryRunReadinessReport, DryRunReadinessRequest
from app.schemas.dry_run_status import DryRunStatusSnapshot


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
