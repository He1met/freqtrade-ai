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


class BacktestRun(Base):
    __tablename__ = "backtest_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')",
            name="backtest_runs_status_check",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    strategy_version_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("strategy_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    profile_name: Mapped[Optional[str]] = mapped_column(String(120))
    config_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    requested_task_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    strategy_version: Mapped["StrategyVersion"] = relationship("StrategyVersion")
    tasks: Mapped[list["BacktestTask"]] = relationship(
        "BacktestTask",
        back_populates="run",
        cascade="all, delete-orphan",
    )
    results: Mapped[list["BacktestResult"]] = relationship(
        "BacktestResult",
        back_populates="run",
        cascade="all, delete-orphan",
    )


class BacktestTask(Base):
    __tablename__ = "backtest_tasks"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')",
            name="backtest_tasks_status_check",
        ),
        UniqueConstraint(
            "backtest_run_id",
            "pair",
            "timeframe",
            name="backtest_tasks_run_pair_timeframe_unique",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    backtest_run_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("backtest_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    pair: Mapped[str] = mapped_column(String(80), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    config_path: Mapped[Optional[str]] = mapped_column(Text)
    result_path: Mapped[Optional[str]] = mapped_column(Text)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    run: Mapped[BacktestRun] = relationship("BacktestRun", back_populates="tasks")
    result: Mapped[Optional["BacktestResult"]] = relationship(
        "BacktestResult",
        back_populates="task",
        cascade="all, delete-orphan",
        uselist=False,
    )


class BacktestResult(Base):
    __tablename__ = "backtest_results"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    backtest_run_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("backtest_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    backtest_task_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("backtest_tasks.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    result_path: Mapped[str] = mapped_column(Text, nullable=False)
    metrics_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    profit_total: Mapped[Optional[float]] = mapped_column(Float)
    profit_pct: Mapped[Optional[float]] = mapped_column(Float)
    max_drawdown_pct: Mapped[Optional[float]] = mapped_column(Float)
    win_rate: Mapped[Optional[float]] = mapped_column(Float)
    total_trades: Mapped[Optional[int]] = mapped_column(Integer)
    timerange: Mapped[Optional[str]] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    run: Mapped[BacktestRun] = relationship("BacktestRun", back_populates="results")
    task: Mapped[BacktestTask] = relationship("BacktestTask", back_populates="result")
