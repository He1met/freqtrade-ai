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


class Strategy(Base):
    __tablename__ = "strategies"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'active', 'archived')",
            name="strategies_status_check",
        ),
        CheckConstraint(
            "source IN ('ai_generated', 'imported', 'manual')",
            name="strategies_source_check",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    slug: Mapped[str] = mapped_column(String(180), nullable=False, unique=True)
    description: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="ai_generated")
    tags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    current_version_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("strategy_versions.id", ondelete="SET NULL"),
    )
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

    versions: Mapped[list["StrategyVersion"]] = relationship(
        "StrategyVersion",
        back_populates="strategy",
        cascade="all, delete-orphan",
        foreign_keys="StrategyVersion.strategy_id",
    )
    current_version: Mapped[Optional["StrategyVersion"]] = relationship(
        "StrategyVersion",
        foreign_keys=[current_version_id],
        post_update=True,
    )


class StrategyVersion(Base):
    __tablename__ = "strategy_versions"
    __table_args__ = (
        CheckConstraint("version_number > 0", name="strategy_versions_version_number_check"),
        CheckConstraint(
            "validation_status IN ('pending', 'passed', 'failed')",
            name="strategy_versions_validation_status_check",
        ),
        UniqueConstraint(
            "strategy_id",
            "version_number",
            name="strategy_versions_strategy_version_unique",
        ),
        UniqueConstraint("file_path", name="strategy_versions_file_path_unique"),
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
    generation_run_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("strategy_generation_runs.id", ondelete="SET NULL"),
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    blueprint: Mapped[dict] = mapped_column(JSON, nullable=False)
    generated_code: Mapped[str] = mapped_column(Text, nullable=False)
    code_hash: Mapped[Optional[str]] = mapped_column(String(128))
    file_path: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    validation_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    validation_errors: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    strategy: Mapped[Strategy] = relationship(
        "Strategy",
        back_populates="versions",
        foreign_keys=[strategy_id],
    )
