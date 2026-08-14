"""Independent canonical-only SQLAlchemy metadata for Strategy Platform V1.3.

The module deliberately does not import ``app.models.base.Base`` or any legacy model.
Every table lives in the dedicated ``strategy_platform_v13`` schema and every foreign
key resolves within that same metadata/schema boundary.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import DeclarativeBase

from app.canonical_v13.manifest import (
    CANONICAL_BUSINESS_SCHEMA,
    CANONICAL_TABLE_NAMES,
)


NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class CanonicalBase(DeclarativeBase):
    """Declarative root isolated from the legacy application metadata."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _table(name: str, *items: object) -> Table:
    return Table(
        name,
        CanonicalBase.metadata,
        *items,
        schema=CANONICAL_BUSINESS_SCHEMA,
    )


def _uuid_id() -> Column[UUID]:
    return Column("id", Uuid(as_uuid=True), primary_key=True, default=uuid4)


def _uuid_fk(
    name: str,
    target_table: str,
    *,
    nullable: bool = False,
    unique: bool = False,
) -> Column[UUID]:
    return Column(
        name,
        Uuid(as_uuid=True),
        ForeignKey(
            f"{CANONICAL_BUSINESS_SCHEMA}.{target_table}.id",
            ondelete="RESTRICT",
        ),
        nullable=nullable,
        unique=unique,
        # PostgreSQL does not create an index for a foreign key automatically.
        # Unique FKs already receive a backing unique index; every other FK is
        # indexed explicitly so canonical relationship checks cannot degrade
        # into table scans as evidence grows.
        index=not unique,
    )


def _digest(name: str, *, nullable: bool = False) -> Column[str]:
    return Column(
        name,
        String(64),
        CheckConstraint(f"length({name}) = 64", name=f"{name}_digest_length"),
        nullable=nullable,
    )


def _created_at(name: str = "created_at", *, nullable: bool = False) -> Column[datetime]:
    return Column(name, DateTime(timezone=True), nullable=nullable)


def _status_check(table: str, column: str, values: tuple[str, ...]) -> CheckConstraint:
    quoted = ", ".join(f"'{value}'" for value in values)
    return CheckConstraint(
        f"{column} IN ({quoted})",
        name=f"{table}_{column}_values",
    )


def _lineage_columns(*, include_plan: bool = True) -> tuple[Column[Any], ...]:
    columns: list[Column[Any]] = [
        _uuid_fk("strategy_version_id", "strategy_versions"),
        _uuid_fk("research_target_id", "research_targets"),
        _uuid_fk("configuration_bundle_id", "configuration_bundles"),
        _digest("configuration_bundle_digest"),
        _uuid_fk("market_snapshot_id", "market_snapshots"),
        _digest("market_snapshot_digest"),
    ]
    if include_plan:
        columns.extend(
            (
                _uuid_fk("validation_plan_id", "validation_plans"),
                _digest("validation_plan_digest"),
            )
        )
    return tuple(columns)


# Schema identity and append-only audit.
SCHEMA_METADATA_TABLE = _table(
    "schema_metadata",
    Column("metadata_key", String(80), primary_key=True),
    Column("database_purpose", String(80), nullable=False),
    Column("business_schema", String(80), nullable=False),
    Column("genesis_version", String(32), nullable=False),
    Column("manifest_key", String(80), nullable=False),
    _digest("manifest_digest"),
    Column("legacy_import_mode", String(48), nullable=False),
    Column("production_default_target", String(16), nullable=False),
    Column("production_default_count", String(16), nullable=False),
    Column("production_default_cap", String(16), nullable=False),
    Column("trading_capability", String(32), nullable=False),
    _created_at("installed_at"),
    Column("installer_identity", String(160), nullable=False),
    CheckConstraint(
        "length(manifest_digest) = 64",
        name="schema_metadata_manifest_digest_length",
    ),
)

AUDIT_EVENTS_TABLE = _table(
    "audit_events",
    _uuid_id(),
    Column("event_type", String(80), nullable=False),
    Column("aggregate_type", String(80), nullable=False),
    Column("aggregate_id", String(160), nullable=False),
    Column("actor_identity", String(160), nullable=False),
    _digest("request_digest"),
    _digest("receipt_digest"),
    Column("evidence_json", JSON, nullable=False),
    _created_at(),
    # Authorization is the only audit aggregate with a terminal one-shot
    # contract.  The partial unique index lets unrelated append-only audit
    # streams retain repeated event types while making concurrent duplicate
    # authorize/consume/revoke events fail closed at the database boundary.
    Index(
        "audit_events_research_authorization_terminal_unique",
        "aggregate_type",
        "aggregate_id",
        "event_type",
        unique=True,
        postgresql_where=text(
            "aggregate_type = 'research_execution_authorization'"
        ),
        sqlite_where=text("aggregate_type = 'research_execution_authorization'"),
    ),
)

