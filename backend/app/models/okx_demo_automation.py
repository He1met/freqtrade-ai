from datetime import datetime
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
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base


class OkxDemoAutomationGuardState(Base):
    """One target-level standing Demo authorization and circuit state."""

    __tablename__ = "okx_demo_automation_guard_states"
    __table_args__ = (
        CheckConstraint(
            "execution_target_id='OKX_DEMO'",
            name="okx_demo_automation_guard_target_check",
        ),
        CheckConstraint(
            "authorization_mode IN ('DISABLED','CONTINUOUS_DEMO_V1')",
            name="okx_demo_automation_guard_authorization_check",
        ),
        CheckConstraint(
            "operational_state IN ('RUNNING','COOLDOWN','MANUAL_RESET_REQUIRED')",
            name="okx_demo_automation_guard_state_check",
        ),
        CheckConstraint(
            "critical_failure_count BETWEEN 0 AND 3 AND fencing_version>=0",
            name="okx_demo_automation_guard_counter_check",
        ),
        CheckConstraint(
            "length(policy_digest)=64 AND length(deployment_set_digest)=64",
            name="okx_demo_automation_guard_digest_check",
        ),
    )

    execution_target_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("execution_scopes.scope_id"), primary_key=True
    )
    authorization_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    operational_state: Mapped[str] = mapped_column(String(32), nullable=False)
    policy_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    deployment_set_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    critical_failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_window_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    cooldown_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    health_check_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    manual_reset_reason: Mapped[Optional[str]] = mapped_column(String(160))
    last_healthy_reconciliation_run_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("reconciliation_runs.id", ondelete="RESTRICT"),
    )
    fencing_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class OkxDemoAutomationGuardEvent(Base):
    """Append-only audit event for standing Demo dispatch and circuit decisions."""

    __tablename__ = "okx_demo_automation_guard_events"
    __table_args__ = (
        CheckConstraint(
            "execution_target_id='OKX_DEMO'",
            name="okx_demo_automation_guard_events_target_check",
        ),
        CheckConstraint(
            "event_kind IN ('AUTHORIZATION_ENABLED','ACTION_DISPATCH',"
            "'CRITICAL_FAILURE','COOLDOWN_ENTERED','HEALTH_RECOVERED',"
            "'MANUAL_LATCHED','MANUAL_RESET')",
            name="okx_demo_automation_guard_events_kind_check",
        ),
        CheckConstraint(
            "failure_class IS NULL OR failure_class IN "
            "('SUBMISSION','AUTHENTICATION','RECONCILIATION_TRANSIENT',"
            "'RECONCILIATION','DUPLICATE')",
            name="okx_demo_automation_guard_events_failure_check",
        ),
        CheckConstraint(
            "length(event_key)=64 AND length(policy_digest)=64",
            name="okx_demo_automation_guard_events_digest_check",
        ),
        UniqueConstraint("event_key", name="okx_demo_automation_guard_events_key_unique"),
        Index(
            "okx_demo_automation_guard_events_rate_idx",
            "execution_target_id",
            "event_kind",
            "observed_at",
        ),
        Index(
            "okx_demo_automation_guard_action_approval_idx",
            "approved_execution_id",
            unique=True,
            postgresql_where=text("event_kind='ACTION_DISPATCH'"),
            sqlite_where=text("event_kind='ACTION_DISPATCH'"),
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    execution_target_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("execution_scopes.scope_id"), nullable=False
    )
    event_key: Mapped[str] = mapped_column(String(64), nullable=False)
    event_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    failure_class: Mapped[Optional[str]] = mapped_column(String(24))
    policy_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    approved_execution_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("approved_executions.id", ondelete="RESTRICT"),
    )
    reconciliation_run_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("reconciliation_runs.id", ondelete="RESTRICT"),
    )
    evidence_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
