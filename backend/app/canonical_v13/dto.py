"""Canonical-only V1.3 API data-transfer contracts.

Command DTOs are deliberately separate from projection/receipt DTOs.  This module
contains no SQLAlchemy models and no legacy identifiers, defaults, or readiness
inference.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field


SHA256_PATTERN = r"^[0-9a-f]{64}$"


class CanonicalCommandDTO(BaseModel):
    model_config = {"extra": "forbid", "populate_by_name": True}


class CanonicalProjectionDTO(BaseModel):
    model_config = {"extra": "forbid", "frozen": True, "populate_by_name": True}


class CanonicalErrorDetailDTO(CanonicalProjectionDTO):
    code: str
    detail: str


class CanonicalErrorResponseDTO(CanonicalProjectionDTO):
    status: Literal["BLOCKED"] = "BLOCKED"
    error: CanonicalErrorDetailDTO


class SubmissionVersionCommandDTO(CanonicalCommandDTO):
    source_strategy_key: str = Field(min_length=1, max_length=200)
    version_id: str = Field(min_length=1, max_length=200)
    version_number: int = Field(gt=0)
    artifact_base64: str = Field(min_length=1)


class SubmissionCommandDTO(CanonicalCommandDTO):
    caller_identity: str = Field(min_length=1, max_length=160)
    idempotency_key: str = Field(min_length=1, max_length=200)
    display_name: str = Field(min_length=1, max_length=240)
    archive_snapshot_digest: str = Field(pattern=SHA256_PATTERN)
    source_entry_key: str = Field(min_length=1, max_length=500)
    source_strategy_key: str = Field(min_length=1, max_length=200)
    current_version_id: str = Field(min_length=1, max_length=200)
    versions: list[SubmissionVersionCommandDTO] = Field(min_length=1)


class SubmissionReceiptDTO(CanonicalProjectionDTO):
    submission_id: UUID
    artifact_id: UUID
    strategy_id: UUID
    strategy_version_id: UUID
    intake_receipt_id: UUID
    request_digest: str = Field(pattern=SHA256_PATTERN)
    artifact_digest: str = Field(pattern=SHA256_PATTERN)
    receipt_digest: str = Field(pattern=SHA256_PATTERN)
    intake_status: Literal["INTAKE_ACCEPTED"]
    catalog_status: Literal["DRAFT"]
    validation_status: Literal["UNVALIDATED"]
    qualification_status: Literal["NOT_EVALUATED"] = "NOT_EVALUATED"
    execution_authorized: Literal[False]
    idempotent_replay: bool


class StrategyProjectionDTO(CanonicalProjectionDTO):
    strategy_id: UUID
    display_name: str
    catalog_status: Literal["DRAFT", "ACTIVE", "ARCHIVED"]
    intake_status: Literal["INTAKE_ACCEPTED", "REJECTED", "BLOCKED"]
    current_version_id: UUID
    version_number: int
    artifact_id: UUID
    artifact_digest: str = Field(pattern=SHA256_PATTERN)
    validation_status: Literal[
        "UNVALIDATED", "VALIDATING", "VALIDATED", "REJECTED", "BLOCKED"
    ]
    qualification_status: Literal[
        "NOT_EVALUATED", "PENDING", "QUALIFIED", "REJECTED", "BLOCKED", "FAILED"
    ]
    execution_authorized: bool
    created_at: datetime


class StrategyCatalogProjectionDTO(CanonicalProjectionDTO):
    status: Literal["EMPTY", "AVAILABLE"]
    items: list[StrategyProjectionDTO]


class ConfigurationDependencyCommandDTO(CanonicalCommandDTO):
    version_id: UUID
    expected_kind: str = Field(min_length=1, max_length=32)
    relation_key: str = Field(min_length=1, max_length=120)


class ConfigurationDraftCommandDTO(CanonicalCommandDTO):
    actor_identity: str = Field(min_length=1, max_length=160)
    idempotency_key: str = Field(min_length=1, max_length=200)
    profile_key: str = Field(min_length=1, max_length=160)
    scope_key: str = Field(min_length=1, max_length=200)
    workflow_key: str = Field(min_length=1, max_length=160)
    configuration_schema: dict[str, Any] = Field(alias="schema_json")
    payload_json: dict[str, Any]
    adapter_identity: str = Field(min_length=1, max_length=200)
    adapter_digest: str = Field(pattern=SHA256_PATTERN)
    dependencies: list[ConfigurationDependencyCommandDTO] = Field(default_factory=list)


class ConfigurationValidateCommandDTO(CanonicalCommandDTO):
    actor_identity: str = Field(min_length=1, max_length=160)
    idempotency_key: str = Field(min_length=1, max_length=200)
    adapter_manifest_digest: str = Field(pattern=SHA256_PATTERN)


class ConfigurationDraftResultDTO(CanonicalProjectionDTO):
    profile_id: UUID
    version_id: UUID
    version_number: int
    configuration_kind: str
    lifecycle_status: Literal["DRAFT"]
    schema_digest: str = Field(pattern=SHA256_PATTERN)
    payload_digest: str = Field(pattern=SHA256_PATTERN)
    idempotency_receipt_id: UUID
    receipt_digest: str = Field(pattern=SHA256_PATTERN)
    idempotent_replay: bool


class ConfigurationValidationResultDTO(CanonicalProjectionDTO):
    snapshot_id: UUID
    version_id: UUID
    configuration_kind: str
    lifecycle_status: Literal["VALIDATED"] = "VALIDATED"
    snapshot_digest: str = Field(pattern=SHA256_PATTERN)
    dependency_digest: str = Field(pattern=SHA256_PATTERN)
    member_count: int = Field(ge=0)
    target_count: int = Field(ge=0)
    total_candidate_count: int = Field(ge=0)
    repeat_noop: bool
    idempotency_receipt_id: UUID
    receipt_digest: str = Field(pattern=SHA256_PATTERN)
    idempotent_replay: bool


class ConfigurationVersionProjectionDTO(CanonicalProjectionDTO):
    version_id: UUID
    version_number: int
    lifecycle_status: Literal["DRAFT", "VALIDATED", "RETIRED"]
    configuration_schema: dict[str, Any] = Field(alias="schema_json")
    payload_json: dict[str, Any]
    schema_digest: str = Field(pattern=SHA256_PATTERN)
    payload_digest: str = Field(pattern=SHA256_PATTERN)
    adapter_identity: str
    adapter_digest: str = Field(pattern=SHA256_PATTERN)
    snapshot_id: Optional[UUID]
    snapshot_digest: Optional[str] = Field(default=None, pattern=SHA256_PATTERN)
    created_at: datetime
    validated_at: Optional[datetime]


class ConfigurationProfileProjectionDTO(CanonicalProjectionDTO):
    profile_id: UUID
    profile_key: str
    configuration_kind: str
    scope_key: str
    workflow_key: str
    versions: list[ConfigurationVersionProjectionDTO]


class ConfigurationCatalogProjectionDTO(CanonicalProjectionDTO):
    status: Literal["UNSET", "AVAILABLE"]
    configured_kinds: list[str]
    unset_kinds: list[str]
    items: list[ConfigurationProfileProjectionDTO]


class ResearchBundlePreviewCommandDTO(CanonicalCommandDTO):
    scope_key: str = Field(min_length=1, max_length=200)
    workflow_key: str = Field(min_length=1, max_length=160)
    snapshot_ids: dict[str, UUID]
    market_snapshot_id: Optional[UUID] = None


class ResearchBundleActivateCommandDTO(CanonicalCommandDTO):
    scope_key: str = Field(min_length=1, max_length=200)
    workflow_key: str = Field(min_length=1, max_length=160)
    snapshot_ids: dict[str, UUID]
    market_snapshot_id: Optional[UUID] = None
    actor_identity: str = Field(min_length=1, max_length=160)
    expected_bundle_digest: str = Field(pattern=SHA256_PATTERN)


class ResearchBundlePreviewDTO(CanonicalProjectionDTO):
    status: Literal["READY", "BLOCKED"]
    reason_codes: list[str]
    scope_key: str
    workflow_key: str
    snapshot_ids: dict[str, UUID]
    snapshot_digests: dict[str, str]
    market_snapshot_id: Optional[UUID]
    market_snapshot_digest: Optional[str]
    target_count: int = Field(ge=0)
    total_candidate_count: int = Field(ge=0)
    capability_json: dict[str, Any]
    bundle_digest: Optional[str]
    prospective_bundle_id: Optional[UUID]


class ResearchBundleActivationDTO(CanonicalProjectionDTO):
    configuration_bundle_id: UUID
    configuration_activation_id: UUID
    bundle_digest: str = Field(pattern=SHA256_PATTERN)
    previous_bundle_id: Optional[UUID]
    repeat_noop: bool
    created_bundle: bool
    execution_side_effects: Literal[0] = 0


class MarketSnapshotSummaryDTO(CanonicalProjectionDTO):
    snapshot_id: UUID
    snapshot_digest: str = Field(pattern=SHA256_PATTERN)
    market_profile_version_id: UUID
    member_count: int = Field(ge=0)
    created_at: datetime


class MarketInventoryProjectionDTO(CanonicalProjectionDTO):
    status: Literal["MARKET_SNAPSHOT_UNSET", "AVAILABLE"]
    profile_count: int = Field(ge=0)
    validated_profile_count: int = Field(ge=0)
    artifact_count: int = Field(ge=0)
    accepted_receipt_count: int = Field(ge=0)
    snapshots: list[MarketSnapshotSummaryDTO]


class MarketSnapshotMemberProjectionDTO(CanonicalProjectionDTO):
    market_artifact_id: UUID
    artifact_digest: str = Field(pattern=SHA256_PATTERN)
    market_receipt_id: UUID
    receipt_digest: str = Field(pattern=SHA256_PATTERN)
    receipt_status: Literal["ACCEPTED", "REJECTED", "BLOCKED"]
    research_target_id: UUID
    target_key: str
    coverage_start: datetime
    coverage_end: datetime
    coverage_digest: str = Field(pattern=SHA256_PATTERN)


class MarketSnapshotProjectionDTO(CanonicalProjectionDTO):
    snapshot_id: UUID
    snapshot_digest: str = Field(pattern=SHA256_PATTERN)
    market_profile_version_id: UUID
    status: Literal["ACCEPTED", "BLOCKED"]
    reason_codes: list[str]
    members: list[MarketSnapshotMemberProjectionDTO]
    created_at: datetime


class FreshMarketPlanCommandDTO(CanonicalCommandDTO):
    target_snapshot_id: UUID
    target_snapshot_digest: str = Field(pattern=SHA256_PATTERN)
    window_snapshot_id: UUID
    window_snapshot_digest: str = Field(pattern=SHA256_PATTERN)
    target_key: str = Field(min_length=1, max_length=160)


class FreshMarketApplyCommandDTO(FreshMarketPlanCommandDTO):
    expected_plan_digest: str = Field(pattern=SHA256_PATTERN)
    profile_key: str = Field(min_length=1, max_length=160)
    scope_key: str = Field(min_length=1, max_length=200)


class FreshMarketPlanDTO(CanonicalProjectionDTO):
    status: Literal["PLANNED"] = "PLANNED"
    plan_digest: str = Field(pattern=SHA256_PATTERN)
    target_snapshot_id: UUID
    target_snapshot_digest: str = Field(pattern=SHA256_PATTERN)
    window_snapshot_id: UUID
    window_snapshot_digest: str = Field(pattern=SHA256_PATTERN)
    research_target_id: UUID
    target_key: str
    instrument: str
    pair: str
    timeframe: str
    data_kind: str
    requested_start: datetime
    requested_end: datetime
    minimum_closed_candles: int = Field(gt=0)
    warmup_closed_candles: int = Field(ge=0)
    integrity_margin_closed_candles: int = Field(ge=0)
    freshness_max_age_seconds: int = Field(gt=0)
    source: Literal["OKX_PUBLIC_MARKET_DATA_ONLY"] = "OKX_PUBLIC_MARKET_DATA_ONLY"
    offline_exchange_metadata_contract: Literal[
        "canonical-v13-okx-offline-exchange-metadata-v1"
    ] = "canonical-v13-okx-offline-exchange-metadata-v1"
    offline_exchange_adapter_identity: Literal[
        "freqtrade-2026.6-ccxt-4.5.61-okx-offline-v1"
    ] = "freqtrade-2026.6-ccxt-4.5.61-okx-offline-v1"
    credential_access: Literal["NONE"] = "NONE"
    trading_capability: Literal["TRADING_DISABLED"] = "TRADING_DISABLED"
    execution_side_effects: Literal[0] = 0


class FreshMarketReceiptDTO(CanonicalProjectionDTO):
    status: Literal["ACCEPTED"] = "ACCEPTED"
    plan_digest: str = Field(pattern=SHA256_PATTERN)
    market_profile_version_id: UUID
    artifact_id: UUID
    artifact_locator: str
    artifact_digest: str = Field(pattern=SHA256_PATTERN)
    receipt_id: UUID
    market_snapshot_id: UUID
    market_snapshot_digest: str = Field(pattern=SHA256_PATTERN)
    artifact_file_replay: bool
    database_replay: bool
    exchange_metadata_artifact_id: UUID
    exchange_metadata_receipt_id: UUID
    exchange_metadata_locator: str
    exchange_metadata_digest: str = Field(pattern=SHA256_PATTERN)
    exchange_metadata_receipt_digest: str = Field(pattern=SHA256_PATTERN)
    source: Literal["OKX_PUBLIC_MARKET_DATA_ONLY"] = "OKX_PUBLIC_MARKET_DATA_ONLY"
    credential_access: Literal["NONE"] = "NONE"
    trading_capability: Literal["TRADING_DISABLED"] = "TRADING_DISABLED"
    execution_side_effects: Literal[0] = 0


class ReadinessProjectionDTO(CanonicalProjectionDTO):
    status: Literal["READY", "BLOCKED", "PENDING_FIRST_BACKTEST"]
    reason_codes: list[str]
    scope_key: Optional[str] = None
    workflow_key: Optional[str] = None
    configuration_bundle_id: Optional[UUID] = None
    bundle_digest: Optional[str] = None
    market_snapshot_id: Optional[UUID] = None
    target_count: Optional[int] = Field(default=None, ge=0)
    total_candidate_count: Optional[int] = Field(default=None, ge=0)
    deployment_id: Optional[UUID] = None
    runtime_instance_id: Optional[UUID] = None


class OptimizationProjectionDTO(CanonicalProjectionDTO):
    optimization_run_id: UUID
    baseline_qualification_decision_id: UUID
    status: Literal[
        "NOT_STARTED", "PENDING_BASELINE", "RUNNING", "SUCCEEDED", "FAILED", "BLOCKED"
    ]
    request_digest: str = Field(pattern=SHA256_PATTERN)
    receipt_digest: Optional[str] = Field(default=None, pattern=SHA256_PATTERN)
    created_at: datetime
    completed_at: Optional[datetime]


class OptimizationListProjectionDTO(CanonicalProjectionDTO):
    status: Literal["PENDING_FIRST_BACKTEST", "AVAILABLE"]
    items: list[OptimizationProjectionDTO]


class ResearchLineageDTO(CanonicalProjectionDTO):
    strategy_version_id: UUID
    research_target_id: UUID
    configuration_bundle_id: UUID
    configuration_bundle_digest: str = Field(pattern=SHA256_PATTERN)
    market_snapshot_id: UUID
    market_snapshot_digest: str = Field(pattern=SHA256_PATTERN)


class LookaheadReceiptCommandDTO(CanonicalCommandDTO):
    artifact_digest: str = Field(pattern=SHA256_PATTERN)
    analyzer_identity: str = Field(min_length=1, max_length=200)
    analyzer_digest: str = Field(pattern=SHA256_PATTERN)
    evidence_digest: str = Field(pattern=SHA256_PATTERN)
    status: Literal["PASSED", "FAILED", "BLOCKED"]
    has_bias: Optional[bool]
    observed_signal_count: int = Field(ge=0)
    blocked_reason_code: Optional[str] = Field(default=None, min_length=1, max_length=120)
    blocked_observed_trade_count: Optional[int] = Field(default=None, ge=0)
    blocked_required_trade_count: Optional[int] = Field(default=None, gt=0)
    request_digest: str = Field(pattern=SHA256_PATTERN)
    receipt_digest: str = Field(pattern=SHA256_PATTERN)


class ValidationPlanCommandDTO(CanonicalCommandDTO):
    lineage: ResearchLineageDTO
    static_validator_identity: str = Field(min_length=1, max_length=200)
    static_validator_digest: str = Field(pattern=SHA256_PATTERN)
    lookahead_receipt: LookaheadReceiptCommandDTO
    orchestrator_identity: str = Field(min_length=1, max_length=200)


class ValidationPlanReceiptDTO(CanonicalProjectionDTO):
    validation_plan_id: UUID
    validation_plan_digest: str = Field(pattern=SHA256_PATTERN)
    status: Literal["READY"]
    window_count: int = Field(gt=0)
    required_window_count: int = Field(gt=0)
    static_receipt_digest: str = Field(pattern=SHA256_PATTERN)
    lookahead_receipt_digest: str = Field(pattern=SHA256_PATTERN)
    repeat_noop: bool


class ResearchAuthorizationCommandDTO(CanonicalCommandDTO):
    attempt_id: UUID
    lineage: ResearchLineageDTO
    validation_plan_id: UUID
    validation_plan_digest: str = Field(pattern=SHA256_PATTERN)
    actor_identity: str = Field(min_length=1, max_length=160)
    purpose: str = Field(min_length=1, max_length=240)
    ttl_seconds: int = Field(ge=1, le=900)


class ResearchAuthorizationReceiptDTO(CanonicalProjectionDTO):
    authorization_id: UUID
    attempt_id: UUID
    validation_plan_id: UUID
    validation_plan_digest: str = Field(pattern=SHA256_PATTERN)
    actor_identity: str
    purpose: str
    request_digest: str = Field(pattern=SHA256_PATTERN)
    receipt_digest: str = Field(pattern=SHA256_PATTERN)
    authorized_at: datetime
    expires_at: datetime
    one_shot: Literal[True]
    environment_class: Literal["PRODUCTION_RESEARCH"]


class ResearchAuthorizationConsumeCommandDTO(CanonicalCommandDTO):
    attempt_id: UUID
    lineage: ResearchLineageDTO
    validation_plan_id: UUID
    validation_plan_digest: str = Field(pattern=SHA256_PATTERN)
    actor_identity: str = Field(min_length=1, max_length=160)


class ResearchAuthorizationConsumptionReceiptDTO(CanonicalProjectionDTO):
    authorization_id: UUID
    consumption_id: UUID
    attempt_id: UUID
    lineage: ResearchLineageDTO
    validation_plan_id: UUID
    validation_plan_digest: str = Field(pattern=SHA256_PATTERN)
    actor_identity: str
    authorization_receipt_digest: str = Field(pattern=SHA256_PATTERN)
    request_digest: str = Field(pattern=SHA256_PATTERN)
    receipt_digest: str = Field(pattern=SHA256_PATTERN)
    consumed_at: datetime
    environment_class: Literal["PRODUCTION_RESEARCH"]


class ResearchAuthorizationRevokeCommandDTO(CanonicalCommandDTO):
    actor_identity: str = Field(min_length=1, max_length=160)
    reason: str = Field(min_length=1, max_length=240)


class ResearchAuthorizationRevokeReceiptDTO(CanonicalProjectionDTO):
    authorization_id: UUID
    revocation_event_id: UUID
    status: Literal["REVOKED"] = "REVOKED"


class ResearchAttemptStartCommandDTO(CanonicalCommandDTO):
    validation_plan_id: UUID
    validation_plan_digest: str = Field(pattern=SHA256_PATTERN)
    executor_identity: str = Field(min_length=1, max_length=200)
    executor_image_digest: str = Field(pattern=SHA256_PATTERN)
    authorization_consumption: ResearchAuthorizationConsumptionReceiptDTO


class ResearchAttemptStartReceiptDTO(CanonicalProjectionDTO):
    validation_attempt_id: UUID
    validation_plan_id: UUID
    attempt_number: int = Field(gt=0)
    status: Literal["RUNNING"]
    request_digest: str = Field(pattern=SHA256_PATTERN)
    environment_class: Literal["PRODUCTION_RESEARCH"] = "PRODUCTION_RESEARCH"


class ResearchScoreCommandDTO(CanonicalCommandDTO):
    validation_plan_id: UUID
    validation_attempt_id: UUID
    scorer_identity: str = Field(min_length=1, max_length=200)


class ResearchScoreReceiptDTO(CanonicalProjectionDTO):
    contract: Literal["canonical-v13-scoring-receipt-v1"]
    target_score_id: UUID
    validation_plan_id: UUID
    validation_attempt_id: UUID
    scoring_snapshot_id: UUID
    overall_score: str
    required_window_result_set_digest: str = Field(pattern=SHA256_PATTERN)
    score_digest: str = Field(pattern=SHA256_PATTERN)
    required_window_count: int = Field(gt=0)
    repeat_noop: bool


class ResearchQualificationCommandDTO(CanonicalCommandDTO):
    validation_plan_id: UUID
    validation_attempt_id: UUID
    qualifier_identity: str = Field(min_length=1, max_length=200)


class ResearchQualificationReceiptDTO(CanonicalProjectionDTO):
    contract: Literal["canonical-v13-qualification-receipt-v1"]
    qualification_decision_id: UUID
    target_score_id: UUID
    validation_plan_id: UUID
    validation_attempt_id: UUID
    quality_snapshot_id: UUID
    status: Literal["QUALIFIED", "REJECTED", "BLOCKED", "FAILED"]
    reason_code: str
    decision_digest: str = Field(pattern=SHA256_PATTERN)
    evidence_count: int = Field(gt=0)
    repeat_noop: bool


class ResearchChainProjectionDTO(CanonicalProjectionDTO):
    validation_plan_id: UUID
    validation_plan_digest: str = Field(pattern=SHA256_PATTERN)
    strategy_version_id: UUID
    research_target_id: UUID
    target_key: str
    plan_status: Literal["DECLARED", "READY", "RUNNING", "COMPLETE", "FAILED", "BLOCKED"]
    validation_attempt_id: Optional[UUID]
    attempt_status: Optional[Literal["PENDING", "RUNNING", "SUCCEEDED", "FAILED", "BLOCKED"]]
    attempt_receipt_digest: Optional[str] = Field(default=None, pattern=SHA256_PATTERN)
    target_score_id: Optional[UUID]
    overall_score: Optional[str]
    score_digest: Optional[str] = Field(default=None, pattern=SHA256_PATTERN)
    qualification_decision_id: Optional[UUID]
    qualification_status: Optional[Literal["QUALIFIED", "REJECTED", "BLOCKED", "FAILED"]]
    qualification_reason_code: Optional[str]
    qualification_decision_digest: Optional[str] = Field(
        default=None, pattern=SHA256_PATTERN
    )


__all__ = [name for name in tuple(globals()) if name.endswith("DTO")]