IDEMPOTENCY_RECEIPTS_TABLE = _table(
    "idempotency_receipts",
    _uuid_id(),
    Column("actor_identity", String(160), nullable=False),
    Column("idempotency_key", String(200), nullable=False),
    _digest("request_digest"),
    _digest("receipt_digest"),
    Column("outcome", String(32), nullable=False),
    Column("evidence_json", JSON, nullable=False),
    _created_at(),
    UniqueConstraint(
        "actor_identity",
        "idempotency_key",
        name="idempotency_receipts_actor_key_unique",
    ),
)


# Controlled submission and independent catalog identity.
STRATEGY_ARTIFACTS_TABLE = _table(
    "strategy_artifacts",
    _uuid_id(),
    _digest("content_digest"),
    Column("encoding", String(32), nullable=False),
    Column("size_bytes", Integer, nullable=False),
    Column("normalized_content", Text, nullable=False),
    _created_at(),
    UniqueConstraint(
        "content_digest", name="strategy_artifacts_content_digest_unique"
    ),
    CheckConstraint(
        "length(content_digest) = 64",
        name="strategy_artifacts_content_digest_length",
    ),
    CheckConstraint(
        "size_bytes >= 0", name="strategy_artifacts_size_bytes_nonnegative"
    ),
)

STRATEGY_SUBMISSIONS_TABLE = _table(
    "strategy_submissions",
    _uuid_id(),
    Column("caller_identity", String(160), nullable=False),
    Column("idempotency_key", String(200), nullable=False),
    _digest("request_digest"),
    _digest("source_archive_digest"),
    Column("source_entry_key", String(500), nullable=False),
    _uuid_fk("artifact_id", "strategy_artifacts", nullable=True),
    Column("status", String(24), nullable=False),
    Column("reason_code", String(120), nullable=True),
    _created_at("received_at"),
    UniqueConstraint(
        "caller_identity",
        "idempotency_key",
        name="strategy_submissions_caller_key_unique",
    ),
    UniqueConstraint(
        "source_archive_digest",
        "source_entry_key",
        name="strategy_submissions_source_entry_unique",
    ),
    _status_check(
        "strategy_submissions",
        "status",
        ("RECEIVED", "INTAKE_ACCEPTED", "REJECTED", "BLOCKED"),
    ),
)

STRATEGY_INTAKE_RECEIPTS_TABLE = _table(
    "strategy_intake_receipts",
    _uuid_id(),
    _uuid_fk("submission_id", "strategy_submissions", unique=True),
    _digest("archive_snapshot_digest"),
    _digest("source_entry_digest"),
    _digest("artifact_digest", nullable=True),
    _digest("submission_digest"),
    _digest("receipt_digest"),
    Column("status", String(24), nullable=False),
    Column("checks_json", JSON, nullable=False),
    _created_at(),
    _status_check(
        "strategy_intake_receipts",
        "status",
        ("INTAKE_ACCEPTED", "REJECTED", "BLOCKED"),
    ),
)

STRATEGIES_TABLE = _table(
    "strategies",
    _uuid_id(),
    _uuid_fk("source_submission_id", "strategy_submissions", unique=True),
    Column("catalog_status", String(16), nullable=False),
    Column("display_name", String(240), nullable=False),
    _created_at(),
    _status_check(
        "strategies", "catalog_status", ("DRAFT", "ACTIVE", "ARCHIVED")
    ),
)

STRATEGY_VERSIONS_TABLE = _table(
    "strategy_versions",
    _uuid_id(),
    _uuid_fk("strategy_id", "strategies"),
    _uuid_fk("artifact_id", "strategy_artifacts"),
    Column("version_number", Integer, nullable=False),
    Column("validation_status", String(24), nullable=False),
    Column("execution_authorized", Boolean, nullable=False),
    _created_at(),
    UniqueConstraint(
        "strategy_id",
        "version_number",
        name="strategy_versions_strategy_number_unique",
    ),
    CheckConstraint(
        "version_number > 0", name="strategy_versions_version_number_positive"
    ),
    _status_check(
        "strategy_versions",
        "validation_status",
        ("UNVALIDATED", "VALIDATING", "VALIDATED", "REJECTED", "BLOCKED"),
    ),
)


