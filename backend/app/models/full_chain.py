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
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base


FULL_CHAIN_STAGES = (
    "GENERATION",
    "BACKTEST",
    "SCORING",
    "CANDIDATE_APPROVAL",
    "SIGNAL",
    "RISK",
    "EXECUTION",
    "FILL",
    "RECONCILIATION",
)


class FullChainRun(Base):
    """One durable bridge from a research job to one OKX Demo execution."""

    __tablename__ = "full_chain_runs"
    __table_args__ = (
        CheckConstraint(
            "research_scope_id = 'LOCAL_DRY_RUN'",
            name="full_chain_runs_research_scope_check",
        ),
        CheckConstraint(
            "execution_target_id = 'OKX_DEMO'",
            name="full_chain_runs_execution_target_check",
        ),
        CheckConstraint(
            "status IN ('RUNNING', 'AWAITING_APPROVAL', 'APPROVED', "
            "'EXECUTING', 'RECONCILING', 'SUCCESS', 'FAILED', 'BLOCKED', "
            "'CANCELLED', 'STALE')",
            name="full_chain_runs_status_check",
        ),
        CheckConstraint(
            "(run_kind = 'RESEARCH' AND signal_evaluation_id IS NULL) OR "
            "(run_kind = 'EXECUTION' AND signal_evaluation_id IS NOT NULL)",
            name="full_chain_runs_kind_binding_check",
        ),
        Index(
            "full_chain_runs_research_job_unique",
            "research_job_id",
            unique=True,
            postgresql_where=text("run_kind = 'RESEARCH'"),
            sqlite_where=text("run_kind = 'RESEARCH'"),
        ),
        Index(
            "full_chain_runs_signal_evaluation_unique",
            "signal_evaluation_id",
            unique=True,
            postgresql_where=text("run_kind = 'EXECUTION'"),
            sqlite_where=text("run_kind = 'EXECUTION'"),
        ),
        Index("full_chain_runs_status_created_idx", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    research_job_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("research_jobs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    research_job_attempt_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("research_job_attempts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    run_kind: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="RESEARCH",
    )
    signal_evaluation_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("signal_evaluations.id", ondelete="RESTRICT"),
    )
    research_scope_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("execution_scopes.scope_id"),
        nullable=False,
        default="LOCAL_DRY_RUN",
    )
    execution_target_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("execution_scopes.scope_id"),
        nullable=False,
        default="OKX_DEMO",
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="RUNNING")
    current_stage: Mapped[str] = mapped_column(
        String(40), nullable=False, default="GENERATION"
    )
    strategy_generation_run_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("strategy_generation_runs.id", ondelete="RESTRICT"),
    )
    strategy_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("strategies.id", ondelete="RESTRICT"),
    )
    strategy_version_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("strategy_versions.id", ondelete="RESTRICT"),
    )
    backtest_run_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("backtest_runs.id", ondelete="RESTRICT"),
    )
    backtest_task_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("backtest_tasks.id", ondelete="RESTRICT"),
    )
    backtest_result_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("backtest_results.id", ondelete="RESTRICT"),
    )
    strategy_score_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("strategy_scores.id", ondelete="RESTRICT"),
    )
    candidate_approval_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite")
    )
    signal_snapshot_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite")
    )
    trade_intent_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("trade_intents.id", ondelete="RESTRICT"),
    )
    risk_decision_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("risk_decisions.id", ondelete="RESTRICT"),
    )
    approved_execution_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("approved_executions.id", ondelete="RESTRICT"),
    )
    exchange_order_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("exchange_orders.id", ondelete="RESTRICT"),
    )
    exchange_fill_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("exchange_fills.id", ondelete="RESTRICT"),
    )
    reconciliation_run_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("reconciliation_runs.id", ondelete="RESTRICT"),
    )
    terminal_reason: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
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


