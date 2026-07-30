from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.models.base import Base


class StrategyValidationPlan(Base):
    """Immutable, pre-declared OOS/walk-forward validation plan."""

    __tablename__ = "strategy_validation_plans"
    __table_args__ = (
        CheckConstraint(
            "status IN ('DECLARED', 'RUNNING', 'PASSED', 'BLOCKED')",
            name="strategy_validation_plans_status_check",
        ),
        UniqueConstraint(
            "promotion_backtest_result_id",
            name="strategy_validation_plans_promotion_result_unique",
        ),
        UniqueConstraint(
            "strategy_version_id",
            "plan_digest",
            name="strategy_validation_plans_version_digest_unique",
        ),
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
    promotion_backtest_result_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("backtest_results.id", ondelete="RESTRICT"),
        nullable=False,
    )
    provider_name: Mapped[str] = mapped_column(String(40), nullable=False)
    strategy_code_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="DECLARED")
    promotion_evidence: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    evidence_digest: Mapped[Optional[str]] = mapped_column(String(64))
    blocked_reason: Mapped[Optional[str]] = mapped_column(Text)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    windows: Mapped[list["StrategyValidationWindow"]] = relationship(
        "StrategyValidationWindow",
        back_populates="plan",
        cascade="all, delete-orphan",
        order_by="StrategyValidationWindow.ordinal",
    )
    promotion_result = relationship("BacktestResult")
    strategy_version = relationship("StrategyVersion")


class StrategyValidationWindow(Base):
    """One independent persisted Freqtrade validation run."""

    __tablename__ = "strategy_validation_windows"
    __table_args__ = (
        CheckConstraint(
            "window_kind IN ('OOS', 'WALK_FORWARD')",
            name="strategy_validation_windows_kind_check",
        ),
        CheckConstraint(
            "status IN ('DECLARED', 'READY', 'PASSED', 'BLOCKED')",
            name="strategy_validation_windows_status_check",
        ),
        UniqueConstraint(
            "validation_plan_id",
            "ordinal",
            name="strategy_validation_windows_plan_ordinal_unique",
        ),
        UniqueConstraint("backtest_run_id", name="strategy_validation_windows_run_unique"),
        UniqueConstraint("backtest_task_id", name="strategy_validation_windows_task_unique"),
        UniqueConstraint("backtest_result_id", name="strategy_validation_windows_result_unique"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    validation_plan_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("strategy_validation_plans.id", ondelete="CASCADE"),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    window_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    market_state: Mapped[Optional[str]] = mapped_column(String(24))
    timerange: Mapped[str] = mapped_column(String(80), nullable=False)
    profile_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    expected_config_digest: Mapped[Optional[str]] = mapped_column(String(64))
    expected_market_data_digest: Mapped[str] = mapped_column(String(64), nullable=False)
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
    execution_id: Mapped[Optional[str]] = mapped_column(String(160))
    artifact_manifest_checksum: Mapped[Optional[str]] = mapped_column(String(64))
    result_checksum: Mapped[Optional[str]] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="DECLARED")
    blocked_reason: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    plan: Mapped[StrategyValidationPlan] = relationship(
        "StrategyValidationPlan", back_populates="windows"
    )
    backtest_run = relationship("BacktestRun")
    backtest_task = relationship("BacktestTask")
    backtest_result = relationship("BacktestResult")