# Seven P0 configuration kinds, frozen snapshots/bundles, targets, and allocation.
CONFIGURATION_PROFILES_TABLE = _table(
    "configuration_profiles",
    _uuid_id(),
    Column("profile_key", String(160), nullable=False, unique=True),
    Column("configuration_kind", String(32), nullable=False),
    Column("scope_key", String(200), nullable=False),
    Column("workflow_key", String(160), nullable=False),
    _created_at(),
    _status_check(
        "configuration_profiles",
        "configuration_kind",
        (
            "TARGET",
            "WINDOW",
            "GENERATION",
            "DIVERSITY",
            "QUALITY_QUALIFICATION",
            "SCORING",
            "RESEARCH_AGGREGATE",
        ),
    ),
)

CONFIGURATION_VERSIONS_TABLE = _table(
    "configuration_versions",
    _uuid_id(),
    _uuid_fk("profile_id", "configuration_profiles"),
    Column("version_number", Integer, nullable=False),
    Column("lifecycle_status", String(16), nullable=False),
    Column("schema_json", JSON, nullable=False),
    Column("payload_json", JSON, nullable=False),
    _digest("schema_digest"),
    _digest("payload_digest"),
    Column("adapter_identity", String(200), nullable=False),
    _digest("adapter_digest"),
    _created_at(),
    _created_at("validated_at", nullable=True),
    _created_at("retired_at", nullable=True),
    UniqueConstraint(
        "profile_id",
        "version_number",
        name="configuration_versions_profile_number_unique",
    ),
    CheckConstraint(
        "version_number > 0", name="configuration_versions_number_positive"
    ),
    _status_check(
        "configuration_versions",
        "lifecycle_status",
        ("DRAFT", "VALIDATED", "RETIRED"),
    ),
)

CONFIGURATION_DEPENDENCIES_TABLE = _table(
    "configuration_dependencies",
    _uuid_id(),
    _uuid_fk("configuration_version_id", "configuration_versions"),
    _uuid_fk("depends_on_version_id", "configuration_versions"),
    Column("relation_key", String(120), nullable=False),
    UniqueConstraint(
        "configuration_version_id",
        "depends_on_version_id",
        "relation_key",
        name="configuration_dependencies_edge_unique",
    ),
    CheckConstraint(
        "configuration_version_id <> depends_on_version_id",
        name="configuration_dependencies_not_self",
    ),
)

CONFIGURATION_SNAPSHOTS_TABLE = _table(
    "configuration_snapshots",
    _uuid_id(),
    _uuid_fk("configuration_version_id", "configuration_versions", unique=True),
    Column("configuration_kind", String(32), nullable=False),
    _digest("schema_digest"),
    _digest("payload_digest"),
    _digest("dependency_digest"),
    _digest("adapter_manifest_digest"),
    _digest("snapshot_digest"),
    Column("snapshot_json", JSON, nullable=False),
    _created_at(),
    UniqueConstraint(
        "snapshot_digest", name="configuration_snapshots_digest_unique"
    ),
)

CONFIGURATION_SNAPSHOT_MEMBERS_TABLE = _table(
    "configuration_snapshot_members",
    _uuid_id(),
    _uuid_fk("configuration_snapshot_id", "configuration_snapshots"),
    Column("member_key", String(240), nullable=False),
    Column("member_identity", String(200), nullable=False),
    _digest("member_digest"),
    UniqueConstraint(
        "configuration_snapshot_id",
        "member_key",
        name="configuration_snapshot_members_key_unique",
    ),
)

CONFIGURATION_BUNDLES_TABLE = _table(
    "configuration_bundles",
    _uuid_id(),
    Column("scope_key", String(200), nullable=False),
    Column("workflow_key", String(160), nullable=False),
    _uuid_fk("market_snapshot_id", "market_snapshots"),
    _digest("market_snapshot_digest"),
    _digest("bundle_digest"),
    Column("capability_json", JSON, nullable=False),
    _created_at(),
    UniqueConstraint("bundle_digest", name="configuration_bundles_digest_unique"),
)

CONFIGURATION_BUNDLE_MEMBERS_TABLE = _table(
    "configuration_bundle_members",
    _uuid_id(),
    _uuid_fk("configuration_bundle_id", "configuration_bundles"),
    _uuid_fk("configuration_snapshot_id", "configuration_snapshots"),
    Column("configuration_kind", String(32), nullable=False),
    Column("member_key", String(240), nullable=False),
    _digest("snapshot_digest"),
    UniqueConstraint(
        "configuration_bundle_id",
        "member_key",
        name="configuration_bundle_members_key_unique",
    ),
)

