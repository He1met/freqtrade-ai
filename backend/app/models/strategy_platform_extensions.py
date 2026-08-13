"""Additive Strategy Platform V1.3 persistence contracts.

The tables in this module are deliberately metadata-only.  They describe
versioned policy, registered capabilities, and durable audit facts; they do not
store credentials, arbitrary executable code, commands, or runtime secrets.
"""

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base


def _bigint_type():
    return BigInteger().with_variant(Integer, "sqlite")


class AdapterDefinition(Base):
    """Registry metadata for one installed, non-user-programmable adapter."""

    __tablename__ = "adapter_definitions"
    __table_args__ = (
        CheckConstraint(
            "length(adapter_key) > 0 AND length(adapter_kind) > 0 "
            "AND length(implementation_version) > 0 "
            "AND length(input_schema_version) > 0",
            name="adapter_definitions_identity_check",
        ),
        CheckConstraint(
            "registry_metadata_only = TRUE "
            "AND contains_secret_material = FALSE "
            "AND contains_executable_payload = FALSE",
            name="adapter_definitions_safe_metadata_check",
        ),
        Index("adapter_definitions_kind_enabled_idx", "adapter_kind", "enabled"),
    )

    adapter_key: Mapped[str] = mapped_column(String(160), primary_key=True)
    adapter_kind: Mapped[str] = mapped_column(String(120), nullable=False)
    implementation_version: Mapped[str] = mapped_column(String(80), nullable=False)
    input_schema_version: Mapped[str] = mapped_column(String(80), nullable=False)
    output_schema_version: Mapped[Optional[str]] = mapped_column(String(80))
    capabilities: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    display_metadata: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    registry_metadata_only: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    contains_secret_material: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    contains_executable_payload: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class StrategySourceDefinition(Base):
    __tablename__ = "strategy_source_definitions"

    id: Mapped[int] = mapped_column(
        _bigint_type(), primary_key=True, autoincrement=True
    )
    source_key: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class StrategySourceDefinitionVersion(Base):
    __tablename__ = "strategy_source_definition_versions"
    __table_args__ = (
        UniqueConstraint(
            "strategy_source_definition_id",
            "configuration_version_id",
            name="strategy_source_definition_versions_identity_unique",
        ),
        CheckConstraint(
            "metadata_only = TRUE AND executable_payload_allowed = FALSE",
            name="strategy_source_definition_versions_safe_metadata_check",
        ),
    )

    configuration_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("configuration_versions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    strategy_source_definition_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("strategy_source_definitions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    name_zh: Mapped[str] = mapped_column(String(160), nullable=False)
    description_zh: Mapped[str] = mapped_column(Text, nullable=False)
    allows_external_submission: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    required_audit_fields: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list
    )
    display_metadata: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    metadata_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    executable_payload_allowed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )


class TriggerSourceDefinition(Base):
    __tablename__ = "trigger_source_definitions"

    id: Mapped[int] = mapped_column(
        _bigint_type(), primary_key=True, autoincrement=True
    )
    trigger_key: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TriggerSourceDefinitionVersion(Base):
    __tablename__ = "trigger_source_definition_versions"
    __table_args__ = (
        UniqueConstraint(
            "trigger_source_definition_id",
            "configuration_version_id",
            name="trigger_source_definition_versions_identity_unique",
        ),
        CheckConstraint(
            "metadata_only = TRUE AND executable_payload_allowed = FALSE",
            name="trigger_source_definition_versions_safe_metadata_check",
        ),
    )

    configuration_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("configuration_versions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    trigger_source_definition_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("trigger_source_definitions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    name_zh: Mapped[str] = mapped_column(String(160), nullable=False)
    description_zh: Mapped[str] = mapped_column(Text, nullable=False)
    required_audit_fields: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list
    )
    display_metadata: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    metadata_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    executable_payload_allowed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )


