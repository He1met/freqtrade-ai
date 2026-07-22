from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Index,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base


class ResearchJob(Base):
    """Durable local research job spanning Provider generation through scoring."""

    __tablename__ = "research_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'SUCCESS', 'FAILED', 'BLOCKED', "
            "'CANCELLED', 'STALE')",
            name="research_jobs_status_check",
        ),
        CheckConstraint("attempt_count >= 0", name="research_jobs_attempt_count_check"),
        CheckConstraint("max_attempts >= 1", name="research_jobs_max_attempts_check"),
        UniqueConstraint(
            "operation",
            "idempotency_key_digest",
            name="research_jobs_operation_idempotency_unique",
        ),
        Index("research_jobs_claim_idx", "status", "created_at", "id"),
        Index("research_jobs_lease_expiry_idx", "status", "lease_expires_at"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    job_type: Mapped[str] = mapped_column(String(80), nullable=False)
    operation: Mapped[str] = mapped_column(String(120), nullable=False)
    idempotency_key_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    stage: Mapped[str] = mapped_column(String(80), nullable=False, default="QUEUED")
    lease_owner: Mapped[Optional[str]] = mapped_column(String(160))
    lease_token: Mapped[Optional[str]] = mapped_column(String(64))
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    provider_attempted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    provider_completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    strategy_generation_run_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("strategy_generation_runs.id", ondelete="SET NULL"),
    )
    strategy_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("strategies.id", ondelete="SET NULL"),
    )
    strategy_version_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("strategy_versions.id", ondelete="SET NULL"),
    )
    backtest_run_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("backtest_runs.id", ondelete="SET NULL"),
    )
    backtest_task_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("backtest_tasks.id", ondelete="SET NULL"),
    )
    backtest_result_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("backtest_results.id", ondelete="SET NULL"),
    )
    strategy_score_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("strategy_scores.id", ondelete="SET NULL"),
    )
    evidence_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class ResearchWorkerControl(Base):
    """Singleton persisted pause switch for the local research worker."""

    __tablename__ = "research_worker_control"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reason: Mapped[Optional[str]] = mapped_column(String(500))
    active_job_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite")
    )
    active_lease_token: Mapped[Optional[str]] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
