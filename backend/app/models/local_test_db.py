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


class LocalTestBatch(Base):
    """Tracks Phase 8 local-only reset/seed/dirty-data batches."""

    __tablename__ = "local_test_batches"
    __table_args__ = (
        CheckConstraint(
            "environment_label IN ("
            "'local', 'dev', 'test', 'debug', 'phase8', 'phase9', "
            "'local-test', 'phase8-local', 'phase9-local'"
            ")",
            name="local_test_batches_environment_check",
        ),
        UniqueConstraint("batch_key", name="local_test_batches_batch_key_unique"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    batch_key: Mapped[str] = mapped_column(String(120), nullable=False)
    scenario_set: Mapped[str] = mapped_column(String(80), nullable=False)
    source_label: Mapped[str] = mapped_column(String(120), nullable=False)
    environment_label: Mapped[str] = mapped_column(String(40), nullable=False)
    database_url: Mapped[str] = mapped_column(Text, nullable=False)
    seed_version: Mapped[str] = mapped_column(String(40), nullable=False)
    batch_metadata: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    events: Mapped[list["LocalTestDbEvent"]] = relationship(
        "LocalTestDbEvent",
        back_populates="batch",
        cascade="all, delete-orphan",
    )


class LocalTestDbEvent(Base):
    """Records safe local test DB reset, seed, dirty, and summary actions."""

    __tablename__ = "local_test_db_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('reset', 'baseline_seed', 'dirty_seed', 'summary')",
            name="local_test_db_events_type_check",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    batch_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("local_test_batches.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    scenario_name: Mapped[Optional[str]] = mapped_column(String(120))
    source_kind: Mapped[str] = mapped_column(String(80), nullable=False)
    details: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    batch: Mapped[LocalTestBatch] = relationship("LocalTestBatch", back_populates="events")
