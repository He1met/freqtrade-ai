from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
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


class StrategyResearchBatch(Base):
    """One auditable, non-deploying strategy research experiment."""

    __tablename__ = "strategy_research_batches"
    __table_args__ = (
        CheckConstraint(
            "status IN ('GENERATED', 'VALIDATED', 'FAILED')",
            name="strategy_research_batches_status_check",
        ),
        CheckConstraint(
            "requested_count >= 0 AND generated_count >= 0 AND persisted_count >= 0 "
            "AND qualified_count >= 0 AND rejected_count >= 0",
            name="strategy_research_batches_counts_check",
        ),
        UniqueConstraint("run_id", name="strategy_research_batches_run_id_unique"),
        UniqueConstraint("report_digest", name="strategy_research_batches_report_digest_unique"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    run_id: Mapped[str] = mapped_column(String(80), nullable=False)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False, default="codex")
    repository_commit: Mapped[str] = mapped_column(String(64), nullable=False)
    report_schema_version: Mapped[str] = mapped_column(String(120), nullable=False)
    report_path: Mapped[str] = mapped_column(Text, nullable=False)
    report_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="GENERATED")
    requested_count: Mapped[int] = mapped_column(Integer, nullable=False)
    generated_count: Mapped[int] = mapped_column(Integer, nullable=False)
    persisted_count: Mapped[int] = mapped_column(Integer, nullable=False)
    qualified_count: Mapped[int] = mapped_column(Integer, nullable=False)
    rejected_count: Mapped[int] = mapped_column(Integer, nullable=False)
    failure_reason: Mapped[Optional[str]] = mapped_column(Text)
    safety_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    selection_policy: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    window_evidence: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    candidates: Mapped[list["StrategyResearchCandidate"]] = relationship(
        "StrategyResearchCandidate",
        back_populates="batch",
        cascade="all, delete-orphan",
        order_by="StrategyResearchCandidate.id",
    )


class StrategyResearchCandidate(Base):
    """A research candidate kept separate from the canonical strategies catalog."""

    __tablename__ = "strategy_research_candidates"
    __table_args__ = (
        CheckConstraint(
            "status IN ('QUALIFIED', 'REJECTED', 'VALIDATION_FAILED')",
            name="strategy_research_candidates_status_check",
        ),
        UniqueConstraint(
            "batch_id", "candidate_name", name="strategy_research_candidates_batch_name_unique"
        ),
        UniqueConstraint(
            "batch_id", "code_digest", name="strategy_research_candidates_batch_digest_unique"
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    batch_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("strategy_research_batches.id", ondelete="CASCADE"),
        nullable=False,
    )
    candidate_name: Mapped[str] = mapped_column(String(180), nullable=False)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    code_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    loadable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    static_check: Mapped[str] = mapped_column(String(32), nullable=False)
    lookahead_status: Mapped[str] = mapped_column(String(32), nullable=False)
    score: Mapped[Optional[float]] = mapped_column(Float)
    validation_passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    deployable_candidate: Mapped[bool] = mapped_column(Boolean, nullable=False)
    rejection_reasons: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)
    evidence_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    batch: Mapped[StrategyResearchBatch] = relationship(
        "StrategyResearchBatch", back_populates="candidates"
    )