class FullChainStageRun(Base):
    """Append-once stage checkpoint written before any stage side effect."""

    __tablename__ = "full_chain_stage_runs"
    __table_args__ = (
        CheckConstraint(
            "stage IN ('GENERATION', 'BACKTEST', 'SCORING', "
            "'CANDIDATE_APPROVAL', 'SIGNAL', 'RISK', 'EXECUTION', 'FILL', "
            "'RECONCILIATION')",
            name="full_chain_stage_runs_stage_check",
        ),
        CheckConstraint(
            "status IN ('PREPARED', 'SUCCESS', 'FAILED', 'BLOCKED', "
            "'CANCELLED', 'STALE')",
            name="full_chain_stage_runs_status_check",
        ),
        UniqueConstraint(
            "full_chain_run_id",
            "stage",
            name="full_chain_stage_runs_run_stage_unique",
        ),
        UniqueConstraint(
            "full_chain_run_id",
            "idempotency_key_digest",
            name="full_chain_stage_runs_idempotency_unique",
        ),
        Index("full_chain_stage_runs_created_idx", "full_chain_run_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    full_chain_run_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("full_chain_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    stage: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="PREPARED")
    idempotency_key_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    input_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    output_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    database_ids: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    error_code: Mapped[Optional[str]] = mapped_column(String(80))
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    prepared_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class StrategyCandidateApproval(Base):
    """Human approval of one exact scored candidate, separate from risk approval."""

    __tablename__ = "strategy_candidate_approvals"
    __table_args__ = (
        CheckConstraint(
            "execution_target_id = 'OKX_DEMO'",
            name="strategy_candidate_approvals_target_check",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'APPROVED', 'REJECTED', 'EXPIRED', 'REVOKED')",
            name="strategy_candidate_approvals_status_check",
        ),
        UniqueConstraint(
            "full_chain_run_id",
            name="strategy_candidate_approvals_run_unique",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    full_chain_run_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("full_chain_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    execution_target_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("execution_scopes.scope_id"),
        nullable=False,
        default="OKX_DEMO",
    )
    strategy_version_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("strategy_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    backtest_result_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("backtest_results.id", ondelete="RESTRICT"),
        nullable=False,
    )
    strategy_score_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("strategy_scores.id", ondelete="RESTRICT"),
        nullable=False,
    )
    candidate_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    # This is deliberately stored next to the human decision instead of only in
    # a stage checkpoint.  A later signal must be able to prove exactly which
    # policy and research evidence the operator approved, then revalidate it.
    promotion_policy_version: Mapped[str] = mapped_column(String(80), nullable=False)
    promotion_evidence: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="PENDING")
    requested_by: Mapped[str] = mapped_column(String(160), nullable=False)
    decided_by: Mapped[Optional[str]] = mapped_column(String(160))
    decision_reason: Mapped[Optional[str]] = mapped_column(Text)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class FullChainSignalSnapshot(Base):
    """Immutable, expiring signal evidence bound to the approved candidate."""

    __tablename__ = "full_chain_signal_snapshots"
    __table_args__ = (
        CheckConstraint(
            "execution_target_id = 'OKX_DEMO'",
            name="full_chain_signal_snapshots_target_check",
        ),
        CheckConstraint(
            "source_type IN ('database', 'api_aggregate') AND core_data = TRUE",
            name="full_chain_signal_snapshots_source_check",
        ),
        CheckConstraint(
            "observed_at < expires_at",
            name="full_chain_signal_snapshots_time_check",
        ),
        UniqueConstraint(
            "full_chain_run_id",
            name="full_chain_signal_snapshots_run_unique",
        ),
        UniqueConstraint("signal_digest", name="full_chain_signal_snapshots_digest_unique"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    full_chain_run_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("full_chain_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    candidate_approval_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("strategy_candidate_approvals.id", ondelete="RESTRICT"),
        nullable=False,
    )
    execution_target_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("execution_scopes.scope_id"),
        nullable=False,
        default="OKX_DEMO",
    )
    instrument_id: Mapped[str] = mapped_column(String(80), nullable=False)
    signal_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    core_data: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    source_database_ids: Mapped[dict] = mapped_column(JSON, nullable=False)
    signal_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
