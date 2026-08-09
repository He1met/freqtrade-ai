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
    Index,
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


class MarketDataQualityReceipt(Base):
    """Immutable evidence that one exact market-data artifact was inspected."""

    __tablename__ = "market_data_quality_receipts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PASSED', 'BLOCKED', 'FAILED')",
            name="market_data_quality_receipts_status_check",
        ),
        CheckConstraint(
            "row_count >= 0 AND expected_interval_seconds > 0 "
            "AND missing_interval_count >= 0 AND duplicate_timestamp_count >= 0 "
            "AND out_of_order_count >= 0 AND misaligned_timestamp_count >= 0 "
            "AND null_ohlcv_count >= 0 AND invalid_ohlc_count >= 0 "
            "AND negative_volume_count >= 0",
            name="market_data_quality_receipts_counts_check",
        ),
        UniqueConstraint(
            "evidence_digest", name="market_data_quality_receipts_digest_unique"
        ),
        CheckConstraint(
            "length(file_sha256) = 64 AND length(evidence_digest) = 64",
            name="market_data_quality_receipts_digest_check",
        ),
        CheckConstraint(
            "status <> 'PASSED' OR (row_count > 0 AND first_open_at IS NOT NULL "
            "AND last_open_at IS NOT NULL AND missing_interval_count = 0 "
            "AND duplicate_timestamp_count = 0 AND out_of_order_count = 0 "
            "AND misaligned_timestamp_count = 0 AND null_ohlcv_count = 0 "
            "AND invalid_ohlc_count = 0 AND negative_volume_count = 0)",
            name="market_data_quality_receipts_passed_check",
        ),
        Index(
            "market_data_quality_receipts_pair_time_idx",
            "pair",
            "timeframe",
            "inspected_at",
        ),
        Index(
            "market_data_quality_receipts_file_contract_idx",
            "file_sha256",
            "contract_version",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    contract_version: Mapped[str] = mapped_column(String(40), nullable=False)
    exchange: Mapped[str] = mapped_column(String(40), nullable=False)
    pair: Mapped[str] = mapped_column(String(80), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(20), nullable=False)
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    file_format: Mapped[str] = mapped_column(String(20), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    inspected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    first_open_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_open_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    expected_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    missing_interval_count: Mapped[int] = mapped_column(Integer, nullable=False)
    duplicate_timestamp_count: Mapped[int] = mapped_column(Integer, nullable=False)
    out_of_order_count: Mapped[int] = mapped_column(Integer, nullable=False)
    misaligned_timestamp_count: Mapped[int] = mapped_column(Integer, nullable=False)
    null_ohlcv_count: Mapped[int] = mapped_column(Integer, nullable=False)
    invalid_ohlc_count: Mapped[int] = mapped_column(Integer, nullable=False)
    negative_volume_count: Mapped[int] = mapped_column(Integer, nullable=False)
    freshness_seconds: Mapped[Optional[int]] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    reason_codes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    evidence_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class StrategyResearchAttemptEvent(Base):
    """Append-only control-plane receipt for every formal research invocation."""

    __tablename__ = "strategy_research_attempt_events"
    __table_args__ = (
        CheckConstraint(
            "phase IN ('PRECHECK', 'STARTED', 'TERMINAL', 'RECOVERY')",
            name="strategy_research_attempt_events_phase_check",
        ),
        CheckConstraint(
            "outcome IN ('NOT_GENERATED', 'RUNNING', 'COMPLETED', 'FAILED', 'BLOCKED')",
            name="strategy_research_attempt_events_outcome_check",
        ),
        CheckConstraint(
            "trigger IN ('manual', 'automation')",
            name="strategy_research_attempt_events_trigger_check",
        ),
        CheckConstraint(
            "sequence >= 1 AND requested_count >= 0 AND generated_count >= 0 "
            "AND validated_count >= 0 AND persisted_count >= 0 "
            "AND qualified_count >= 0 AND rejected_count >= 0 "
            "AND generated_count <= requested_count "
            "AND validated_count <= generated_count "
            "AND persisted_count <= generated_count "
            "AND qualified_count + rejected_count <= persisted_count",
            name="strategy_research_attempt_events_counts_check",
        ),
        CheckConstraint(
            "length(event_digest) = 64",
            name="strategy_research_attempt_events_digest_check",
        ),
        CheckConstraint(
            "outcome <> 'NOT_GENERATED' OR (requested_count = 0 "
            "AND generated_count = 0 AND validated_count = 0 "
            "AND persisted_count = 0 AND qualified_count = 0 AND rejected_count = 0)",
            name="strategy_research_attempt_events_not_generated_check",
        ),
        UniqueConstraint(
            "attempt_id", "sequence", name="strategy_research_attempt_events_identity_unique"
        ),
        UniqueConstraint(
            "event_digest", name="strategy_research_attempt_events_digest_unique"
        ),
        Index(
            "strategy_research_attempt_events_created_idx", "created_at", "id"
        ),
        Index(
            "strategy_research_attempt_events_run_idx", "run_id", "created_at"
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    attempt_id: Mapped[str] = mapped_column(String(36), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    run_id: Mapped[Optional[str]] = mapped_column(String(80))
    batch_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("strategy_research_batches.id", ondelete="RESTRICT"),
    )
    market_data_quality_receipt_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("market_data_quality_receipts.id", ondelete="RESTRICT"),
    )
    trigger: Mapped[str] = mapped_column(String(20), nullable=False)
    phase: Mapped[str] = mapped_column(String(20), nullable=False)
    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(80), nullable=False)
    redacted_reason: Mapped[str] = mapped_column(Text, nullable=False)
    requested_count: Mapped[int] = mapped_column(Integer, nullable=False)
    generated_count: Mapped[int] = mapped_column(Integer, nullable=False)
    validated_count: Mapped[int] = mapped_column(Integer, nullable=False)
    persisted_count: Mapped[int] = mapped_column(Integer, nullable=False)
    qualified_count: Mapped[int] = mapped_column(Integer, nullable=False)
    rejected_count: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    event_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
