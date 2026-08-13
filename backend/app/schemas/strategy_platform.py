from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


def _non_empty_trimmed(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("value must contain non-whitespace characters")
    return normalized


class ConfigurationTypeRead(BaseModel):
    type_key: str
    name_zh: str
    description_zh: str
    schema_version: str
    handler_key: str
    editor_capability: dict[str, Any]
    enabled: bool


class ConfigurationCatalogRead(BaseModel):
    schema_version: str = "strategy-platform-v1.3-read-v1"
    items: list[ConfigurationTypeRead]


class ConfigurationVersionRead(BaseModel):
    id: int
    type_key: str
    version_number: int
    lifecycle_status: str
    payload_json: dict[str, Any]
    schema_version: str
    config_digest: str
    change_summary: Optional[str]
    created_by: str
    created_at: datetime
    validated_at: Optional[datetime]

    model_config = {"from_attributes": True}


class ConfigurationVersionListRead(BaseModel):
    config_type: str
    scope_type: str
    scope_key: str
    active_version_id: Optional[int]
    items: list[ConfigurationVersionRead]


class ConfigurationDependencyWrite(BaseModel):
    depends_on_version_id: int = Field(gt=0)
    relation_key: str = Field(min_length=1, max_length=120)

    _normalize_relation_key = field_validator("relation_key")(_non_empty_trimmed)


class ConfigurationDraftCreateRequest(BaseModel):
    scope_type: str = Field(min_length=1, max_length=80)
    scope_key: str = Field(min_length=1, max_length=160)
    change_summary: str = Field(min_length=1, max_length=4000)
    source_version_id: Optional[int] = Field(default=None, gt=0)
    payload_json: Optional[dict[str, Any]] = None
    dependencies: Optional[list[ConfigurationDependencyWrite]] = None

    _normalize_scope = field_validator("scope_type", "scope_key")(_non_empty_trimmed)
    _normalize_summary = field_validator("change_summary")(_non_empty_trimmed)


class ConfigurationVersionActionRequest(BaseModel):
    scope_type: str = Field(min_length=1, max_length=80)
    scope_key: str = Field(min_length=1, max_length=160)
    reason: Optional[str] = Field(default=None, max_length=4000)

    _normalize_scope = field_validator("scope_type", "scope_key")(_non_empty_trimmed)


class ConfigurationAuditEventRead(BaseModel):
    id: int
    configuration_version_id: int
    event_type: str
    actor: str
    request_id: str
    scope_type: Optional[str]
    scope_key: Optional[str]
    reason: Optional[str]
    event_snapshot: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class ConfigurationAuditEventListRead(BaseModel):
    config_type: str
    scope_type: str
    scope_key: str
    items: list[ConfigurationAuditEventRead]


class ConfigurationDiffEntryRead(BaseModel):
    path: str
    before: Any = None
    after: Any = None


class ConfigurationVersionDiffRead(BaseModel):
    config_type: str
    scope_type: str
    scope_key: str
    from_version_id: int
    to_version_id: int
    items: list[ConfigurationDiffEntryRead]


class ActiveConfigurationRead(BaseModel):
    scope_type: str
    scope_key: str
    activated_at: datetime
    activated_by: str
    configuration_type: ConfigurationTypeRead
    version: ConfigurationVersionRead


class ConfigurationDependencyRead(BaseModel):
    configuration_version_id: int
    configuration_type: str
    depends_on_version_id: int
    depends_on_type: str
    relation_key: str


class ConfigurationBundleResolveRequest(BaseModel):
    workflow_kind: str = Field(min_length=1, max_length=80)
    aggregate_config_type: str = Field(min_length=1, max_length=120)
    scope_type: str = Field(min_length=1, max_length=80)
    scope_key: str = Field(min_length=1, max_length=160)


class ConfigurationBundleResolutionRead(BaseModel):
    schema_version: str = "configuration-bundle-resolution-v1"
    persisted: bool
    snapshot_id: Optional[int] = None
    workflow_kind: str
    scope_type: str
    scope_key: str
    aggregate_profile_version_id: int
    resolved_versions: list[ConfigurationVersionRead]
    dependencies: list[ConfigurationDependencyRead]
    resolved_versions_json: dict[str, int]
    resolved_digests_json: dict[str, str]
    bundle_digest: str
    capability_snapshot: dict[str, Any]


class ConfigurationBundleSnapshotRead(ConfigurationBundleResolutionRead):
    persisted: bool = True
    snapshot_id: int
    created_at: datetime


class ConfigurationDependencyListRead(BaseModel):
    items: list[ConfigurationDependencyRead]


class ConfigurationVersionDetailRead(BaseModel):
    version: ConfigurationVersionRead
    dependencies: list[ConfigurationDependencyRead]


class ConfigurationWriteResult(BaseModel):
    schema_version: str = "configuration-management-v1"
    request_id: str
    idempotent_replay: bool = False
    operation: str
    scope_type: str
    scope_key: str
    version: ConfigurationVersionRead
    dependencies: list[ConfigurationDependencyRead] = Field(default_factory=list)
    previous_active_version_id: Optional[int] = None
    active_version_id: Optional[int] = None
    validation_bundle: Optional[ConfigurationBundleResolutionRead] = None


class ConfigurationBundleSnapshotListRead(BaseModel):
    scope_type: str
    scope_key: str
    items: list[ConfigurationBundleSnapshotRead]


class StrategyCatalogCurrentVersionRead(BaseModel):
    id: int
    version_number: int
    static_validation_status: str
    created_at: datetime


class StrategyTargetProjectionRead(BaseModel):
    id: int
    strategy_version_id: int
    execution_target_id: int
    execution_target_key: str
    instrument_id: str
    pair: str
    timeframe: str
    status: str
    validation_priority: int
    latest_validation_plan_id: Optional[int] = None
    research_status: str
    last_completed_validation_at: Optional[datetime]
    next_validation_not_before: Optional[datetime]
    created_at: datetime
    updated_at: datetime


class StrategyCatalogItemRead(BaseModel):
    id: int
    name: str
    slug: str
    description: Optional[str]
    source: str
    tags: list[str]
    catalog_status: str
    current_version: Optional[StrategyCatalogCurrentVersionRead]
    targets: list[StrategyTargetProjectionRead]
    target_count: int
    created_at: datetime
    updated_at: datetime


class StrategyCatalogPageRead(BaseModel):
    schema_version: str = "strategy-catalog-v1.3-read-v1"
    items: list[StrategyCatalogItemRead]
    next_cursor: Optional[str]


class ValidationFailureReasonRead(BaseModel):
    code: str
    message: Optional[str] = None
    quality_gate_rule_id: Optional[int] = None
    actual_value: Optional[float] = None
    operator: Optional[str] = None
    threshold_snapshot: Optional[dict[str, Any]] = None


class DynamicValidationWindowRead(BaseModel):
    id: int
    window_config_id: Optional[int]
    window_key: Optional[str]
    ordinal: int
    attempt_number: int
    name_zh: Optional[str]
    description_zh: Optional[str]
    projection_status: str
    score: Optional[float]
    status: str
    net_profit_after_cost: Optional[float]
    max_drawdown: Optional[float]
    volatility: Optional[float]
    total_trades: Optional[int]
    failure_reasons: list[ValidationFailureReasonRead]


class StrategyValidationCycleRead(BaseModel):
    id: int
    strategy_version_id: int
    strategy_target_id: Optional[int]
    target: Optional[StrategyTargetProjectionRead]
    cycle_number: Optional[int]
    status: str
    required_window_count: Optional[int]
    passed_window_count: Optional[int]
    failed_window_count: Optional[int]
    overall_score: Optional[float]
    reason_codes: list[str]
    configuration_bundle_snapshot_id: Optional[int]
    validation_window_config_set_id: Optional[int]
    created_at: datetime
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    windows: list[DynamicValidationWindowRead]


class StrategyValidationHistoryRead(BaseModel):
    schema_version: str = "strategy-validation-history-v1.3-read-v1"
    strategy_id: int
    cycles: list[StrategyValidationCycleRead]