class TimeframeDefinition(Base):
    __tablename__ = "timeframe_definitions"

    id: Mapped[int] = mapped_column(
        _bigint_type(), primary_key=True, autoincrement=True
    )
    timeframe_key: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TimeframeDefinitionVersion(Base):
    __tablename__ = "timeframe_definition_versions"
    __table_args__ = (
        UniqueConstraint(
            "timeframe_definition_id",
            "configuration_version_id",
            name="timeframe_definition_versions_identity_unique",
        ),
        CheckConstraint(
            "duration_seconds > 0",
            name="timeframe_definition_versions_duration_check",
        ),
        Index(
            "timeframe_definition_versions_order_idx",
            "enabled",
            "sort_order",
        ),
    )

    configuration_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("configuration_versions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    timeframe_definition_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("timeframe_definitions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    name_zh: Mapped[str] = mapped_column(String(160), nullable=False)
    description_zh: Mapped[Optional[str]] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)
    display_metadata: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class ResearchTargetConfigSet(Base):
    __tablename__ = "research_target_config_sets"

    id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("configuration_versions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)


class ResearchTargetConfig(Base):
    __tablename__ = "research_target_configs"
    __table_args__ = (
        CheckConstraint(
            "priority >= 0 AND max_data_age_seconds > 0",
            name="research_target_configs_limits_check",
        ),
        UniqueConstraint(
            "config_set_id",
            "exchange",
            "pair",
            "instrument_id",
            "timeframe",
            "data_kind",
            name="research_target_configs_identity_unique",
        ),
        Index(
            "research_target_configs_queue_idx",
            "config_set_id",
            "enabled",
            "priority",
        ),
    )

    id: Mapped[int] = mapped_column(
        _bigint_type(), primary_key=True, autoincrement=True
    )
    config_set_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("research_target_config_sets.id", ondelete="RESTRICT"),
        nullable=False,
    )
    exchange: Mapped[str] = mapped_column(String(80), nullable=False)
    pair: Mapped[str] = mapped_column(String(120), nullable=False)
    instrument_id: Mapped[str] = mapped_column(String(120), nullable=False)
    timeframe: Mapped[str] = mapped_column(
        String(40),
        ForeignKey("timeframe_definitions.timeframe_key", ondelete="RESTRICT"),
        nullable=False,
    )
    data_kind: Mapped[str] = mapped_column(String(80), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    max_data_age_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    display_metadata: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class StrategyFamilyDefinition(Base):
    __tablename__ = "strategy_family_definitions"

    id: Mapped[int] = mapped_column(
        _bigint_type(), primary_key=True, autoincrement=True
    )
    family_key: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class StrategyFamilyDefinitionVersion(Base):
    __tablename__ = "strategy_family_definition_versions"
    __table_args__ = (
        UniqueConstraint(
            "strategy_family_definition_id",
            "configuration_version_id",
            name="strategy_family_definition_versions_identity_unique",
        ),
    )

    configuration_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("configuration_versions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    strategy_family_definition_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("strategy_family_definitions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    name_zh: Mapped[str] = mapped_column(String(160), nullable=False)
    description_zh: Mapped[str] = mapped_column(Text, nullable=False)
    display_metadata: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class ProviderModelConfigVersion(Base):
    __tablename__ = "provider_model_config_versions"
    __table_args__ = (
        CheckConstraint(
            "timeout_seconds > 0 AND max_output_tokens > 0",
            name="provider_model_config_versions_limits_check",
        ),
        CheckConstraint(
            "credential_reference_kind IN ('NONE', 'REFERENCE_NAME')",
            name="provider_model_config_versions_credential_kind_check",
        ),
        CheckConstraint(
            "(credential_reference_kind = 'NONE' "
            "AND credential_reference_name IS NULL) OR "
            "(credential_reference_kind = 'REFERENCE_NAME' "
            "AND credential_reference_name IS NOT NULL "
            "AND length(credential_reference_name) > 0)",
            name="provider_model_config_versions_credential_reference_check",
        ),
        CheckConstraint(
            "secret_material_present = FALSE "
            "AND executable_payload_present = FALSE",
            name="provider_model_config_versions_safe_payload_check",
        ),
        UniqueConstraint(
            "provider_key",
            "model_key",
            "configuration_version_id",
            name="provider_model_config_versions_identity_unique",
        ),
    )

    configuration_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("configuration_versions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    provider_adapter_key: Mapped[str] = mapped_column(
        String(160),
        ForeignKey("adapter_definitions.adapter_key", ondelete="RESTRICT"),
        nullable=False,
    )
    provider_key: Mapped[str] = mapped_column(String(120), nullable=False)
    model_key: Mapped[str] = mapped_column(String(160), nullable=False)
    capabilities: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    max_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    generation_limits: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    credential_reference_kind: Mapped[str] = mapped_column(
        String(24), nullable=False, default="NONE"
    )
    credential_reference_name: Mapped[Optional[str]] = mapped_column(String(200))
    secret_material_present: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    executable_payload_present: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class GenerationProfileVersion(Base):
    __tablename__ = "generation_profile_versions"
    __table_args__ = (
        CheckConstraint(
            "candidates_per_target > 0 AND structure_slot_count > 0",
            name="generation_profile_versions_counts_check",
        ),
    )

    configuration_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("configuration_versions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    provider_model_config_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey(
            "provider_model_config_versions.configuration_version_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    candidates_per_target: Mapped[int] = mapped_column(Integer, nullable=False)
    structure_slot_count: Mapped[int] = mapped_column(Integer, nullable=False)
    model_selection_policy: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict
    )
    generation_limits: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    blueprint_requirements: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict
    )


class GenerationProfileFamily(Base):
    __tablename__ = "generation_profile_families"
    __table_args__ = (
        CheckConstraint(
            "ordinal >= 0 AND (allocation_count IS NULL OR allocation_count > 0)",
            name="generation_profile_families_allocation_check",
        ),
        UniqueConstraint(
            "generation_profile_version_id",
            "strategy_family_definition_version_id",
            name="generation_profile_families_identity_unique",
        ),
        UniqueConstraint(
            "generation_profile_version_id",
            "ordinal",
            name="generation_profile_families_ordinal_unique",
        ),
    )

    id: Mapped[int] = mapped_column(
        _bigint_type(), primary_key=True, autoincrement=True
    )
    generation_profile_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey(
            "generation_profile_versions.configuration_version_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    strategy_family_definition_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey(
            "strategy_family_definition_versions.configuration_version_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    allocation_count: Mapped[Optional[int]] = mapped_column(Integer)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class ScoringProfileVersion(Base):
    __tablename__ = "scoring_profile_versions"

    configuration_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("configuration_versions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    scoring_adapter_key: Mapped[str] = mapped_column(
        String(160),
        ForeignKey("adapter_definitions.adapter_key", ondelete="RESTRICT"),
        nullable=False,
    )
    algorithm_version: Mapped[str] = mapped_column(String(80), nullable=False)
    aggregation_method: Mapped[str] = mapped_column(String(120), nullable=False)
    primary_window_selector: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict
    )
    score_bounds: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class ScoringRule(Base):
    __tablename__ = "scoring_rules"
    __table_args__ = (
        CheckConstraint(
            "weight >= 0 AND priority >= 0",
            name="scoring_rules_weight_priority_check",
        ),
        UniqueConstraint(
            "profile_version_id",
            "metric_definition_version_id",
            "priority",
            name="scoring_rules_metric_priority_unique",
        ),
        Index(
            "scoring_rules_profile_order_idx", "profile_version_id", "priority", "id"
        ),
    )

    id: Mapped[int] = mapped_column(
        _bigint_type(), primary_key=True, autoincrement=True
    )
    profile_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey(
            "scoring_profile_versions.configuration_version_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    metric_definition_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey(
            "metric_definition_versions.configuration_version_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    normalization_adapter_key: Mapped[str] = mapped_column(
        String(160),
        ForeignKey("adapter_definitions.adapter_key", ondelete="RESTRICT"),
        nullable=False,
    )
    normalization_parameters: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict
    )
    weight: Mapped[Decimal] = mapped_column(Numeric(24, 12), nullable=False)
    data_source: Mapped[str] = mapped_column(String(160), nullable=False)
    aggregation_method: Mapped[str] = mapped_column(String(120), nullable=False)
    window_selector: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)


