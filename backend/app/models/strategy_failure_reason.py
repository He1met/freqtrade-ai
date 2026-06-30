from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.models.base import Base


class StrategyFailureReason(Base):
    __tablename__ = "strategy_failure_reasons"
    __table_args__ = (
        CheckConstraint(
            "stage IN ('generation', 'validation', 'static_check', 'backtest_probe')",
            name="strategy_failure_reasons_stage_check",
        ),
        CheckConstraint(
            (
                "reason_type IN ("
                "'blueprint_schema_error', 'validation_error', 'render_error', "
                "'static_policy_violation', 'backtest_probe_failed', 'unknown'"
                ")"
            ),
            name="strategy_failure_reasons_type_check",
        ),
        CheckConstraint(
            "severity IN ('info', 'warning', 'error')",
            name="strategy_failure_reasons_severity_check",
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
    stage: Mapped[str] = mapped_column(String(40), nullable=False)
    reason_type: Mapped[str] = mapped_column(String(80), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False, default="error")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    strategy = relationship("Strategy")
    strategy_version = relationship("StrategyVersion")
