from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base


class StrategyDeployment(Base):
    """Immutable promotion binding for one long-running OKX Demo strategy."""

    __tablename__ = "strategy_deployments"
    __table_args__ = (
        CheckConstraint(
            "execution_target_id = 'OKX_DEMO'",
            name="strategy_deployments_target_check",
        ),
        CheckConstraint(
            "status IN ('ACTIVE', 'DISABLED')",
            name="strategy_deployments_status_check",
        ),
        CheckConstraint(
            "(status = 'ACTIVE' AND disabled_at IS NULL AND disabled_reason IS NULL) "
            "OR (status = 'DISABLED' AND disabled_at IS NOT NULL "
            "AND disabled_reason IS NOT NULL)",
            name="strategy_deployments_disable_state_check",
        ),
        UniqueConstraint(
            "candidate_approval_id",
            name="strategy_deployments_candidate_unique",
        ),
        Index(
            "strategy_deployments_target_status_idx",
            "execution_target_id",
            "status",
            "created_at",
        ),
        Index(
            "strategy_deployments_active_slot_idx",
            "execution_target_id",
            "active_slot",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
            sqlite_where=text("status = 'ACTIVE'"),
        ),
        CheckConstraint(
            "(status = 'ACTIVE' AND active_slot BETWEEN 1 AND 3) OR "
            "(status = 'DISABLED' AND active_slot IS NULL)",
            name="strategy_deployments_active_slot_check",
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
    candidate_approval_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("strategy_candidate_approvals.id", ondelete="RESTRICT"),
        nullable=False,
    )
    strategy_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("strategies.id", ondelete="RESTRICT"),
        nullable=False,
    )
    strategy_version_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("strategy_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    candidate_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    promotion_policy_version: Mapped[str] = mapped_column(String(80), nullable=False)
    deployment_policy_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    risk_policy_digest: Mapped[Optional[str]] = mapped_column(String(64))
    instrument_id: Mapped[str] = mapped_column(String(80), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")
    active_slot: Mapped[Optional[int]] = mapped_column(Integer)
    evidence_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    disabled_reason: Mapped[Optional[str]] = mapped_column(Text)
    disabled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class SignalEvaluation(Base):
    """One durable evaluation of one deployment at one closed candle."""

    __tablename__ = "signal_evaluations"
    __table_args__ = (
        CheckConstraint(
            "execution_target_id = 'OKX_DEMO'",
            name="signal_evaluations_target_check",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'LEASED', 'NO_ACTION', 'ACTIONABLE', "
            "'BLOCKED', 'FAILED')",
            name="signal_evaluations_status_check",
        ),
        CheckConstraint(
            "fencing_sequence >= 0",
            name="signal_evaluations_fencing_check",
        ),
        CheckConstraint(
            "(status = 'LEASED' AND lease_owner IS NOT NULL "
            "AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND heartbeat_at IS NOT NULL) OR "
            "(status <> 'LEASED' AND lease_owner IS NULL "
            "AND lease_token IS NULL AND lease_expires_at IS NULL "
            "AND heartbeat_at IS NULL)",
            name="signal_evaluations_lease_state_check",
        ),
        UniqueConstraint(
            "deployment_id",
            "instrument_id",
            "timeframe",
            "closed_candle_at",
            name="signal_evaluations_deployment_candle_unique",
        ),
        Index(
            "signal_evaluations_claim_idx",
            "status",
            "closed_candle_at",
            "id",
        ),
        Index(
            "signal_evaluations_lease_expiry_idx",
            "status",
            "lease_expires_at",
        ),
        Index(
            "signal_evaluations_single_consumer_idx",
            "execution_target_id",
            unique=True,
            postgresql_where=text("status = 'LEASED'"),
            sqlite_where=text("status = 'LEASED'"),
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    deployment_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("strategy_deployments.id", ondelete="CASCADE"),
        nullable=False,
    )
    execution_target_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("execution_scopes.scope_id"),
        nullable=False,
        default="OKX_DEMO",
    )
    instrument_id: Mapped[str] = mapped_column(String(80), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False)
    closed_candle_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    lease_owner: Mapped[Optional[str]] = mapped_column(String(160))
    lease_token: Mapped[Optional[str]] = mapped_column(String(64))
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    fencing_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_digest: Mapped[Optional[str]] = mapped_column(String(64))
    result_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    error_code: Mapped[Optional[str]] = mapped_column(String(80))
    error_message: Mapped[Optional[str]] = mapped_column(Text)
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