class DiversityProfileVersion(Base):
    __tablename__ = "diversity_profile_versions"

    configuration_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("configuration_versions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    evaluation_adapter_key: Mapped[str] = mapped_column(
        String(160),
        ForeignKey("adapter_definitions.adapter_key", ondelete="RESTRICT"),
        nullable=False,
    )
    evaluation_scope: Mapped[str] = mapped_column(String(120), nullable=False)
    parameters: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class DiversityRule(Base):
    __tablename__ = "diversity_rules"
    __table_args__ = (
        CheckConstraint(
            "threshold_value >= 0 AND priority >= 0",
            name="diversity_rules_threshold_priority_check",
        ),
        CheckConstraint(
            "severity IN ('BLOCKING', 'WARNING')",
            name="diversity_rules_severity_check",
        ),
        UniqueConstraint(
            "profile_version_id",
            "metric_key",
            "priority",
            name="diversity_rules_metric_priority_unique",
        ),
    )

    id: Mapped[int] = mapped_column(
        _bigint_type(), primary_key=True, autoincrement=True
    )
    profile_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey(
            "diversity_profile_versions.configuration_version_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    metric_key: Mapped[str] = mapped_column(String(120), nullable=False)
    comparison_adapter_key: Mapped[str] = mapped_column(
        String(160),
        ForeignKey("adapter_definitions.adapter_key", ondelete="RESTRICT"),
        nullable=False,
    )
    parameters: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    threshold_value: Mapped[Decimal] = mapped_column(Numeric(24, 12), nullable=False)
    unit: Mapped[str] = mapped_column(String(40), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)


class WorkerExecutionProfileVersion(Base):
    __tablename__ = "worker_execution_profile_versions"
    __table_args__ = (
        CheckConstraint(
            "concurrency_limit > 0 AND batch_size > 0 "
            "AND lease_seconds > 0 AND heartbeat_seconds > 0 "
            "AND timeout_seconds > 0 AND max_retries >= 0 "
            "AND backoff_seconds >= 0",
            name="worker_execution_profile_versions_limits_check",
        ),
        CheckConstraint(
            "heartbeat_seconds < lease_seconds",
            name="worker_execution_profile_versions_heartbeat_check",
        ),
    )

    configuration_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("configuration_versions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    concurrency_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    batch_size: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    heartbeat_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False)
    backoff_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    resource_limits: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class SchedulerProfileVersion(Base):
    __tablename__ = "scheduler_profile_versions"
    __table_args__ = (
        CheckConstraint(
            "schedule_kind IN ('CRON', 'INTERVAL', 'DISABLED')",
            name="scheduler_profile_versions_kind_check",
        ),
        CheckConstraint(
            "jitter_seconds >= 0 "
            "AND revalidation_interval_seconds >= 0 "
            "AND (interval_seconds IS NULL OR interval_seconds > 0)",
            name="scheduler_profile_versions_intervals_check",
        ),
        CheckConstraint(
            "(schedule_kind = 'CRON' AND cron_expression IS NOT NULL "
            "AND interval_seconds IS NULL) OR "
            "(schedule_kind = 'INTERVAL' AND cron_expression IS NULL "
            "AND interval_seconds IS NOT NULL) OR "
            "(schedule_kind = 'DISABLED' AND cron_expression IS NULL "
            "AND interval_seconds IS NULL)",
            name="scheduler_profile_versions_schedule_shape_check",
        ),
    )

    configuration_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("configuration_versions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    schedule_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    cron_expression: Mapped[Optional[str]] = mapped_column(String(160))
    interval_seconds: Mapped[Optional[int]] = mapped_column(Integer)
    timezone: Mapped[str] = mapped_column(String(80), nullable=False)
    jitter_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    catch_up_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    revalidation_interval_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    claim_ordering: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class MarketDataPolicyVersion(Base):
    __tablename__ = "market_data_policy_versions"
    __table_args__ = (
        CheckConstraint(
            "max_data_age_seconds > 0",
            name="market_data_policy_versions_limits_check",
        ),
        CheckConstraint(
            "(overlap_by_timeframe IS NULL "
            "AND incremental_overlap_seconds IS NOT NULL "
            "AND incremental_overlap_seconds >= 0) OR "
            "(overlap_by_timeframe IS NOT NULL "
            "AND incremental_overlap_seconds IS NULL "
            "AND length(CAST(overlap_by_timeframe AS TEXT)) > 2)",
            name="market_data_policy_versions_overlap_shape_check",
        ),
        CheckConstraint(
            "closed_candles_only = TRUE",
            name="market_data_policy_versions_closed_candles_check",
        ),
    )

    configuration_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("configuration_versions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    downloader_adapter_key: Mapped[str] = mapped_column(
        String(160),
        ForeignKey("adapter_definitions.adapter_key", ondelete="RESTRICT"),
        nullable=False,
    )
    timeframe_capability: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict
    )
    # New profiles use the explicit per-timeframe candle counts.  The scalar
    # seconds column remains nullable solely so an already-installed legacy
    # profile can be represented without rewriting immutable configuration.
    overlap_by_timeframe: Mapped[Optional[dict]] = mapped_column(JSON)
    incremental_overlap_seconds: Mapped[Optional[int]] = mapped_column(Integer)
    repair_gaps: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    max_data_age_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    closed_candles_only: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    resource_limits: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class EvidenceFreshnessProfileVersion(Base):
    __tablename__ = "evidence_freshness_profile_versions"

    configuration_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("configuration_versions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    fail_closed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class EvidenceFreshnessRule(Base):
    __tablename__ = "evidence_freshness_rules"
    __table_args__ = (
        CheckConstraint(
            "max_age_seconds > 0 AND future_skew_seconds >= 0 "
            "AND renewal_lead_seconds >= 0 "
            "AND renewal_lead_seconds < max_age_seconds",
            name="evidence_freshness_rules_limits_check",
        ),
        UniqueConstraint(
            "profile_version_id",
            "evidence_kind",
            name="evidence_freshness_rules_kind_unique",
        ),
    )

    id: Mapped[int] = mapped_column(
        _bigint_type(), primary_key=True, autoincrement=True
    )
    profile_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey(
            "evidence_freshness_profile_versions.configuration_version_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    evidence_kind: Mapped[str] = mapped_column(String(120), nullable=False)
    max_age_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    future_skew_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    renewal_lead_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    expired_reason_code: Mapped[str] = mapped_column(String(160), nullable=False)


class MonitoringProfileVersion(Base):
    __tablename__ = "monitoring_profile_versions"
    __table_args__ = (
        CheckConstraint(
            "heartbeat_ttl_seconds > 0 AND read_cache_ttl_seconds >= 0 "
            "AND soak_seconds > 0 AND probe_interval_seconds > 0 "
            "AND max_probe_gap_seconds >= probe_interval_seconds "
            "AND retention_seconds > 0",
            name="monitoring_profile_versions_intervals_check",
        ),
    )

    configuration_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("configuration_versions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    heartbeat_ttl_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    read_cache_ttl_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    soak_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    probe_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    max_probe_gap_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    retention_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    alert_thresholds: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class PromotionProfileVersion(Base):
    __tablename__ = "promotion_profile_versions"
    __table_args__ = (
        CheckConstraint(
            "minimum_market_regime_count >= 0 "
            "AND required_approval_count >= 0",
            name="promotion_profile_versions_counts_check",
        ),
    )

    configuration_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("configuration_versions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    quality_gate_profile_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey(
            "quality_gate_profile_versions.configuration_version_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    minimum_market_regime_count: Mapped[int] = mapped_column(Integer, nullable=False)
    required_approval_count: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_chain_requirements: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict
    )
    approval_policy: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class PromotionRule(Base):
    __tablename__ = "promotion_rules"
    __table_args__ = (
        CheckConstraint(
            "priority >= 0 AND severity IN ('BLOCKING', 'WARNING')",
            name="promotion_rules_priority_severity_check",
        ),
        UniqueConstraint(
            "profile_version_id",
            "rule_key",
            name="promotion_rules_key_unique",
        ),
    )

    id: Mapped[int] = mapped_column(
        _bigint_type(), primary_key=True, autoincrement=True
    )
    profile_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey(
            "promotion_profile_versions.configuration_version_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    rule_key: Mapped[str] = mapped_column(String(120), nullable=False)
    metric_definition_version_id: Mapped[Optional[int]] = mapped_column(
        _bigint_type(),
        ForeignKey(
            "metric_definition_versions.configuration_version_id",
            ondelete="RESTRICT",
        ),
    )
    evaluation_adapter_key: Mapped[str] = mapped_column(
        String(160),
        ForeignKey("adapter_definitions.adapter_key", ondelete="RESTRICT"),
        nullable=False,
    )
    parameters: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    threshold_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(24, 12))
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)


class RiskProfileVersion(Base):
    __tablename__ = "risk_profile_versions"
    __table_args__ = (
        CheckConstraint(
            "stake_amount > 0 AND leverage > 0 AND max_open_positions > 0",
            name="risk_profile_versions_limits_check",
        ),
        CheckConstraint(
            "fail_closed = TRUE AND allow_real_funds = FALSE",
            name="risk_profile_versions_safety_check",
        ),
    )

    configuration_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("configuration_versions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    stake_amount: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    stake_currency: Mapped[str] = mapped_column(String(40), nullable=False)
    leverage: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    max_open_positions: Mapped[int] = mapped_column(Integer, nullable=False)
    fail_closed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    allow_real_funds: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )


class RiskRule(Base):
    __tablename__ = "risk_rules"
    __table_args__ = (
        CheckConstraint(
            "priority >= 0 AND severity IN ('BLOCKING', 'WARNING')",
            name="risk_rules_priority_severity_check",
        ),
        UniqueConstraint(
            "profile_version_id", "rule_key", name="risk_rules_key_unique"
        ),
    )

    id: Mapped[int] = mapped_column(
        _bigint_type(), primary_key=True, autoincrement=True
    )
    profile_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey(
            "risk_profile_versions.configuration_version_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    rule_key: Mapped[str] = mapped_column(String(120), nullable=False)
    scope_selector: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    evaluation_adapter_key: Mapped[str] = mapped_column(
        String(160),
        ForeignKey("adapter_definitions.adapter_key", ondelete="RESTRICT"),
        nullable=False,
    )
    parameters: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    threshold_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(36, 18))
    threshold_max: Mapped[Optional[Decimal]] = mapped_column(Numeric(36, 18))
    unit: Mapped[str] = mapped_column(String(40), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)


class CapacityProfileVersion(Base):
    __tablename__ = "capacity_profile_versions"
    __table_args__ = (
        CheckConstraint(
            "max_active_instances > 0 AND max_instances_per_instrument > 0 "
            "AND slot_min > 0 AND slot_max >= slot_min",
            name="capacity_profile_versions_limits_check",
        ),
    )

    configuration_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("configuration_versions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    max_active_instances: Mapped[int] = mapped_column(Integer, nullable=False)
    max_instances_per_instrument: Mapped[int] = mapped_column(Integer, nullable=False)
    slot_min: Mapped[int] = mapped_column(Integer, nullable=False)
    slot_max: Mapped[int] = mapped_column(Integer, nullable=False)
    allocation_policy: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class RuntimeProfileVersion(Base):
    __tablename__ = "runtime_profile_versions"
    __table_args__ = (
        CheckConstraint(
            "startup_timeout_seconds > 0 AND stop_timeout_seconds > 0 "
            "AND heartbeat_seconds > 0",
            name="runtime_profile_versions_intervals_check",
        ),
        CheckConstraint(
            "demo_only = TRUE AND allow_real_funds = FALSE "
            "AND single_writer_required = TRUE "
            "AND secret_material_present = FALSE "
            "AND executable_payload_present = FALSE",
            name="runtime_profile_versions_safety_check",
        ),
    )

    configuration_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("configuration_versions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    runtime_adapter_key: Mapped[str] = mapped_column(
        String(160),
        ForeignKey("adapter_definitions.adapter_key", ondelete="RESTRICT"),
        nullable=False,
    )
    startup_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    stop_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    heartbeat_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    image_policy: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    runtime_limits: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    demo_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    allow_real_funds: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    single_writer_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    secret_material_present: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    executable_payload_present: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )


class DeploymentProfileVersion(Base):
    __tablename__ = "deployment_profile_versions"
    __table_args__ = (
        CheckConstraint(
            "demo_only = TRUE AND allow_real_funds = FALSE "
            "AND single_writer_required = TRUE",
            name="deployment_profile_versions_safety_check",
        ),
    )

    configuration_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("configuration_versions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    execution_target_definition_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("execution_target_definitions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    promotion_profile_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey(
            "promotion_profile_versions.configuration_version_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    risk_profile_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey(
            "risk_profile_versions.configuration_version_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    capacity_profile_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey(
            "capacity_profile_versions.configuration_version_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    runtime_profile_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey(
            "runtime_profile_versions.configuration_version_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    monitoring_profile_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey(
            "monitoring_profile_versions.configuration_version_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    evidence_freshness_profile_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey(
            "evidence_freshness_profile_versions.configuration_version_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    target_selector: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    strategy_selector: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    demo_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    allow_real_funds: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    single_writer_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )


class MarketDataProfileVersion(Base):
    __tablename__ = "market_data_profile_versions"

    configuration_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("configuration_versions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    research_target_config_set_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("research_target_config_sets.id", ondelete="RESTRICT"),
        nullable=False,
    )
    market_data_policy_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey(
            "market_data_policy_versions.configuration_version_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    evidence_freshness_profile_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey(
            "evidence_freshness_profile_versions.configuration_version_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    worker_execution_profile_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey(
            "worker_execution_profile_versions.configuration_version_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )


class OptimizationProfileVersion(Base):
    __tablename__ = "optimization_profile_versions"
    __table_args__ = (
        CheckConstraint(
            "max_epochs > 0 AND executable_payload_present = FALSE",
            name="optimization_profile_versions_safety_limits_check",
        ),
    )

    configuration_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("configuration_versions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    optimizer_adapter_key: Mapped[str] = mapped_column(
        String(160),
        ForeignKey("adapter_definitions.adapter_key", ondelete="RESTRICT"),
        nullable=False,
    )
    research_target_config_set_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("research_target_config_sets.id", ondelete="RESTRICT"),
        nullable=False,
    )
    validation_window_config_set_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("validation_window_config_sets.id", ondelete="RESTRICT"),
        nullable=False,
    )
    scoring_profile_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey(
            "scoring_profile_versions.configuration_version_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    quality_gate_profile_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey(
            "quality_gate_profile_versions.configuration_version_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    market_data_policy_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey(
            "market_data_policy_versions.configuration_version_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    worker_execution_profile_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey(
            "worker_execution_profile_versions.configuration_version_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    spaces: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    loss_key: Mapped[str] = mapped_column(String(160), nullable=False)
    max_epochs: Mapped[int] = mapped_column(Integer, nullable=False)
    default_parameters: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict
    )
    resource_limits: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    executable_payload_present: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )


class UiPresentationProfileVersion(Base):
    __tablename__ = "ui_presentation_profile_versions"
    __table_args__ = (
        CheckConstraint(
            "executable_payload_present = FALSE",
            name="ui_presentation_profile_versions_safe_payload_check",
        ),
    )

    configuration_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("configuration_versions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    default_sort: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    visible_columns: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    filter_order: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    status_display_metadata: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict
    )
    page_capabilities: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict
    )
    executable_payload_present: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )


class ResearchProfileVersion(Base):
    """The sole aggregate configuration entry point for research jobs."""

    __tablename__ = "research_profile_versions"

    configuration_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("configuration_versions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    research_target_config_set_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("research_target_config_sets.id", ondelete="RESTRICT"),
        nullable=False,
    )
    validation_window_config_set_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("validation_window_config_sets.id", ondelete="RESTRICT"),
        nullable=False,
    )
    quality_gate_profile_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey(
            "quality_gate_profile_versions.configuration_version_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    scoring_profile_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey(
            "scoring_profile_versions.configuration_version_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    diversity_profile_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey(
            "diversity_profile_versions.configuration_version_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    generation_profile_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey(
            "generation_profile_versions.configuration_version_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    provider_model_config_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey(
            "provider_model_config_versions.configuration_version_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    market_data_policy_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey(
            "market_data_policy_versions.configuration_version_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    evidence_freshness_profile_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey(
            "evidence_freshness_profile_versions.configuration_version_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    scheduler_profile_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey(
            "scheduler_profile_versions.configuration_version_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    worker_execution_profile_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey(
            "worker_execution_profile_versions.configuration_version_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )


class StrategySubmission(Base):
    __tablename__ = "strategy_submissions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('RECEIVED', 'VALIDATING', 'ACCEPTED', 'DUPLICATE', "
            "'REJECTED', 'FAILED')",
            name="strategy_submissions_status_check",
        ),
        CheckConstraint(
            "length(request_digest) = 64 "
            "AND (code_digest IS NULL OR length(code_digest) = 64) "
            "AND (blueprint_digest IS NULL OR length(blueprint_digest) = 64)",
            name="strategy_submissions_digest_check",
        ),
        CheckConstraint(
            "payload_redacted = TRUE AND contains_secret_material = FALSE "
            "AND contains_executable_payload = FALSE "
            "AND execution_requested = FALSE",
            name="strategy_submissions_safe_payload_check",
        ),
        CheckConstraint(
            "(completed_at IS NULL) OR (completed_at >= created_at)",
            name="strategy_submissions_completion_time_check",
        ),
        UniqueConstraint(
            "source_adapter_key",
            "idempotency_key",
            name="strategy_submissions_source_idempotency_unique",
        ),
        Index("strategy_submissions_status_created_idx", "status", "created_at"),
        Index("strategy_submissions_code_digest_idx", "code_digest"),
    )

    id: Mapped[int] = mapped_column(
        _bigint_type(), primary_key=True, autoincrement=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    source_adapter_key: Mapped[str] = mapped_column(
        String(160),
        ForeignKey("adapter_definitions.adapter_key", ondelete="RESTRICT"),
        nullable=False,
    )
    strategy_source_key: Mapped[Optional[str]] = mapped_column(
        String(120),
        ForeignKey("strategy_source_definitions.source_key", ondelete="RESTRICT"),
    )
    provider_model_config_id: Mapped[Optional[int]] = mapped_column(
        _bigint_type(),
        ForeignKey(
            "provider_model_config_versions.configuration_version_id",
            ondelete="RESTRICT",
        ),
    )
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    code_digest: Mapped[Optional[str]] = mapped_column(String(64))
    blueprint_digest: Mapped[Optional[str]] = mapped_column(String(64))
    description: Mapped[Optional[str]] = mapped_column(Text)
    payload_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    payload_redacted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    contains_secret_material: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    contains_executable_payload: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    execution_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="RECEIVED")
    strategy_id: Mapped[Optional[int]] = mapped_column(
        _bigint_type(), ForeignKey("strategies.id", ondelete="RESTRICT")
    )
    strategy_version_id: Mapped[Optional[int]] = mapped_column(
        _bigint_type(), ForeignKey("strategy_versions.id", ondelete="RESTRICT")
    )
    research_job_id: Mapped[Optional[int]] = mapped_column(
        _bigint_type(), ForeignKey("research_jobs.id", ondelete="RESTRICT")
    )
    error_code: Mapped[Optional[str]] = mapped_column(String(160))
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    validation_started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class StrategyRuntimeInstance(Base):
    __tablename__ = "strategy_runtime_instances"
    __table_args__ = (
        CheckConstraint(
            "status IN ('UNKNOWN', 'STARTING', 'HEALTHY', 'DEGRADED', "
            "'STOPPED', 'FAILED')",
            name="strategy_runtime_instances_status_check",
        ),
        CheckConstraint(
            "demo_only = TRUE AND allow_real_funds = FALSE "
            "AND single_writer_required = TRUE",
            name="strategy_runtime_instances_safety_check",
        ),
        CheckConstraint(
            "(image_digest IS NULL OR length(image_digest) = 64) "
            "AND length(config_digest) = 64",
            name="strategy_runtime_instances_digest_check",
        ),
        CheckConstraint(
            "(status IN ('STOPPED', 'FAILED') AND stopped_at IS NOT NULL) OR "
            "(status NOT IN ('STOPPED', 'FAILED') AND stopped_at IS NULL)",
            name="strategy_runtime_instances_stop_state_check",
        ),
        UniqueConstraint(
            "runtime_adapter_key",
            "runtime_instance_id",
            name="strategy_runtime_instances_adapter_instance_unique",
        ),
        Index(
            "strategy_runtime_instances_deployment_active_idx",
            "deployment_id",
            unique=True,
            postgresql_where=text(
                "stopped_at IS NULL AND status IN "
                "('UNKNOWN','STARTING','HEALTHY','DEGRADED')"
            ),
            sqlite_where=text(
                "stopped_at IS NULL AND status IN "
                "('UNKNOWN','STARTING','HEALTHY','DEGRADED')"
            ),
        ),
        Index(
            "strategy_runtime_instances_status_heartbeat_idx",
            "status",
            "heartbeat_at",
        ),
    )

    id: Mapped[int] = mapped_column(
        _bigint_type(), primary_key=True, autoincrement=True
    )
    deployment_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("strategy_deployments.id", ondelete="RESTRICT"),
        nullable=False,
    )
    strategy_target_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("strategy_targets.id", ondelete="RESTRICT"),
        nullable=False,
    )
    configuration_bundle_snapshot_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("configuration_bundle_snapshots.id", ondelete="RESTRICT"),
        nullable=False,
    )
    runtime_adapter_key: Mapped[str] = mapped_column(
        String(160),
        ForeignKey("adapter_definitions.adapter_key", ondelete="RESTRICT"),
        nullable=False,
    )
    runtime_instance_id: Mapped[str] = mapped_column(String(200), nullable=False)
    container_name: Mapped[Optional[str]] = mapped_column(String(200))
    image_digest: Mapped[Optional[str]] = mapped_column(String(64))
    config_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="UNKNOWN")
    demo_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    allow_real_funds: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    single_writer_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    stopped_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[Optional[str]] = mapped_column(String(160))
    last_error_message: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class StrategyPositionLedgerEntry(Base):
    __tablename__ = "strategy_position_ledger_entries"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('OPEN', 'INCREASE', 'REDUCE', 'CLOSE', 'ADJUSTMENT')",
            name="strategy_position_ledger_entries_event_type_check",
        ),
        CheckConstraint(
            "quantity_delta <> 0 AND price >= 0 AND fee >= 0",
            name="strategy_position_ledger_entries_amounts_check",
        ),
        CheckConstraint(
            "(event_type = 'ADJUSTMENT' "
            "AND adjustment_reconciliation_run_id IS NOT NULL "
            "AND exchange_fill_row_id IS NULL) OR "
            "(event_type <> 'ADJUSTMENT' "
            "AND adjustment_reconciliation_run_id IS NULL "
            "AND exchange_fill_row_id IS NOT NULL)",
            name="strategy_position_ledger_entries_evidence_check",
        ),
        UniqueConstraint(
            "exchange_fill_row_id",
            name="strategy_position_ledger_entries_fill_unique",
        ),
        Index(
            "strategy_position_ledger_entries_position_idx",
            "strategy_id",
            "instrument_id",
            "position_side",
            "created_at",
        ),
        Index(
            "strategy_position_ledger_entries_deployment_idx",
            "deployment_id",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(
        _bigint_type(), primary_key=True, autoincrement=True
    )
    strategy_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("strategies.id", ondelete="RESTRICT"),
        nullable=False,
    )
    strategy_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("strategy_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    deployment_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("strategy_deployments.id", ondelete="RESTRICT"),
        nullable=False,
    )
    runtime_instance_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("strategy_runtime_instances.id", ondelete="RESTRICT"),
        nullable=False,
    )
    exchange_fill_row_id: Mapped[Optional[int]] = mapped_column(
        _bigint_type(), ForeignKey("exchange_fills.id", ondelete="RESTRICT")
    )
    adjustment_reconciliation_run_id: Mapped[Optional[int]] = mapped_column(
        _bigint_type(), ForeignKey("reconciliation_runs.id", ondelete="RESTRICT")
    )
    instrument_id: Mapped[str] = mapped_column(String(120), nullable=False)
    position_side: Mapped[str] = mapped_column(String(40), nullable=False)
    quantity_delta: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    fee: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    realized_pnl_delta: Mapped[Decimal] = mapped_column(
        Numeric(36, 18), nullable=False, default=0
    )
    event_type: Mapped[str] = mapped_column(String(16), nullable=False)
    evidence_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class StrategyPositionReconciliationItem(Base):
    __tablename__ = "strategy_position_reconciliation_items"
    __table_args__ = (
        CheckConstraint(
            "item_kind IN ('STRATEGY_ATTRIBUTION', 'INSTRUMENT_AGGREGATE')",
            name="strategy_position_reconciliation_items_kind_check",
        ),
        CheckConstraint(
            "attribution_source = 'INTERNAL_ATTRIBUTION'",
            name="strategy_position_reconciliation_items_attribution_check",
        ),
        CheckConstraint(
            "status IN ('MATCHED', 'DRIFTED', 'UNKNOWN')",
            name="strategy_position_reconciliation_items_status_check",
        ),
        CheckConstraint(
            "(item_kind = 'STRATEGY_ATTRIBUTION' AND strategy_id IS NOT NULL) OR "
            "(item_kind = 'INSTRUMENT_AGGREGATE' AND strategy_id IS NULL "
            "AND strategy_version_id IS NULL AND deployment_id IS NULL)",
            name="strategy_position_reconciliation_items_shape_check",
        ),
        UniqueConstraint(
            "reconciliation_run_id",
            "item_kind",
            "strategy_id",
            "instrument_id",
            "position_side",
            name="strategy_position_reconciliation_items_identity_unique",
        ),
        Index(
            "strategy_position_reconciliation_items_run_status_idx",
            "reconciliation_run_id",
            "status",
        ),
        Index(
            "strategy_position_reconciliation_items_aggregate_unique_idx",
            "reconciliation_run_id",
            "instrument_id",
            "position_side",
            unique=True,
            postgresql_where=text("item_kind = 'INSTRUMENT_AGGREGATE'"),
            sqlite_where=text("item_kind = 'INSTRUMENT_AGGREGATE'"),
        ),
    )

    id: Mapped[int] = mapped_column(
        _bigint_type(), primary_key=True, autoincrement=True
    )
    reconciliation_run_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("reconciliation_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    item_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    strategy_id: Mapped[Optional[int]] = mapped_column(
        _bigint_type(), ForeignKey("strategies.id", ondelete="RESTRICT")
    )
    strategy_version_id: Mapped[Optional[int]] = mapped_column(
        _bigint_type(), ForeignKey("strategy_versions.id", ondelete="RESTRICT")
    )
    deployment_id: Mapped[Optional[int]] = mapped_column(
        _bigint_type(), ForeignKey("strategy_deployments.id", ondelete="RESTRICT")
    )
    instrument_id: Mapped[str] = mapped_column(String(120), nullable=False)
    position_side: Mapped[str] = mapped_column(String(40), nullable=False)
    internal_quantity: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    aggregate_internal_quantity: Mapped[Decimal] = mapped_column(
        Numeric(36, 18), nullable=False
    )
    exchange_quantity: Mapped[Optional[Decimal]] = mapped_column(Numeric(36, 18))
    difference_quantity: Mapped[Optional[Decimal]] = mapped_column(Numeric(36, 18))
    attribution_source: Mapped[str] = mapped_column(
        String(32), nullable=False, default="INTERNAL_ATTRIBUTION"
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    evidence_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MarketDataFileRecord(Base):
    __tablename__ = "market_data_file_records"
    __table_args__ = (
        CheckConstraint(
            "file_size >= 0 AND row_count >= 0 AND gap_count >= 0 "
            "AND duplicate_count >= 0 AND null_count >= 0",
            name="market_data_file_records_counts_check",
        ),
        CheckConstraint(
            "length(file_sha256) = 64 AND length(scan_evidence_digest) = 64 "
            "AND length(source_receipt_digest) = 64",
            name="market_data_file_records_digests_check",
        ),
        CheckConstraint(
            "freshness_status IN ('FRESH', 'STALE', 'INVALID', 'UNKNOWN')",
            name="market_data_file_records_freshness_check",
        ),
        CheckConstraint(
            "first_open_at IS NULL OR last_open_at IS NULL "
            "OR first_open_at <= last_open_at",
            name="market_data_file_records_open_range_check",
        ),
        UniqueConstraint(
            "exchange",
            "market_type",
            "pair",
            "timeframe",
            "data_kind",
            "relative_path",
            "file_sha256",
            "observed_at",
            name="market_data_file_records_observation_unique",
        ),
        Index(
            "market_data_file_records_lookup_idx",
            "exchange",
            "pair",
            "timeframe",
            "data_kind",
            "observed_at",
        ),
        Index(
            "market_data_file_records_receipt_idx",
            "source_receipt_id",
            "observed_at",
        ),
    )

    id: Mapped[int] = mapped_column(
        _bigint_type(), primary_key=True, autoincrement=True
    )
    exchange: Mapped[str] = mapped_column(String(80), nullable=False)
    market_type: Mapped[str] = mapped_column(String(80), nullable=False)
    pair: Mapped[str] = mapped_column(String(120), nullable=False)
    instrument_id: Mapped[Optional[str]] = mapped_column(String(120))
    timeframe: Mapped[str] = mapped_column(
        String(40),
        ForeignKey("timeframe_definitions.timeframe_key", ondelete="RESTRICT"),
        nullable=False,
    )
    data_kind: Mapped[str] = mapped_column(String(80), nullable=False)
    absolute_path: Mapped[str] = mapped_column(Text, nullable=False)
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    file_format: Mapped[str] = mapped_column(String(40), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    row_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    first_open_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_open_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_close_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    gap_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    duplicate_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    null_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    gap_evidence: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    freshness_status: Mapped[str] = mapped_column(String(16), nullable=False)
    scan_id: Mapped[str] = mapped_column(String(160), nullable=False)
    scan_evidence_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source_receipt_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source_receipt_id: Mapped[Optional[int]] = mapped_column(
        _bigint_type(),
        ForeignKey("market_data_quality_receipts.id", ondelete="RESTRICT"),
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MarketDataUpdateJob(Base):
    __tablename__ = "market_data_update_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'PARTIAL', "
            "'FAILED', 'BLOCKED')",
            name="market_data_update_jobs_status_check",
        ),
        CheckConstraint(
            "length(request_digest) = 64",
            name="market_data_update_jobs_digest_check",
        ),
        CheckConstraint(
            "(completed_at IS NULL) OR (started_at IS NOT NULL "
            "AND completed_at >= started_at)",
            name="market_data_update_jobs_time_check",
        ),
        UniqueConstraint(
            "exchange",
            "idempotency_key",
            name="market_data_update_jobs_exchange_idempotency_unique",
        ),
        Index(
            "market_data_update_jobs_status_created_idx", "status", "created_at", "id"
        ),
    )

    id: Mapped[int] = mapped_column(
        _bigint_type(), primary_key=True, autoincrement=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    exchange: Mapped[str] = mapped_column(String(80), nullable=False)
    pair: Mapped[str] = mapped_column(String(120), nullable=False)
    trigger_source_key: Mapped[str] = mapped_column(
        String(120),
        ForeignKey("trigger_source_definitions.trigger_key", ondelete="RESTRICT"),
        nullable=False,
    )
    requested_by: Mapped[str] = mapped_column(String(160), nullable=False)
    requested_timeframes: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list
    )
    market_data_profile_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey(
            "market_data_profile_versions.configuration_version_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    configuration_bundle_snapshot_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("configuration_bundle_snapshots.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="QUEUED")
    target_closed_candle_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    error_code: Mapped[Optional[str]] = mapped_column(String(160))
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MarketDataUpdateItem(Base):
    __tablename__ = "market_data_update_items"
    __table_args__ = (
        CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'BLOCKED')",
            name="market_data_update_items_status_check",
        ),
        CheckConstraint(
            "rows_added >= 0",
            name="market_data_update_items_rows_check",
        ),
        CheckConstraint(
            "(artifact_sha256 IS NULL OR length(artifact_sha256) = 64)",
            name="market_data_update_items_digest_check",
        ),
        UniqueConstraint(
            "update_job_id",
            "file_identity_key",
            name="market_data_update_items_job_file_unique",
        ),
        Index(
            "market_data_update_items_active_file_idx",
            "file_identity_key",
            unique=True,
            postgresql_where=text("status IN ('QUEUED','RUNNING')"),
            sqlite_where=text("status IN ('QUEUED','RUNNING')"),
        ),
        Index(
            "market_data_update_items_job_status_idx",
            "update_job_id",
            "status",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(
        _bigint_type(), primary_key=True, autoincrement=True
    )
    update_job_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("market_data_update_jobs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    file_identity_key: Mapped[str] = mapped_column(String(200), nullable=False)
    pair: Mapped[str] = mapped_column(String(120), nullable=False)
    timeframe: Mapped[str] = mapped_column(
        String(40),
        ForeignKey("timeframe_definitions.timeframe_key", ondelete="RESTRICT"),
        nullable=False,
    )
    data_kind: Mapped[str] = mapped_column(String(80), nullable=False)
    before_file_record_id: Mapped[Optional[int]] = mapped_column(
        _bigint_type(), ForeignKey("market_data_file_records.id", ondelete="RESTRICT")
    )
    after_file_record_id: Mapped[Optional[int]] = mapped_column(
        _bigint_type(), ForeignKey("market_data_file_records.id", ondelete="RESTRICT")
    )
    before_first_open_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    before_last_close_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    after_first_open_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    after_last_close_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    rows_added: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    target_closed_candle_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    artifact_path: Mapped[Optional[str]] = mapped_column(Text)
    artifact_sha256: Mapped[Optional[str]] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="QUEUED")
    error_code: Mapped[Optional[str]] = mapped_column(String(160))
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class OptimizationRun(Base):
    __tablename__ = "optimization_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'BLOCKED', "
            "'CANCELLED')",
            name="optimization_runs_status_check",
        ),
        CheckConstraint(
            "length(request_digest) = 64",
            name="optimization_runs_digest_check",
        ),
        UniqueConstraint(
            "optimizer_adapter_key",
            "idempotency_key",
            name="optimization_runs_adapter_idempotency_unique",
        ),
        Index("optimization_runs_status_created_idx", "status", "created_at", "id"),
        Index(
            "optimization_runs_strategy_idx",
            "parent_strategy_version_id",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(
        _bigint_type(), primary_key=True, autoincrement=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    optimizer_adapter_key: Mapped[str] = mapped_column(
        String(160),
        ForeignKey("adapter_definitions.adapter_key", ondelete="RESTRICT"),
        nullable=False,
    )
    optimization_profile_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey(
            "optimization_profile_versions.configuration_version_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    configuration_bundle_snapshot_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("configuration_bundle_snapshots.id", ondelete="RESTRICT"),
        nullable=False,
    )
    parent_strategy_version_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("strategy_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    strategy_target_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("strategy_targets.id", ondelete="RESTRICT"),
        nullable=False,
    )
    adapter_category_key: Mapped[str] = mapped_column(String(120), nullable=False)
    adapter_display_metadata: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict
    )
    request_parameters: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="QUEUED")
    error_code: Mapped[Optional[str]] = mapped_column(String(160))
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class OptimizationTrial(Base):
    __tablename__ = "optimization_trials"
    __table_args__ = (
        CheckConstraint(
            "trial_number >= 0 AND status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', "
            "'FAILED', 'BLOCKED', 'PRUNED')",
            name="optimization_trials_status_number_check",
        ),
        CheckConstraint(
            "artifact_sha256 IS NULL OR length(artifact_sha256) = 64",
            name="optimization_trials_digest_check",
        ),
        CheckConstraint(
            "selected = FALSE OR (status = 'SUCCEEDED' "
            "AND result_strategy_version_id IS NOT NULL)",
            name="optimization_trials_selection_check",
        ),
        UniqueConstraint(
            "optimization_run_id",
            "trial_number",
            name="optimization_trials_run_number_unique",
        ),
        Index(
            "optimization_trials_selected_unique_idx",
            "optimization_run_id",
            unique=True,
            postgresql_where=text("selected = TRUE"),
            sqlite_where=text("selected = TRUE"),
        ),
        Index(
            "optimization_trials_run_status_idx",
            "optimization_run_id",
            "status",
            "trial_number",
        ),
    )

    id: Mapped[int] = mapped_column(
        _bigint_type(), primary_key=True, autoincrement=True
    )
    optimization_run_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("optimization_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    trial_number: Mapped[int] = mapped_column(Integer, nullable=False)
    parameters: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    metrics: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="QUEUED")
    artifact_path: Mapped[Optional[str]] = mapped_column(Text)
    artifact_sha256: Mapped[Optional[str]] = mapped_column(String(64))
    selected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    result_strategy_version_id: Mapped[Optional[int]] = mapped_column(
        _bigint_type(), ForeignKey("strategy_versions.id", ondelete="RESTRICT")
    )
    error_code: Mapped[Optional[str]] = mapped_column(String(160))
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class StrategyPlatformMigrationRun(Base):
    """One auditable design-lab or shared-database migration execution."""

    __tablename__ = "strategy_platform_migration_runs"
    __table_args__ = (
        CheckConstraint(
            "execution_scope IN ('DESIGN_LAB', 'SHARED_DATABASE')",
            name="strategy_platform_migration_runs_scope_check",
        ),
        CheckConstraint(
            "status IN ('PLANNED', 'RUNNING', 'RECONCILING', 'SUCCEEDED', "
            "'FAILED', 'BLOCKED')",
            name="strategy_platform_migration_runs_status_check",
        ),
        CheckConstraint(
            "source_snapshot_digest IS NULL "
            "OR length(source_snapshot_digest) = 64",
            name="strategy_platform_migration_runs_source_digest_check",
        ),
        CheckConstraint(
            "target_snapshot_digest IS NULL "
            "OR length(target_snapshot_digest) = 64",
            name="strategy_platform_migration_runs_target_digest_check",
        ),
        CheckConstraint(
            "report_digest IS NULL OR length(report_digest) = 64",
            name="strategy_platform_migration_runs_report_digest_check",
        ),
        CheckConstraint(
            "length(evidence_manifest_digest) = 64",
            name="strategy_platform_migration_runs_evidence_digest_check",
        ),
        CheckConstraint(
            "status NOT IN ('SUCCEEDED', 'FAILED', 'BLOCKED') OR "
            "(status = 'SUCCEEDED' AND target_snapshot_digest IS NOT NULL "
            "AND report_digest IS NOT NULL AND completed_at IS NOT NULL "
            "AND error_code IS NULL AND error_message IS NULL "
            "AND CAST(evidence_manifest AS TEXT) <> '{}') OR "
            "(status IN ('FAILED', 'BLOCKED') AND completed_at IS NOT NULL "
            "AND error_code IS NOT NULL AND error_message IS NOT NULL)",
            name="strategy_platform_migration_runs_terminal_shape_check",
        ),
        CheckConstraint(
            "destructive_write_count = 0 AND overwritten_row_count = 0 "
            "AND deleted_row_count = 0",
            name="strategy_platform_migration_runs_forward_only_check",
        ),
        UniqueConstraint(
            "migration_key",
            "execution_scope",
            "source_snapshot_digest",
            "request_id",
            name="strategy_platform_migration_runs_identity_unique",
        ),
        Index(
            "strategy_platform_migration_runs_status_created_idx",
            "status",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(
        _bigint_type(), primary_key=True, autoincrement=True
    )
    migration_key: Mapped[str] = mapped_column(String(160), nullable=False)
    execution_scope: Mapped[str] = mapped_column(String(24), nullable=False)
    source_schema_version: Mapped[str] = mapped_column(String(40), nullable=False)
    target_schema_version: Mapped[str] = mapped_column(String(40), nullable=False)
    source_snapshot_digest: Mapped[Optional[str]] = mapped_column(String(64))
    target_snapshot_digest: Mapped[Optional[str]] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="PLANNED")
    operator_identity: Mapped[str] = mapped_column(String(160), nullable=False)
    request_id: Mapped[str] = mapped_column(String(160), nullable=False)
    destructive_write_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    overwritten_row_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    deleted_row_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    unknown_dimensions: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    evidence_manifest: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        server_default=text("'{}'"),
    )
    evidence_manifest_digest: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        # This is the exact digest of the non-terminal default manifest ``{}``.
        # SUCCEEDED rows are separately forbidden from retaining an empty
        # manifest, so this convenience default cannot stand in for evidence.
        default="44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
    )
    report_path: Mapped[Optional[str]] = mapped_column(Text)
    report_digest: Mapped[Optional[str]] = mapped_column(String(64))
    error_code: Mapped[Optional[str]] = mapped_column(String(160))
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class StrategyPlatformMigrationTableSnapshot(Base):
    __tablename__ = "strategy_platform_migration_table_snapshots"
    __table_args__ = (
        CheckConstraint(
            "snapshot_phase IN ('BEFORE', 'AFTER')",
            name="strategy_platform_migration_table_snapshots_phase_check",
        ),
        CheckConstraint(
            "row_count >= 0 AND orphan_count >= 0",
            name="strategy_platform_migration_table_snapshots_counts_check",
        ),
        CheckConstraint(
            "content_digest IS NULL OR length(content_digest) = 64",
            name="strategy_platform_migration_table_snapshots_digest_check",
        ),
        UniqueConstraint(
            "migration_run_id",
            "snapshot_phase",
            "table_name",
            name="strategy_platform_migration_table_snapshots_identity_unique",
        ),
    )

    id: Mapped[int] = mapped_column(
        _bigint_type(), primary_key=True, autoincrement=True
    )
    migration_run_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("strategy_platform_migration_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    snapshot_phase: Mapped[str] = mapped_column(String(8), nullable=False)
    table_name: Mapped[str] = mapped_column(String(160), nullable=False)
    row_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    minimum_id: Mapped[Optional[str]] = mapped_column(String(200))
    maximum_id: Mapped[Optional[str]] = mapped_column(String(200))
    orphan_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    content_digest: Mapped[Optional[str]] = mapped_column(String(64))
    constraint_evidence: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class StrategyPlatformMigrationEntityMapping(Base):
    __tablename__ = "strategy_platform_migration_entity_mappings"
    __table_args__ = (
        CheckConstraint(
            "mapping_status IN ('MAPPED', 'PRESERVED', 'UNMAPPED', 'AMBIGUOUS', "
            "'NOT_APPLICABLE')",
            name="strategy_platform_migration_entity_mappings_status_check",
        ),
        CheckConstraint(
            "source_digest IS NULL OR length(source_digest) = 64",
            name="strategy_platform_migration_entity_mappings_source_digest_check",
        ),
        CheckConstraint(
            "target_digest IS NULL OR length(target_digest) = 64",
            name="strategy_platform_migration_entity_mappings_target_digest_check",
        ),
        CheckConstraint(
            "quality_status_asserted IS NULL "
            "OR quality_status_asserted IN ('UNKNOWN', 'QUALIFIED', 'REJECTED', "
            "'FAILED', 'BLOCKED')",
            name="strategy_platform_migration_mapping_quality_status_check",
        ),
        CheckConstraint(
            "quality_status_asserted <> 'QUALIFIED' "
            "OR dynamic_quality_evidence_id IS NOT NULL",
            name="strategy_platform_migration_mapping_qualified_evidence_check",
        ),
        UniqueConstraint(
            "migration_run_id",
            "source_table",
            "source_primary_key",
            "mapping_kind",
            name="strategy_platform_migration_entity_mappings_source_unique",
        ),
        Index(
            "strategy_platform_migration_entity_mappings_target_idx",
            "migration_run_id",
            "target_table",
            "target_primary_key",
        ),
        Index(
            "strategy_platform_migration_entity_mappings_status_idx",
            "migration_run_id",
            "mapping_status",
        ),
    )

    id: Mapped[int] = mapped_column(
        _bigint_type(), primary_key=True, autoincrement=True
    )
    migration_run_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("strategy_platform_migration_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_table: Mapped[str] = mapped_column(String(160), nullable=False)
    source_primary_key: Mapped[str] = mapped_column(String(240), nullable=False)
    mapping_kind: Mapped[str] = mapped_column(String(120), nullable=False)
    target_table: Mapped[Optional[str]] = mapped_column(String(160))
    target_primary_key: Mapped[Optional[str]] = mapped_column(String(240))
    mapping_status: Mapped[str] = mapped_column(String(24), nullable=False)
    mapping_reason: Mapped[str] = mapped_column(Text, nullable=False)
    source_digest: Mapped[Optional[str]] = mapped_column(String(64))
    target_digest: Mapped[Optional[str]] = mapped_column(String(64))
    quality_status_asserted: Mapped[Optional[str]] = mapped_column(String(16))
    dynamic_quality_evidence_id: Mapped[Optional[int]] = mapped_column(
        _bigint_type(),
        ForeignKey("strategy_evaluation_summaries.id", ondelete="RESTRICT"),
    )
    evidence_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class StrategyPlatformMigrationConflict(Base):
    __tablename__ = "strategy_platform_migration_conflicts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('OPEN', 'PRESERVED', 'RESOLVED', 'BLOCKED')",
            name="strategy_platform_migration_conflicts_status_check",
        ),
        UniqueConstraint(
            "migration_run_id",
            "conflict_key",
            name="strategy_platform_migration_conflicts_key_unique",
        ),
        Index(
            "strategy_platform_migration_conflicts_status_idx",
            "migration_run_id",
            "status",
        ),
    )

    id: Mapped[int] = mapped_column(
        _bigint_type(), primary_key=True, autoincrement=True
    )
    migration_run_id: Mapped[int] = mapped_column(
        _bigint_type(),
        ForeignKey("strategy_platform_migration_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    conflict_key: Mapped[str] = mapped_column(String(200), nullable=False)
    entity_kind: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="OPEN")
    reason_code: Mapped[str] = mapped_column(String(160), nullable=False)
    source_identifiers: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    candidate_targets: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    resolution_evidence: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