CONFIGURATION_ACTIVATIONS_TABLE = _table(
    "configuration_activations",
    _uuid_id(),
    Column("scope_key", String(200), nullable=False),
    Column("workflow_key", String(160), nullable=False),
    _uuid_fk("configuration_bundle_id", "configuration_bundles"),
    _digest("bundle_digest"),
    _uuid_fk("previous_bundle_id", "configuration_bundles", nullable=True),
    Column("activated_by", String(160), nullable=False),
    _created_at("activated_at"),
    UniqueConstraint(
        "scope_key",
        "workflow_key",
        name="configuration_activations_scope_workflow_unique",
    ),
)

RESEARCH_TARGETS_TABLE = _table(
    "research_targets",
    _uuid_id(),
    _uuid_fk("target_snapshot_id", "configuration_snapshots"),
    Column("target_key", String(200), nullable=False),
    Column("instrument", String(120), nullable=False),
    Column("pair", String(120), nullable=False),
    Column("timeframe", String(32), nullable=False),
    Column("data_kind", String(80), nullable=False),
    _digest("target_digest"),
    UniqueConstraint(
        "target_snapshot_id",
        "target_key",
        name="research_targets_snapshot_key_unique",
    ),
)

RESEARCH_TARGET_ALLOCATIONS_TABLE = _table(
    "research_target_allocations",
    _uuid_id(),
    _uuid_fk("generation_snapshot_id", "configuration_snapshots"),
    _uuid_fk("research_target_id", "research_targets"),
    Column("allocation_count", Integer, nullable=False),
    Column("candidate_cap", Integer, nullable=True),
    _digest("allocation_digest"),
    UniqueConstraint(
        "generation_snapshot_id",
        "research_target_id",
        name="research_target_allocations_snapshot_target_unique",
    ),
    CheckConstraint(
        "allocation_count > 0",
        name="research_target_allocations_allocation_positive",
    ),
    CheckConstraint(
        "candidate_cap IS NULL OR candidate_cap >= allocation_count",
        name="research_target_allocations_cap_not_below_allocation",
    ),
)


# Independent market control plane and immutable market evidence.
MARKET_PROFILES_TABLE = _table(
    "market_profiles",
    _uuid_id(),
    Column("profile_key", String(160), nullable=False, unique=True),
    Column("scope_key", String(200), nullable=False),
    _created_at(),
)

MARKET_PROFILE_VERSIONS_TABLE = _table(
    "market_profile_versions",
    _uuid_id(),
    _uuid_fk("market_profile_id", "market_profiles"),
    Column("version_number", Integer, nullable=False),
    Column("lifecycle_status", String(16), nullable=False),
    Column("payload_json", JSON, nullable=False),
    _digest("payload_digest"),
    _created_at(),
    _created_at("validated_at", nullable=True),
    UniqueConstraint(
        "market_profile_id",
        "version_number",
        name="market_profile_versions_profile_number_unique",
    ),
    CheckConstraint(
        "version_number > 0", name="market_profile_versions_number_positive"
    ),
    _status_check(
        "market_profile_versions",
        "lifecycle_status",
        ("DRAFT", "VALIDATED", "RETIRED"),
    ),
)

MARKET_ARTIFACTS_TABLE = _table(
    "market_artifacts",
    _uuid_id(),
    _digest("content_digest"),
    Column("locator", Text, nullable=False),
    Column("size_bytes", Integer, nullable=False),
    Column("media_type", String(120), nullable=False),
    _created_at(),
    UniqueConstraint("content_digest", name="market_artifacts_digest_unique"),
    CheckConstraint(
        "size_bytes >= 0", name="market_artifacts_size_bytes_nonnegative"
    ),
)

MARKET_INSPECTIONS_TABLE = _table(
    "market_inspections",
    _uuid_id(),
    _uuid_fk("market_artifact_id", "market_artifacts"),
    Column("status", String(16), nullable=False),
    Column("inspection_json", JSON, nullable=False),
    _digest("inspection_digest"),
    Column("inspector_identity", String(160), nullable=False),
    _created_at(),
    _status_check(
        "market_inspections",
        "status",
        ("PENDING", "ACCEPTED", "REJECTED", "BLOCKED"),
    ),
)

MARKET_RECEIPTS_TABLE = _table(
    "market_receipts",
    _uuid_id(),
    _uuid_fk("market_artifact_id", "market_artifacts"),
    _uuid_fk("market_inspection_id", "market_inspections", unique=True),
    Column("status", String(16), nullable=False),
    _digest("artifact_digest"),
    _digest("inspection_digest"),
    _digest("receipt_digest"),
    _created_at(),
    _status_check(
        "market_receipts", "status", ("ACCEPTED", "REJECTED", "BLOCKED")
    ),
)

