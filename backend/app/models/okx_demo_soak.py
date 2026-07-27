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


SOAK_RUN_STATUSES = (
    "NOT_RUN",
    "BLOCKED",
    "RUNNING",
    "FAILED",
    "RECOVERY_REQUIRED",
    "PASSED",
)
SOAK_OPERATIONAL_STATES = (
    "PLANNED",
    "ACTIVE",
    "FROZEN",
    "RECONCILING",
    "RECOVERING",
    "CLEANUP",
    "STOPPED",
)


class OkxDemoSoakRun(Base):
    __tablename__ = "okx_demo_soak_runs"
    __table_args__ = (
        CheckConstraint(
            "execution_target_id = 'OKX_DEMO'",
            name="okx_demo_soak_runs_target_check",
        ),
        CheckConstraint(
            "status IN ('NOT_RUN', 'BLOCKED', 'RUNNING', 'FAILED', "
            "'RECOVERY_REQUIRED', 'PASSED')",
            name="okx_demo_soak_runs_status_check",
        ),
        CheckConstraint(
            "operational_state IN ('PLANNED', 'ACTIVE', 'FROZEN', "
            "'RECONCILING', 'RECOVERING', 'CLEANUP', 'STOPPED')",
            name="okx_demo_soak_runs_operational_state_check",
        ),
        CheckConstraint(
            "required_duration_seconds >= 604800",
            name="okx_demo_soak_runs_duration_check",
        ),
        CheckConstraint(
            "probe_interval_seconds BETWEEN 30 AND 3600 "
            "AND max_probe_gap_seconds >= probe_interval_seconds",
            name="okx_demo_soak_runs_probe_window_check",
        ),
        CheckConstraint(
            "status <> 'PASSED' OR completed_at IS NOT NULL "
            "AND final_evidence_json IS NOT NULL",
            name="okx_demo_soak_runs_pass_evidence_check",
        ),
        Index("okx_demo_soak_runs_status_idx", "status", "created_at"),
        Index(
            "okx_demo_soak_runs_one_active_idx",
            "execution_target_id",
            unique=True,
            sqlite_where=text(
                "status IN ('RUNNING', 'RECOVERY_REQUIRED')"
            ),
            postgresql_where=text(
                "status IN ('RUNNING', 'RECOVERY_REQUIRED')"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    execution_target_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("execution_scopes.scope_id"),
        nullable=False,
        default="OKX_DEMO",
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="NOT_RUN"
    )
    operational_state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="PLANNED"
    )
    e2e_evidence_id: Mapped[Optional[str]] = mapped_column(String(128))
    environment_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    gate_evidence_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    final_evidence_json: Mapped[Optional[dict]] = mapped_column(JSON)
    required_duration_seconds: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=604800
    )
    probe_interval_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=300
    )
    max_probe_gap_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=900
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class OkxDemoSoakProbe(Base):
    __tablename__ = "okx_demo_soak_probes"
    __table_args__ = (
        CheckConstraint(
            "execution_target_id = 'OKX_DEMO'",
            name="okx_demo_soak_probes_target_check",
        ),
        CheckConstraint(
            "reconciliation_status IN ('RECONCILED', 'RECOVERED', "
            "'DRIFTED', 'STALE', 'UNKNOWN')",
            name="okx_demo_soak_probes_reconciliation_check",
        ),
        CheckConstraint(
            "runtime_instances >= 0 AND writer_instances >= 0 "
            "AND repository_instances >= 0 AND database_instances >= 0 "
            "AND virtualenv_instances >= 0",
            name="okx_demo_soak_probes_nonnegative_instances_check",
        ),
        CheckConstraint(
            "open_orders >= 0 AND open_positions >= 0 "
            "AND duplicate_orders >= 0 AND unknown_positions >= 0 "
            "AND queue_depth >= 0 AND database_bytes >= 0 AND log_bytes >= 0",
            name="okx_demo_soak_probes_nonnegative_metrics_check",
        ),
        UniqueConstraint(
            "soak_run_id",
            "sequence",
            name="okx_demo_soak_probes_run_sequence_unique",
        ),
        Index(
            "okx_demo_soak_probes_run_observed_idx",
            "soak_run_id",
            "observed_at",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    soak_run_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("okx_demo_soak_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    execution_target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    runtime_instances: Mapped[int] = mapped_column(Integer, nullable=False)
    writer_instances: Mapped[int] = mapped_column(Integer, nullable=False)
    repository_instances: Mapped[int] = mapped_column(Integer, nullable=False)
    database_instances: Mapped[int] = mapped_column(Integer, nullable=False)
    virtualenv_instances: Mapped[int] = mapped_column(Integer, nullable=False)
    reconciliation_status: Mapped[str] = mapped_column(String(32), nullable=False)
    open_orders: Mapped[int] = mapped_column(Integer, nullable=False)
    open_positions: Mapped[int] = mapped_column(Integer, nullable=False)
    duplicate_orders: Mapped[int] = mapped_column(Integer, nullable=False)
    unknown_positions: Mapped[int] = mapped_column(Integer, nullable=False)
    queue_depth: Mapped[int] = mapped_column(Integer, nullable=False)
    database_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    log_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    credentials_exposed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    runtime_healthy: Mapped[bool] = mapped_column(Boolean, nullable=False)
    websocket_healthy: Mapped[bool] = mapped_column(Boolean, nullable=False)
    evidence_refs_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class OkxDemoSoakEvent(Base):
    __tablename__ = "okx_demo_soak_events"
    __table_args__ = (
        CheckConstraint(
            "execution_target_id = 'OKX_DEMO'",
            name="okx_demo_soak_events_target_check",
        ),
        CheckConstraint(
            "event_type IN ('PLANNED', 'BLOCKED', 'STARTED', 'PROBE', "
            "'FROZEN', 'RECONCILING', 'RECOVERY_REQUIRED', "
            "'RECOVERY_STARTED', 'RECOVERED', 'CLEANUP_STARTED', "
            "'FAILED', 'PASSED')",
            name="okx_demo_soak_events_type_check",
        ),
        UniqueConstraint(
            "soak_run_id",
            "sequence",
            name="okx_demo_soak_events_run_sequence_unique",
        ),
        Index(
            "okx_demo_soak_events_run_occurred_idx",
            "soak_run_id",
            "occurred_at",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    soak_run_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("okx_demo_soak_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    execution_target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(80), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    evidence_refs_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
