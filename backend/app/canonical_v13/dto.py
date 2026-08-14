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


__all__ = [name for name in tuple(globals()) if name.endswith("DTO")]