MARKET_SNAPSHOTS_TABLE = _table(
    "market_snapshots",
    _uuid_id(),
    _uuid_fk("market_profile_version_id", "market_profile_versions"),
    _digest("snapshot_digest"),
    _created_at(),
    UniqueConstraint("snapshot_digest", name="market_snapshots_digest_unique"),
)

MARKET_SNAPSHOT_MEMBERS_TABLE = _table(
    "market_snapshot_members",
    _uuid_id(),
    _uuid_fk("market_snapshot_id", "market_snapshots"),
    _uuid_fk("market_artifact_id", "market_artifacts"),
    _uuid_fk("market_receipt_id", "market_receipts"),
    _uuid_fk("research_target_id", "research_targets"),
    _created_at("coverage_start"),
    _created_at("coverage_end"),
    _digest("coverage_digest"),
    UniqueConstraint(
        "market_snapshot_id",
        "research_target_id",
        "market_artifact_id",
        name="market_snapshot_members_target_artifact_unique",
    ),
    CheckConstraint(
        "coverage_end > coverage_start",
        name="market_snapshot_members_coverage_order",
    ),
)


# Validation execution evidence.
VALIDATION_PLANS_TABLE = _table(
    "validation_plans",
    _uuid_id(),
    *_lineage_columns(include_plan=False),
    _uuid_fk("window_snapshot_id", "configuration_snapshots"),
    _digest("validation_plan_digest"),
    Column("status", String(16), nullable=False),
    _created_at(),
    UniqueConstraint(
        "strategy_version_id",
        "research_target_id",
        "configuration_bundle_id",
        "market_snapshot_id",
        "validation_plan_digest",
        name="validation_plans_exact_lineage_unique",
    ),
    _status_check(
        "validation_plans",
        "status",
        ("DECLARED", "READY", "RUNNING", "COMPLETE", "FAILED", "BLOCKED"),
    ),
)

VALIDATION_PLAN_WINDOWS_TABLE = _table(
    "validation_plan_windows",
    _uuid_id(),
    _uuid_fk("validation_plan_id", "validation_plans"),
    _uuid_fk("window_snapshot_member_id", "configuration_snapshot_members"),
    Column("window_key", String(160), nullable=False),
    _digest("window_member_digest"),
    Column("required", Boolean, nullable=False),
    _created_at("window_start"),
    _created_at("window_end"),
    UniqueConstraint(
        "validation_plan_id",
        "window_key",
        name="validation_plan_windows_plan_key_unique",
    ),
    UniqueConstraint(
        "validation_plan_id",
        "window_snapshot_member_id",
        name="validation_plan_windows_plan_member_unique",
    ),
    CheckConstraint(
        "window_end > window_start", name="validation_plan_windows_time_order"
    ),
)

VALIDATION_ATTEMPTS_TABLE = _table(
    "validation_attempts",
    _uuid_id(),
    _uuid_fk("validation_plan_id", "validation_plans"),
    Column("attempt_number", Integer, nullable=False),
    Column("status", String(16), nullable=False),
    Column("executor_identity", String(200), nullable=False),
    _digest("executor_image_digest"),
    _digest("request_digest"),
    _digest("receipt_digest", nullable=True),
    _created_at(),
    _created_at("completed_at", nullable=True),
    UniqueConstraint(
        "validation_plan_id",
        "attempt_number",
        name="validation_attempts_plan_number_unique",
    ),
    CheckConstraint(
        "attempt_number > 0", name="validation_attempts_number_positive"
    ),
    _status_check(
        "validation_attempts",
        "status",
        ("PENDING", "RUNNING", "SUCCEEDED", "FAILED", "BLOCKED"),
    ),
)

VALIDATION_WINDOW_RESULTS_TABLE = _table(
    "validation_window_results",
    _uuid_id(),
    _uuid_fk("validation_attempt_id", "validation_attempts"),
    _uuid_fk("validation_plan_window_id", "validation_plan_windows"),
    Column("metrics_json", JSON, nullable=False),
    _digest("metrics_digest"),
    _digest("receipt_digest"),
    _created_at(),
    UniqueConstraint(
        "validation_attempt_id",
        "validation_plan_window_id",
        name="validation_window_results_attempt_window_unique",
    ),
)


