from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.core.strategy_research_contract import official_research_policy


ResearchBatchStatus = Literal["GENERATED", "VALIDATED", "FAILED"]
ResearchCandidateStatus = Literal["QUALIFIED", "REJECTED", "VALIDATION_FAILED"]


class StrategyResearchCandidateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    batch_id: int
    candidate_name: str
    source_path: str
    code_digest: str
    status: ResearchCandidateStatus
    loadable: bool
    static_check: str
    lookahead_status: str
    score: Optional[float]
    validation_passed: bool
    deployable_candidate: bool
    rejection_reasons: list[dict] = Field(default_factory=list)
    evidence_snapshot: dict = Field(default_factory=dict)
    quality_contract: dict = Field(default_factory=dict)
    created_at: datetime


class StrategyResearchBatchRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    run_id: str
    source_type: str
    repository_commit: str
    report_schema_version: str
    report_path: str
    report_digest: str
    status: ResearchBatchStatus
    requested_count: int
    generated_count: int
    persisted_count: int
    qualified_count: int
    rejected_count: int
    failure_reason: Optional[str]
    safety_snapshot: dict = Field(default_factory=dict)
    selection_policy: dict = Field(default_factory=dict)
    window_evidence: list[dict] = Field(default_factory=list)
    completed_at: Optional[datetime]
    created_at: datetime
    candidates: list[StrategyResearchCandidateRead] = Field(default_factory=list)


FormalResearchRunStatus = Literal["READY", "RUNNING", "COMPLETED", "BLOCKED", "FAILED"]
FormalResearchRunPhase = Literal["STARTING", "RUNNING", "TERMINATING", "FINISHED"]
FormalResearchCleanupStatus = Literal[
    "NOT_REQUIRED",
    "TERM_CONFIRMED",
    "KILL_CONFIRMED",
    "UNCONFIRMED",
]


class FormalResearchRunRead(BaseModel):
    status: FormalResearchRunStatus
    reason_code: str
    reason: str
    active: bool
    run_id: Optional[str] = None
    trigger: Optional[Literal["manual", "automation"]] = None
    started_at: Optional[datetime] = None
    heartbeat_at: Optional[datetime] = None
    deadline_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    phase: Optional[FormalResearchRunPhase] = None
    cleanup_status: Optional[FormalResearchCleanupStatus] = None
    requested_count: int = 10
    generated_count: int = 0
    validated_count: int = 0
    persisted_count: int = 0
    qualified_count: int = 0
    rejected_count: int = 0
    deployment_handoff_status: Literal[
        "NOT_EVALUATED",
        "NOT_QUEUED_NO_QUALIFIED",
        "QUEUED_FOR_EXISTING_AUTOMATION",
    ] = "NOT_EVALUATED"
    quality_contract: dict = Field(default_factory=official_research_policy)
    safety: dict = Field(
        default_factory=lambda: {
            "execution_target": "OKX_DEMO",
            "allow_real_funds": False,
            "real_orders": False,
            "credentials_collected": False,
            "dry_run_trading_authorized": False,
            "grant_authorized": False,
            "manual_order_authorized": False,
        }
    )
