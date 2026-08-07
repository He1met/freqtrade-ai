from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


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