# Target-level scoring and qualifier-only decisions.
TARGET_SCORES_TABLE = _table(
    "target_scores",
    _uuid_id(),
    *_lineage_columns(),
    _uuid_fk("scoring_snapshot_id", "configuration_snapshots"),
    Column("overall_score", Numeric(18, 8), nullable=False),
    _digest("required_window_result_set_digest"),
    _digest("score_digest"),
    Column("scorer_identity", String(200), nullable=False),
    _created_at(),
    UniqueConstraint(
        "strategy_version_id",
        "research_target_id",
        "configuration_bundle_id",
        "market_snapshot_id",
        "validation_plan_id",
        name="target_scores_exact_lineage_unique",
    ),
)

QUALIFICATION_DECISIONS_TABLE = _table(
    "qualification_decisions",
    _uuid_id(),
    *_lineage_columns(),
    _uuid_fk("target_score_id", "target_scores"),
    _uuid_fk("quality_snapshot_id", "configuration_snapshots"),
    Column("status", String(16), nullable=False),
    Column("reason_code", String(120), nullable=False),
    _digest("decision_digest"),
    Column("qualifier_identity", String(200), nullable=False),
    _created_at(),
    UniqueConstraint(
        "strategy_version_id",
        "research_target_id",
        "configuration_bundle_id",
        "market_snapshot_id",
        "validation_plan_id",
        name="qualification_decisions_exact_lineage_unique",
    ),
    _status_check(
        "qualification_decisions",
        "status",
        ("PENDING", "QUALIFIED", "REJECTED", "BLOCKED", "FAILED"),
    ),
)

QUALIFICATION_WINDOW_EVIDENCE_TABLE = _table(
    "qualification_window_evidence",
    _uuid_id(),
    _uuid_fk("qualification_decision_id", "qualification_decisions"),
    _uuid_fk("validation_plan_window_id", "validation_plan_windows"),
    _uuid_fk("validation_window_result_id", "validation_window_results"),
    Column("hard_gate_passed", Boolean, nullable=False),
    Column("evidence_json", JSON, nullable=False),
    _digest("evidence_digest"),
    UniqueConstraint(
        "qualification_decision_id",
        "validation_plan_window_id",
        name="qualification_window_evidence_decision_window_unique",
    ),
)


# Optimization remains downstream of an accepted baseline qualification.
OPTIMIZATION_RUNS_TABLE = _table(
    "optimization_runs",
    _uuid_id(),
    _uuid_fk("baseline_qualification_decision_id", "qualification_decisions"),
    Column("status", String(24), nullable=False),
    Column("actor_identity", String(160), nullable=False),
    Column("objective_json", JSON, nullable=False),
    _digest("request_digest"),
    _digest("receipt_digest"),
    _created_at(),
    _created_at("completed_at", nullable=True),
    _status_check(
        "optimization_runs",
        "status",
        ("NOT_STARTED", "PENDING_BASELINE", "RUNNING", "SUCCEEDED", "FAILED", "BLOCKED"),
    ),
    UniqueConstraint(
        "baseline_qualification_decision_id",
        "request_digest",
        name="optimization_runs_baseline_request_unique",
    ),
)

OPTIMIZATION_TRIALS_TABLE = _table(
    "optimization_trials",
    _uuid_id(),
    _uuid_fk("optimization_run_id", "optimization_runs"),
    Column("trial_number", Integer, nullable=False),
    Column("actor_identity", String(160), nullable=False),
    Column("environment_class", String(32), nullable=False),
    Column("parameters_json", JSON, nullable=False),
    Column("metrics_json", JSON, nullable=False),
    _digest("request_digest"),
    _digest("result_digest"),
    _uuid_fk("submitted_strategy_version_id", "strategy_versions", nullable=True),
    _digest("submission_link_digest", nullable=True),
    _created_at(),
    UniqueConstraint(
        "optimization_run_id",
        "trial_number",
        name="optimization_trials_run_number_unique",
    ),
    CheckConstraint(
        "trial_number > 0", name="optimization_trials_number_positive"
    ),
)


# Human approval, deployment, and long-lived runtime are separately gated.
DEPLOYMENT_APPROVALS_TABLE = _table(
    "deployment_approvals",
    _uuid_id(),
    _uuid_fk("strategy_version_id", "strategy_versions"),
    _uuid_fk("qualification_decision_id", "qualification_decisions"),
    Column("status", String(16), nullable=False),
    Column("actor_identity", String(160), nullable=False),
    Column("reason", Text, nullable=False),
    _digest("approval_digest"),
    _created_at(),
    _status_check(
        "deployment_approvals",
        "status",
        ("NOT_REQUESTED", "PENDING", "APPROVED", "REJECTED", "REVOKED"),
    ),
)

