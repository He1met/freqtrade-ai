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


class FormalResearchSafetyRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_target: Literal["OKX_DEMO"] = "OKX_DEMO"
    allow_real_funds: Literal[False] = False
    real_orders: Literal[False] = False
    credentials_collected: Literal[False] = False
    dry_run_trading_authorized: Literal[False] = False
    grant_authorized: Literal[False] = False
    manual_order_authorized: Literal[False] = False


class FormalResearchRunRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: FormalResearchRunStatus
    reason_code: str
    reason: str
    active: bool
    attempt_id: Optional[str] = None
    market_data_quality_receipt_id: Optional[int] = None
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
        "CANONICAL_LINK_UNAVAILABLE",
    ] = "NOT_EVALUATED"
    quality_contract: dict = Field(default_factory=official_research_policy)
    safety: FormalResearchSafetyRead = Field(default_factory=FormalResearchSafetyRead)


class StrategyResearchAttemptEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: int
    attempt_id: str
    sequence: int
    run_id: Optional[str] = None
    batch_id: Optional[int] = None
    market_data_quality_receipt_id: Optional[int] = None
    trigger: Literal["manual", "automation"]
    phase: Literal["PRECHECK", "STARTED", "TERMINAL", "RECOVERY"]
    outcome: Literal["NOT_GENERATED", "RUNNING", "COMPLETED", "FAILED", "BLOCKED"]
    reason_code: str
    redacted_reason: str
    requested_count: int
    generated_count: int
    validated_count: int
    persisted_count: int
    qualified_count: int
    rejected_count: int
    created_at: datetime


class StrategyResearchAttemptRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt_id: str
    latest_outcome: Literal["NOT_GENERATED", "RUNNING", "COMPLETED", "FAILED", "BLOCKED"]
    events: list[StrategyResearchAttemptEventRead]


class MarketDataQualityReceiptRead(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: int
    contract_version: str
    exchange: str
    pair: str
    timeframe: str
    file_format: str
    inspected_at: datetime
    row_count: int
    first_open_at: Optional[datetime] = None
    last_open_at: Optional[datetime] = None
    expected_interval_seconds: int
    missing_interval_count: int
    duplicate_timestamp_count: int
    out_of_order_count: int
    misaligned_timestamp_count: int
    null_ohlcv_count: int
    invalid_ohlc_count: int
    negative_volume_count: int
    freshness_seconds: Optional[int] = None
    status: Literal["PASSED", "BLOCKED", "FAILED"]
    reason_codes: list[str]
    created_at: datetime


class StrategyResearchWorkspaceSectionRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["AVAILABLE", "UNKNOWN"]
    reason_code: Optional[str] = None


class StrategyResearchWorkspaceSectionsRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempts: StrategyResearchWorkspaceSectionRead
    quality: StrategyResearchWorkspaceSectionRead
    batch: StrategyResearchWorkspaceSectionRead
    bridge: StrategyResearchWorkspaceSectionRead
    approval: StrategyResearchWorkspaceSectionRead
    deployment: StrategyResearchWorkspaceSectionRead


CandidateLifecycleStatus = Literal[
    "NOT_APPLICABLE_REJECTED",
    "NOT_APPLICABLE_VALIDATION_FAILED",
    "UNBRIDGED_REVALIDATION_REQUIRED",
    "BRIDGED_PENDING_CANONICAL_VALIDATION",
    "BRIDGED_PENDING_APPROVAL",
    "BRIDGED_APPROVAL_REJECTED",
    "APPROVED_NOT_DEPLOYED",
    "DEPLOYED_ACTIVE_DEMO",
    "DEPLOYED_DISABLED",
    "UNKNOWN",
]


class StrategyResearchCandidateLifecycleRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: int
    batch_id: int
    candidate_name: str
    research_status: ResearchCandidateStatus
    lifecycle_status: CandidateLifecycleStatus
    reason_code: str
    source_code_digest: str
    bridge_event_id: Optional[int] = None
    bridge_outcome: Optional[Literal["REVALIDATION_REQUIRED", "BRIDGED", "FAILED"]] = None
    bridge_contract_version: Optional[str] = None
    blueprint_digest: Optional[str] = None
    canonical_strategy_id: Optional[int] = None
    canonical_strategy_version_id: Optional[int] = None
    canonical_full_chain_run_id: Optional[int] = None
    candidate_approval_id: Optional[int] = None
    candidate_approval_status: Optional[
        Literal["PENDING", "APPROVED", "REJECTED", "EXPIRED", "REVOKED"]
    ] = None
    deployment_id: Optional[int] = None
    deployment_status: Optional[Literal["ACTIVE", "DISABLED"]] = None
    active_slot: Optional[int] = None
    created_at: Optional[datetime] = None


class StrategyResearchLifecycleSummaryRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal[
        "NOT_EVALUATED",
        "NOT_QUEUED_NO_QUALIFIED",
        "UNBRIDGED_REVALIDATION_REQUIRED",
        "BRIDGED_PENDING_CANONICAL_VALIDATION",
        "BRIDGED_PENDING_APPROVAL",
        "APPROVED_NOT_DEPLOYED",
        "DEPLOYED_ACTIVE_DEMO",
        "MIXED",
        "UNKNOWN",
    ]
    qualified_count: int
    unbridged_count: int
    pending_canonical_validation_count: int
    pending_approval_count: int
    approved_not_deployed_count: int
    active_demo_count: int
    unknown_count: int
    reason_code: str


class StrategyResearchWorkspaceRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[
        "formal-strategy-research-workspace-v1",
        "formal-strategy-research-workspace-v2",
    ]
    as_of: datetime
    source_type: Literal["database"]
    core_data: Literal[True]
    execution_target_id: Literal["OKX_DEMO"] = "OKX_DEMO"
    allow_real_funds: Literal[False] = False
    real_orders: Literal[False] = False
    evidence_status: Literal["COMPLETE", "PARTIAL"]
    sections: StrategyResearchWorkspaceSectionsRead
    attempts: list[StrategyResearchAttemptRead]
    latest_quality_receipt: Optional[MarketDataQualityReceiptRead] = None
    latest_batch: Optional[StrategyResearchBatchRead] = None
    lifecycle_summary: StrategyResearchLifecycleSummaryRead
    candidate_lifecycles: list[StrategyResearchCandidateLifecycleRead]
    handoff_status: Literal[
        "NOT_EVALUATED",
        "NOT_QUEUED_NO_QUALIFIED",
        "CANONICAL_LINK_UNAVAILABLE",
        "UNKNOWN",
    ]
