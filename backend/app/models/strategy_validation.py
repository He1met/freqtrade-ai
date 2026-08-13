from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
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
            "status IN ('DECLARED', 'QUEUED', 'RUNNING', 'PASSED', "
            "'QUALIFIED', 'REJECTED', 'FAILED', 'BLOCKED')",
            name="strategy_validation_plans_status_check",
        ),
        CheckConstraint(
            "cycle_number IS NULL OR cycle_number > 0",
            name="strategy_validation_plans_cycle_number_check",
        ),
        CheckConstraint(
            "(policy_snapshot_digest IS NULL OR length(policy_snapshot_digest) = 64) "
            "AND (market_data_snapshot_digest IS NULL "
            "OR length(market_data_snapshot_digest) = 64)",
            name="strategy_validation_plans_snapshot_digest_check",
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
        UniqueConstraint(
            "strategy_target_id",
            "cycle_number",
            name="strategy_validation_plans_target_cycle_unique",
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
    strategy_target_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("strategy_targets.id", ondelete="RESTRICT"),
    )
    quality_gate_profile_version_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey(
            "quality_gate_profile_versions.configuration_version_id",
            ondelete="RESTRICT",
        ),
    )
    validation_window_config_set_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("validation_window_config_sets.id", ondelete="RESTRICT"),
    )
    configuration_bundle_snapshot_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("configuration_bundle_snapshots.id", ondelete="RESTRICT"),
    )
    cycle_number: Mapped[Optional[int]] = mapped_column(Integer)
    trigger_source_key: Mapped[Optional[str]] = mapped_column(String(120))
    trigger_metadata: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
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
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    policy_snapshot_digest: Mapped[Optional[str]] = mapped_column(String(64))
    market_data_snapshot_digest: Mapped[Optional[str]] = mapped_column(String(64))
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
            "status IN ('DECLARED', 'READY', 'RUNNING', 'PASSED', "
            "'REJECTED', 'FAILED', 'BLOCKED')",
            name="strategy_validation_windows_status_check",
        ),
        CheckConstraint(
            "attempt_number > 0",
            name="strategy_validation_windows_attempt_number_check",
        ),
        CheckConstraint(
            "total_trades IS NULL OR total_trades >= 0",
            name="strategy_validation_windows_total_trades_check",
        ),
        UniqueConstraint(
            "validation_plan_id",
            "window_config_id",
            "attempt_number",
            name="strategy_validation_windows_plan_config_attempt_unique",
        ),
        UniqueConstraint("backtest_run_id", name="strategy_validation_windows_run_unique"),
        UniqueConstraint("backtest_task_id", name="strategy_validation_windows_task_unique"),
        UniqueConstraint("backtest_result_id", name="strategy_validation_windows_result_unique"),
        UniqueConstraint(
            "execution_id",
            name="strategy_validation_windows_execution_id_unique",
        ),
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
    window_config_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("validation_window_configs.id", ondelete="RESTRICT"),
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    window_kind: Mapped[Optional[str]] = mapped_column(String(24))
    window_key_snapshot: Mapped[Optional[str]] = mapped_column(String(120))
    name_zh_snapshot: Mapped[Optional[str]] = mapped_column(String(160))
    description_zh_snapshot: Mapped[Optional[str]] = mapped_column(Text)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    required_market_state: Mapped[Optional[str]] = mapped_column(String(24))
    market_state: Mapped[Optional[str]] = mapped_column(String(24))
    market_state_source: Mapped[Optional[str]] = mapped_column(String(80))
    market_state_algorithm: Mapped[Optional[str]] = mapped_column(String(80))
    market_state_parameters: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    market_state_evidence: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    market_state_evidence_digest: Mapped[Optional[str]] = mapped_column(String(64))
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
    net_profit_after_cost: Mapped[Optional[float]] = mapped_column(Float)
    max_drawdown: Mapped[Optional[float]] = mapped_column(Float)
    volatility: Mapped[Optional[float]] = mapped_column(Float)
    total_trades: Mapped[Optional[int]] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="DECLARED")
    failure_code: Mapped[Optional[str]] = mapped_column(String(160))
    failure_message: Mapped[Optional[str]] = mapped_column(Text)
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