DEPLOYMENTS_TABLE = _table(
    "deployments",
    _uuid_id(),
    _uuid_fk("deployment_approval_id", "deployment_approvals"),
    _uuid_fk("strategy_version_id", "strategy_versions"),
    _uuid_fk("configuration_bundle_id", "configuration_bundles"),
    _digest("configuration_bundle_digest"),
    _uuid_fk("market_snapshot_id", "market_snapshots"),
    _digest("market_snapshot_digest"),
    Column("status", String(16), nullable=False),
    Column("demo_only", Boolean, nullable=False),
    Column("allow_real_funds", Boolean, nullable=False),
    _digest("capability_digest"),
    _created_at(),
    _status_check(
        "deployments",
        "status",
        ("NOT_DEPLOYED", "PENDING", "ACTIVE", "FAILED", "DISABLED"),
    ),
    CheckConstraint(
        "demo_only IS TRUE AND allow_real_funds IS FALSE",
        name="deployments_demo_only_no_real_funds",
    ),
)

RUNTIME_INSTANCES_TABLE = _table(
    "runtime_instances",
    _uuid_id(),
    _uuid_fk("deployment_id", "deployments"),
    Column("runtime_identity", String(200), nullable=False, unique=True),
    _digest("image_digest"),
    _digest("launch_spec_digest"),
    Column("service_account", String(160), nullable=False),
    Column("network_policy", String(64), nullable=False),
    Column("credential_reference", String(240), nullable=False),
    Column("runtime_class", String(64), nullable=False),
    Column("filesystem_mode", String(32), nullable=False),
    Column("research_executor_capability", Boolean, nullable=False),
    Column("status", String(16), nullable=False),
    Column("order_writer_capability", Boolean, nullable=False),
    _created_at(),
    _status_check(
        "runtime_instances",
        "status",
        ("UNKNOWN", "STARTING", "HEALTHY", "DEGRADED", "FAILED", "STOPPED"),
    ),
    CheckConstraint(
        "order_writer_capability IS FALSE",
        name="runtime_instances_no_order_writer_capability",
    ),
)

RUNTIME_RECEIPTS_TABLE = _table(
    "runtime_receipts",
    _uuid_id(),
    _uuid_fk("runtime_instance_id", "runtime_instances"),
    Column("status", String(16), nullable=False),
    _digest("launch_spec_digest"),
    _digest("capability_digest"),
    Column("network_policy", String(80), nullable=False),
    Column("service_account", String(160), nullable=False),
    Column("order_writer_capability", Boolean, nullable=False),
    Column("evidence_class", String(80), nullable=False),
    Column("observation_json", JSON, nullable=False),
    _digest("observation_digest"),
    _digest("receipt_digest"),
    _created_at("observed_at"),
    _status_check(
        "runtime_receipts",
        "status",
        ("UNKNOWN", "STARTING", "HEALTHY", "DEGRADED", "FAILED", "STOPPED"),
    ),
    CheckConstraint(
        "order_writer_capability IS FALSE",
        name="runtime_receipts_no_order_writer_capability",
    ),
)


# Explicitly separated signal, risk, central order, fill, ledger, reconciliation.
SIGNALS_TABLE = _table(
    "signals",
    _uuid_id(),
    _uuid_fk("deployment_id", "deployments"),
    _uuid_fk("runtime_instance_id", "runtime_instances"),
    _uuid_fk("strategy_version_id", "strategy_versions"),
    _uuid_fk("research_target_id", "research_targets"),
    _uuid_fk("configuration_bundle_id", "configuration_bundles"),
    _digest("configuration_bundle_digest"),
    _uuid_fk("market_snapshot_id", "market_snapshots"),
    _digest("market_snapshot_digest"),
    Column("signal_json", JSON, nullable=False),
    _digest("signal_digest"),
    _created_at(),
)

TRADE_INTENTS_TABLE = _table(
    "trade_intents",
    _uuid_id(),
    _uuid_fk("signal_id", "signals", unique=True),
    Column("status", String(24), nullable=False),
    Column("intent_json", JSON, nullable=False),
    _digest("intent_digest"),
    _created_at(),
    _status_check(
        "trade_intents", "status", ("INTENT_ACCEPTED", "BLOCKED", "REJECTED")
    ),
)

RISK_DECISIONS_TABLE = _table(
    "risk_decisions",
    _uuid_id(),
    _uuid_fk("trade_intent_id", "trade_intents", unique=True),
    Column("status", String(24), nullable=False),
    Column("decision_json", JSON, nullable=False),
    _digest("decision_digest"),
    _created_at(),
    _status_check(
        "risk_decisions", "status", ("RISK_ACCEPTED", "BLOCKED", "REJECTED")
    ),
)

