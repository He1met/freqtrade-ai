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
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.models.base import Base


class StrategyScore(Base):
    __tablename__ = "strategy_scores"
    __table_args__ = (
        CheckConstraint("total_score >= 0", name="strategy_scores_total_score_check"),
        UniqueConstraint(
            "strategy_version_id",
            "scoring_version",
            name="strategy_scores_version_unique",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    strategy_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("strategies.id", ondelete="CASCADE"),
        nullable=False,
    )
    strategy_version_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("strategy_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    backtest_result_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("backtest_results.id", ondelete="SET NULL"),
    )
    scoring_version: Mapped[str] = mapped_column(String(80), nullable=False)
    total_score: Mapped[float] = mapped_column(Float, nullable=False)
    profit_score: Mapped[Optional[float]] = mapped_column(Float)
    risk_score: Mapped[Optional[float]] = mapped_column(Float)
    stability_score: Mapped[Optional[float]] = mapped_column(Float)
    quality_score: Mapped[Optional[float]] = mapped_column(Float)
    metrics_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    strategy = relationship("Strategy")
    strategy_version = relationship("StrategyVersion")
    backtest_result = relationship("BacktestResult")
