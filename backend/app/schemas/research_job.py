from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

from app.schemas.data_source import DataSourceTrace, database_record_source, unknown_source


ResearchJobStatus = Literal[
    "PENDING",
    "RUNNING",
    "SUCCESS",
    "FAILED",
    "BLOCKED",
    "CANCELLED",
    "STALE",
]


class ResearchJobRead(BaseModel):
    id: int
    job_type: str
    operation: str
    request_hash: str
    status: ResearchJobStatus
    stage: str
    lease_owner: Optional[str]
    lease_expires_at: Optional[datetime]
    heartbeat_at: Optional[datetime]
    attempt_count: int
    max_attempts: int
    cancel_requested: bool
    provider_attempted_at: Optional[datetime]
    provider_completed_at: Optional[datetime]
    strategy_generation_run_id: Optional[int]
    strategy_id: Optional[int]
    strategy_version_id: Optional[int]
    backtest_run_id: Optional[int]
    backtest_task_id: Optional[int]
    backtest_result_id: Optional[int]
    strategy_score_id: Optional[int]
    evidence_snapshot: dict = Field(default_factory=dict)
    error_message: Optional[str]
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime
    status_url: Optional[str] = None
    data_source: DataSourceTrace = Field(
        default_factory=lambda: unknown_source("unvalidated research job source")
    )

    model_config = {"from_attributes": True}

    @model_validator(mode="after")
    def attach_database_source(self) -> "ResearchJobRead":
        database_ids = {"research_job_id": self.id}
        for key in (
            "strategy_generation_run_id",
            "strategy_id",
            "strategy_version_id",
            "backtest_run_id",
            "backtest_task_id",
            "backtest_result_id",
            "strategy_score_id",
        ):
            value = getattr(self, key)
            if value is not None:
                database_ids[key] = value
        artifact_refs = self.evidence_snapshot.get("artifact_refs", {})
        self.status_url = self.status_url or f"/api/deepseek-backtest-jobs/{self.id}"
        self.data_source = database_record_source(
            "research_job",
            database_ids,
            artifact_refs=artifact_refs if isinstance(artifact_refs, dict) else {},
            freshness=self.updated_at,
        )
        return self


class ResearchWorkerControlRead(BaseModel):
    paused: bool
    reason: Optional[str]
    updated_at: datetime
    active_job_id: Optional[int] = None
    pending_jobs: int = 0
    running_jobs: int = 0
    stale_jobs: int = 0


class ResearchWorkerPauseRequest(BaseModel):
    reason: str = Field(default="Paused by local operator.", min_length=1, max_length=500)


class ResearchJobCancelRequest(BaseModel):
    reason: str = Field(default="Cancelled by local operator.", min_length=1, max_length=500)