ORDERS_TABLE = _table(
    "orders",
    _uuid_id(),
    _uuid_fk("risk_decision_id", "risk_decisions", unique=True),
    Column("writer_identity", String(160), nullable=False),
    Column("idempotency_key", String(200), nullable=False, unique=True),
    Column("exchange_order_id", String(200), nullable=True, unique=True),
    Column("status", String(24), nullable=False),
    Column("demo_only", Boolean, nullable=False),
    Column("allow_real_funds", Boolean, nullable=False),
    _digest("request_digest"),
    _digest("receipt_digest", nullable=True),
    _created_at(),
    _status_check(
        "orders",
        "status",
        (
            "INTENT_ACCEPTED",
            "RISK_ACCEPTED",
            "SUBMITTED",
            "ACCEPTED",
            "PARTIAL",
            "FILLED",
            "CANCELLED",
            "REJECTED",
        ),
    ),
    CheckConstraint(
        "demo_only IS TRUE AND allow_real_funds IS FALSE",
        name="orders_demo_only_no_real_funds",
    ),
)

FILLS_TABLE = _table(
    "fills",
    _uuid_id(),
    _uuid_fk("order_id", "orders"),
    Column("exchange_fill_id", String(200), nullable=False, unique=True),
    Column("fill_json", JSON, nullable=False),
    _digest("receipt_digest"),
    _created_at(),
)

LEDGER_ENTRIES_TABLE = _table(
    "ledger_entries",
    _uuid_id(),
    _uuid_fk("fill_id", "fills", nullable=True),
    Column("entry_key", String(200), nullable=False, unique=True),
    Column("asset", String(40), nullable=False),
    Column("amount", Numeric(36, 18), nullable=False),
    Column("entry_type", String(80), nullable=False),
    _digest("entry_digest"),
    _created_at(),
    CheckConstraint(
        "fill_id IS NOT NULL",
        name="ledger_entries_fill_source_required",
    ),
)

RECONCILIATION_RUNS_TABLE = _table(
    "reconciliation_runs",
    _uuid_id(),
    Column("status", String(16), nullable=False),
    _digest("scope_digest"),
    _digest("receipt_digest", nullable=True),
    _created_at(),
    _created_at("completed_at", nullable=True),
    _status_check(
        "reconciliation_runs",
        "status",
        ("PENDING", "RUNNING", "SUCCEEDED", "FAILED", "BLOCKED"),
    ),
)

RECONCILIATION_ITEMS_TABLE = _table(
    "reconciliation_items",
    _uuid_id(),
    _uuid_fk("reconciliation_run_id", "reconciliation_runs"),
    _uuid_fk("order_id", "orders", nullable=True),
    _uuid_fk("fill_id", "fills", nullable=True),
    _uuid_fk("ledger_entry_id", "ledger_entries", nullable=True),
    Column("item_type", String(80), nullable=False),
    Column("status", String(16), nullable=False),
    Column("evidence_json", JSON, nullable=False),
    _digest("evidence_digest"),
    # PostgreSQL treats NULL values as distinct in a normal composite UNIQUE.
    # The canonical evidence digest is non-null for MATCHED and future GAP rows,
    # so it remains the single idempotency authority for every item shape.
    UniqueConstraint(
        "evidence_digest",
        name="reconciliation_items_evidence_digest_unique",
    ),
    CheckConstraint(
        "order_id IS NOT NULL OR fill_id IS NOT NULL OR ledger_entry_id IS NOT NULL",
        name="reconciliation_items_source_required",
    ),
    _status_check(
        "reconciliation_items", "status", ("MATCHED", "GAP", "BLOCKED")
    ),
)


CANONICAL_TABLES: dict[str, Table] = {
    table.name: table for table in CanonicalBase.metadata.sorted_tables
}

if tuple(CANONICAL_TABLES) != tuple(
    table.name for table in CanonicalBase.metadata.sorted_tables
):
    raise RuntimeError("BLOCKED_DESIGN_DRIFT: duplicate canonical metadata table")
if set(CANONICAL_TABLES) != set(CANONICAL_TABLE_NAMES):
    missing = sorted(set(CANONICAL_TABLE_NAMES) - set(CANONICAL_TABLES))
    extra = sorted(set(CANONICAL_TABLES) - set(CANONICAL_TABLE_NAMES))
    raise RuntimeError(
        f"BLOCKED_DESIGN_DRIFT: canonical metadata mismatch missing={missing} extra={extra}"
    )


__all__ = [
    "CANONICAL_TABLES",
    "CanonicalBase",
    "SCHEMA_METADATA_TABLE",
]
