"""Strategy Platform V1.3 database foundations.

This module intentionally contains persistence contracts only.  Runtime configuration
resolution, API handlers, backfills, and deployment behavior are later phases.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.models.base import Base


class ConfigurationType(Base):
    __tablename__ = "configuration_types"

    type_key: Mapped[str] = mapped_column(String(120), primary_key=True)
    name_zh: Mapped[str] = mapped_column(String(160), nullable=False)
    description_zh: Mapped[str] = mapped_column(Text, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(40), nullable=False)
    handler_key: Mapped[str] = mapped_column(String(160), nullable=False)
    editor_capability: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class ConfigurationVersion(Base):
    __tablename__ = "configuration_versions"
    __table_args__ = (
        CheckConstraint(
            "lifecycle_status IN ('DRAFT', 'VALIDATED', 'RETIRED')",
            name="configuration_versions_lifecycle_status_check",
        ),
        CheckConstraint(
            "version_number > 0",
            name="configuration_versions_version_number_check",
        ),
        CheckConstraint(
            "length(config_digest) = 64",
            name="configuration_versions_digest_check",
        ),
        CheckConstraint(
            "(lifecycle_status = 'DRAFT' AND validated_at IS NULL) OR "
            "(lifecycle_status IN ('VALIDATED', 'RETIRED') AND validated_at IS NOT NULL)",
            name="configuration_versions_validation_time_check",
        ),
        UniqueConstraint(
            "type_key",
            "version_number",
            name="configuration_versions_type_number_unique",
        ),
        UniqueConstraint(
            "id",
            "type_key",
            name="configuration_versions_id_type_unique",
        ),
        Index(
            "configuration_versions_type_status_idx",
            "type_key",
            "lifecycle_status",
            "version_number",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    type_key: Mapped[str] = mapped_column(
        String(120),
        ForeignKey("configuration_types.type_key", ondelete="RESTRICT"),
        nullable=False,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    lifecycle_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="DRAFT"
    )
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    schema_version: Mapped[str] = mapped_column(String(40), nullable=False)
    config_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    change_summary: Mapped[Optional[str]] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(160), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    validated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class ConfigurationDependency(Base):
    __tablename__ = "configuration_dependencies"
    __table_args__ = (
        CheckConstraint(
            "configuration_version_id <> depends_on_version_id",
            name="configuration_dependencies_not_self_check",
        ),
        UniqueConstraint(
            "configuration_version_id",
            "depends_on_version_id",
            "relation_key",
            name="configuration_dependencies_edge_unique",
        ),
        Index(
            "configuration_dependencies_parent_idx",
            "depends_on_version_id",
            "configuration_version_id",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    configuration_version_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("configuration_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    depends_on_version_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("configuration_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    relation_key: Mapped[str] = mapped_column(String(120), nullable=False)


class ConfigurationActivation(Base):
    __tablename__ = "configuration_activations"
    __table_args__ = (
        ForeignKeyConstraint(
            ("version_id", "config_type"),
            ("configuration_versions.id", "configuration_versions.type_key"),
            ondelete="RESTRICT",
            name="configuration_activations_version_type_fkey",
        ),
        CheckConstraint(
            "length(scope_type) > 0 AND length(scope_key) > 0",
            name="configuration_activations_scope_check",
        ),
        UniqueConstraint(
            "config_type",
            "scope_type",
            "scope_key",
            name="configuration_activations_scope_unique",
        ),
        Index(
            "configuration_activations_version_idx",
            "version_id",
            "config_type",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    config_type: Mapped[str] = mapped_column(String(120), nullable=False)
    scope_type: Mapped[str] = mapped_column(String(80), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(160), nullable=False)
    version_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), nullable=False
    )
    activated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    activated_by: Mapped[str] = mapped_column(String(160), nullable=False)


class ConfigurationAuditEvent(Base):
    __tablename__ = "configuration_audit_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('DRAFT_CREATED', 'VALIDATED', 'ACTIVATED', "
            "'DEACTIVATED', 'RETIRED', 'VALIDATION_FAILED', 'ACTIVATION_FAILED')",
            name="configuration_audit_events_type_check",
        ),
        Index(
            "configuration_audit_events_version_created_idx",
            "configuration_version_id",
            "created_at",
        ),
        Index(
            "configuration_audit_events_request_idx",
            "request_id",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    configuration_version_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("configuration_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor: Mapped[str] = mapped_column(String(160), nullable=False)
    request_id: Mapped[str] = mapped_column(String(160), nullable=False)
    scope_type: Mapped[Optional[str]] = mapped_column(String(80))
    scope_key: Mapped[Optional[str]] = mapped_column(String(160))
    reason: Mapped[Optional[str]] = mapped_column(Text)
    event_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ConfigurationBundleSnapshot(Base):
    __tablename__ = "configuration_bundle_snapshots"
    __table_args__ = (
        CheckConstraint(
            "length(workflow_kind) > 0 AND length(scope_type) > 0 "
            "AND length(scope_key) > 0",
            name="configuration_bundle_snapshots_scope_check",
        ),
        CheckConstraint(
            "length(bundle_digest) = 64",
            name="configuration_bundle_snapshots_digest_check",
        ),
        UniqueConstraint(
            "workflow_kind",
            "scope_type",
            "scope_key",
            "bundle_digest",
            name="configuration_bundle_snapshots_identity_unique",
        ),
        Index(
            "configuration_bundle_snapshots_aggregate_idx",
            "aggregate_profile_version_id",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    workflow_kind: Mapped[str] = mapped_column(String(80), nullable=False)
    scope_type: Mapped[str] = mapped_column(String(80), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(160), nullable=False)
    aggregate_profile_version_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("configuration_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    resolved_versions_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    resolved_digests_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    bundle_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    capability_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ExecutionTargetDefinition(Base):
    __tablename__ = "execution_target_definitions"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    target_key: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ExecutionTargetDefinitionVersion(Base):
    __tablename__ = "execution_target_definition_versions"
    __table_args__ = (
        ForeignKeyConstraint(
            ("exchange_adapter_key",),
            ("adapter_definitions.adapter_key",),
            ondelete="RESTRICT",
            use_alter=True,
            name="exec_target_versions_exchange_adapter_fkey",
        ),
        ForeignKeyConstraint(
            ("runtime_adapter_key",),
            ("adapter_definitions.adapter_key",),
            ondelete="RESTRICT",
            use_alter=True,
            name="exec_target_versions_runtime_adapter_fkey",
        ),
        CheckConstraint(
            "demo_only = TRUE AND allow_real_funds = FALSE "
            "AND single_writer_required = TRUE",
            name="execution_target_definition_versions_safety_check",
        ),
        UniqueConstraint(
            "execution_target_definition_id",
            "configuration_version_id",
            name="execution_target_definition_versions_identity_unique",
        ),
    )

    configuration_version_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("configuration_versions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    execution_target_definition_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("execution_target_definitions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    name_zh: Mapped[str] = mapped_column(String(160), nullable=False)
    description_zh: Mapped[str] = mapped_column(Text, nullable=False)
    scope_kind: Mapped[str] = mapped_column(String(80), nullable=False)
    exchange_adapter_key: Mapped[Optional[str]] = mapped_column(String(160))
    runtime_adapter_key: Mapped[Optional[str]] = mapped_column(String(160))
    writer_policy: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    demo_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    allow_real_funds: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    single_writer_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )


class StrategyTarget(Base):
    __tablename__ = "strategy_targets"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ENABLED', 'DISABLED')",
            name="strategy_targets_status_check",
        ),
        UniqueConstraint(
            "strategy_version_id",
            "execution_target_id",
            "instrument_id",
            "timeframe",
            name="strategy_targets_version_target_instrument_timeframe_unique",
        ),
        Index(
            "strategy_targets_validation_queue_idx",
            "status",
            "validation_priority",
            "last_completed_validation_at",
            "created_at",
        ),
        Index("strategy_targets_pair_timeframe_idx", "pair", "timeframe"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    strategy_version_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("strategy_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    execution_target_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("execution_target_definitions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    instrument_id: Mapped[str] = mapped_column(String(120), nullable=False)
    pair: Mapped[str] = mapped_column(String(120), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ENABLED")
    validation_priority: Mapped[int] = mapped_column(Integer, nullable=False)
    last_completed_validation_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    next_validation_not_before: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
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


class ValidationWindowConfigSet(Base):
    __tablename__ = "validation_window_config_sets"
    __table_args__ = (
        ForeignKeyConstraint(
            ("default_classifier_adapter_key",),
            ("adapter_definitions.adapter_key",),
            ondelete="RESTRICT",
            use_alter=True,
            name="validation_window_sets_classifier_adapter_fkey",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("configuration_versions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    default_classifier_adapter_key: Mapped[str] = mapped_column(
        String(160), nullable=False
    )
    default_classifier_parameters: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict
    )


class ValidationWindowPurpose(Base):
    __tablename__ = "validation_window_purposes"
    __table_args__ = (
        UniqueConstraint(
            "config_set_id",
            "key",
            name="validation_window_purposes_set_key_unique",
        ),
        Index(
            "validation_window_purposes_set_order_idx",
            "config_set_id",
            "sort_order",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    config_set_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("validation_window_config_sets.id", ondelete="RESTRICT"),
        nullable=False,
    )
    key: Mapped[str] = mapped_column(String(120), nullable=False)
    name_zh: Mapped[str] = mapped_column(String(160), nullable=False)
    description_zh: Mapped[str] = mapped_column(Text, nullable=False)
    counts_for_qualification: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)


class MarketRegimeDefinition(Base):
    __tablename__ = "market_regime_definitions"
    __table_args__ = (
        UniqueConstraint(
            "config_set_id",
            "dimension_key",
            "key",
            name="market_regime_definitions_set_dimension_key_unique",
        ),
        Index(
            "market_regime_definitions_set_order_idx",
            "config_set_id",
            "dimension_key",
            "sort_order",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    config_set_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("validation_window_config_sets.id", ondelete="RESTRICT"),
        nullable=False,
    )
    key: Mapped[str] = mapped_column(String(120), nullable=False)
    name_zh: Mapped[str] = mapped_column(String(160), nullable=False)
    description_zh: Mapped[str] = mapped_column(Text, nullable=False)
    dimension_key: Mapped[str] = mapped_column(String(120), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)


class ValidationWindowConfig(Base):
    __tablename__ = "validation_window_configs"
    __table_args__ = (
        ForeignKeyConstraint(
            ("classifier_adapter_key",),
            ("adapter_definitions.adapter_key",),
            ondelete="RESTRICT",
            use_alter=True,
            name="validation_windows_classifier_adapter_fkey",
        ),
        ForeignKeyConstraint(
            ("config_set_id", "purpose_key"),
            (
                "validation_window_purposes.config_set_id",
                "validation_window_purposes.key",
            ),
            ondelete="RESTRICT",
            name="validation_window_configs_purpose_fkey",
        ),
        CheckConstraint(
            "start_at < end_at",
            name="validation_window_configs_time_check",
        ),
        UniqueConstraint(
            "config_set_id",
            "pair",
            "timeframe",
            "data_kind",
            "window_key",
            name="validation_window_configs_identity_unique",
        ),
        UniqueConstraint(
            "config_set_id",
            "pair",
            "timeframe",
            "data_kind",
            "ordinal",
            name="validation_window_configs_ordinal_unique",
        ),
        Index(
            "validation_window_configs_lookup_idx",
            "config_set_id",
            "pair",
            "timeframe",
            "data_kind",
            "ordinal",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    config_set_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("validation_window_config_sets.id", ondelete="RESTRICT"),
        nullable=False,
    )
    pair: Mapped[str] = mapped_column(String(120), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(40), nullable=False)
    data_kind: Mapped[str] = mapped_column(String(80), nullable=False)
    window_key: Mapped[str] = mapped_column(String(120), nullable=False)
    purpose_key: Mapped[str] = mapped_column(String(120), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    name_zh: Mapped[str] = mapped_column(String(160), nullable=False)
    description_zh: Mapped[str] = mapped_column(Text, nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    classifier_adapter_key: Mapped[Optional[str]] = mapped_column(String(160))
    classifier_parameters: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict
    )
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    source_receipt_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("market_data_quality_receipts.id", ondelete="RESTRICT"),
    )
    classification_evidence: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict
    )


class ValidationWindowExpectation(Base):
    __tablename__ = "validation_window_expectations"
    __table_args__ = (
        UniqueConstraint(
            "window_config_id",
            "dimension_key",
            "operator",
            "expected_value",
            name="validation_window_expectations_identity_unique",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    window_config_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("validation_window_configs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    dimension_key: Mapped[str] = mapped_column(String(120), nullable=False)
    operator: Mapped[str] = mapped_column(String(40), nullable=False)
    expected_value: Mapped[str] = mapped_column(String(160), nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class MetricDefinition(Base):
    __tablename__ = "metric_definitions"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    metric_key: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)


class MetricDefinitionVersion(Base):
    __tablename__ = "metric_definition_versions"
    __table_args__ = (
        UniqueConstraint(
            "metric_definition_id",
            "configuration_version_id",
            name="metric_definition_versions_identity_unique",
        ),
    )

    configuration_version_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("configuration_versions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    metric_definition_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("metric_definitions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    name_zh: Mapped[str] = mapped_column(String(160), nullable=False)
    unit: Mapped[str] = mapped_column(String(40), nullable=False)
    data_source: Mapped[str] = mapped_column(String(160), nullable=False)
    available_aggregations: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list
    )
    display_metadata: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class QualityGateProfile(Base):
    __tablename__ = "quality_gate_profiles"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    profile_key: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)


class QualityGateProfileVersion(Base):
    __tablename__ = "quality_gate_profile_versions"
    __table_args__ = (
        UniqueConstraint(
            "quality_gate_profile_id",
            "configuration_version_id",
            name="quality_gate_profile_versions_identity_unique",
        ),
    )

    configuration_version_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("configuration_versions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    quality_gate_profile_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("quality_gate_profiles.id", ondelete="RESTRICT"),
        nullable=False,
    )


class QualityGateRule(Base):
    __tablename__ = "quality_gate_rules"
    __table_args__ = (
        ForeignKeyConstraint(
            ("evaluation_adapter_key",),
            ("adapter_definitions.adapter_key",),
            ondelete="RESTRICT",
            use_alter=True,
            name="quality_gate_rules_evaluation_adapter_fkey",
        ),
        CheckConstraint(
            "severity IN ('BLOCKING', 'WARNING')",
            name="quality_gate_rules_severity_check",
        ),
        CheckConstraint(
            "score_weight IS NULL OR score_weight >= 0",
            name="quality_gate_rules_weight_check",
        ),
        Index(
            "quality_gate_rules_profile_priority_idx",
            "profile_version_id",
            "priority",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    profile_version_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey(
            "quality_gate_profile_versions.configuration_version_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    pair: Mapped[Optional[str]] = mapped_column(String(120))
    timeframe: Mapped[Optional[str]] = mapped_column(String(40))
    window_selector: Mapped[dict] = mapped_column(JSON, nullable=False)
    metric_definition_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey(
            "metric_definition_versions.configuration_version_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    evaluation_adapter_key: Mapped[str] = mapped_column(String(160), nullable=False)
    evaluation_parameters: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict
    )
    threshold_value: Mapped[Optional[float]] = mapped_column(Numeric(24, 12))
    threshold_max: Mapped[Optional[float]] = mapped_column(Numeric(24, 12))
    unit: Mapped[str] = mapped_column(String(40), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    score_weight: Mapped[Optional[float]] = mapped_column(Numeric(24, 12))
    priority: Mapped[int] = mapped_column(Integer, nullable=False)


class ValidationWindowScore(Base):
    __tablename__ = "validation_window_scores"
    __table_args__ = (
        CheckConstraint(
            "total_score >= 0",
            name="validation_window_scores_total_score_check",
        ),
        CheckConstraint(
            "length(score_digest) = 64",
            name="validation_window_scores_digest_check",
        ),
        UniqueConstraint(
            "validation_window_id",
            name="validation_window_scores_window_unique",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    validation_window_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("strategy_validation_windows.id", ondelete="RESTRICT"),
        nullable=False,
    )
    scoring_version: Mapped[str] = mapped_column(String(80), nullable=False)
    profile_version_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("configuration_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    total_score: Mapped[float] = mapped_column(Float, nullable=False)
    component_scores_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    metrics_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    score_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    components: Mapped[list["ValidationWindowScoreComponent"]] = relationship(
        "ValidationWindowScoreComponent",
        back_populates="validation_window_score",
        order_by="ValidationWindowScoreComponent.ordinal",
    )


class ValidationWindowScoreComponent(Base):
    __tablename__ = "validation_window_score_components"
    __table_args__ = (
        UniqueConstraint(
            "validation_window_score_id",
            "component_key",
            name="validation_window_score_components_key_unique",
        ),
        UniqueConstraint(
            "validation_window_score_id",
            "ordinal",
            name="validation_window_score_components_ordinal_unique",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    validation_window_score_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("validation_window_scores.id", ondelete="RESTRICT"),
        nullable=False,
    )
    component_key: Mapped[str] = mapped_column(String(120), nullable=False)
    raw_value: Mapped[Optional[float]] = mapped_column(Numeric(24, 12))
    normalized_value: Mapped[float] = mapped_column(Numeric(24, 12), nullable=False)
    weight: Mapped[float] = mapped_column(Numeric(24, 12), nullable=False)
    contribution: Mapped[float] = mapped_column(Numeric(24, 12), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)

    validation_window_score: Mapped[ValidationWindowScore] = relationship(
        "ValidationWindowScore", back_populates="components"
    )


class QualityRuleEvaluation(Base):
    __tablename__ = "quality_rule_evaluations"
    __table_args__ = (
        UniqueConstraint(
            "validation_window_score_id",
            "quality_gate_rule_id",
            name="quality_rule_evaluations_score_rule_unique",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    validation_window_score_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("validation_window_scores.id", ondelete="RESTRICT"),
        nullable=False,
    )
    quality_gate_rule_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("quality_gate_rules.id", ondelete="RESTRICT"),
        nullable=False,
    )
    actual_value: Mapped[Optional[float]] = mapped_column(Numeric(24, 12))
    operator: Mapped[str] = mapped_column(String(40), nullable=False)
    threshold_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    failure_code: Mapped[Optional[str]] = mapped_column(String(160))
    explanation: Mapped[Optional[str]] = mapped_column(Text)


class StrategyEvaluationSummary(Base):
    __tablename__ = "strategy_evaluation_summaries"
    __table_args__ = (
        CheckConstraint(
            "status IN ('QUALIFIED', 'REJECTED', 'FAILED', 'BLOCKED')",
            name="strategy_evaluation_summaries_status_check",
        ),
        CheckConstraint(
            "required_window_count >= 0 AND passed_window_count >= 0 "
            "AND failed_window_count >= 0 "
            "AND passed_window_count + failed_window_count <= required_window_count",
            name="strategy_evaluation_summaries_counts_check",
        ),
        CheckConstraint(
            "length(summary_digest) = 64",
            name="strategy_evaluation_summaries_digest_check",
        ),
        UniqueConstraint(
            "validation_plan_id",
            name="strategy_evaluation_summaries_plan_unique",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    validation_plan_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("strategy_validation_plans.id", ondelete="RESTRICT"),
        nullable=False,
    )
    required_window_count: Mapped[int] = mapped_column(Integer, nullable=False)
    passed_window_count: Mapped[int] = mapped_column(Integer, nullable=False)
    failed_window_count: Mapped[int] = mapped_column(Integer, nullable=False)
    overall_score: Mapped[Optional[float]] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    primary_failure_window_config_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("validation_window_configs.id", ondelete="RESTRICT"),
    )
    reason_codes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    summary_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
