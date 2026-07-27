from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base


NONTERMINAL_PREDICATE = (
    "state IN ('PREPARED', 'ACKNOWLEDGED', 'RECOVERY_REQUIRED', "
    "'RESIDUAL_CLOSE_REQUIRED')"
)
PLACEMENT_PREDICATE = "operation = 'PLACE'"


class OkxOrderWriterLease(Base):
    __tablename__ = "okx_order_writer_leases"
    __table_args__ = (
        CheckConstraint(
            "execution_target_id = 'OKX_DEMO'",
            name="okx_order_writer_leases_target_check",
        ),
        CheckConstraint(
            "length(holder_token_digest) = 64",
            name="okx_order_writer_leases_digest_check",
        ),
        CheckConstraint(
            "generation >= 1",
            name="okx_order_writer_leases_generation_check",
        ),
        CheckConstraint(
            "expires_at > acquired_at AND expires_at >= heartbeat_at",
            name="okx_order_writer_leases_time_check",
        ),
    )

    execution_target_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("execution_scopes.scope_id"),
        primary_key=True,
    )
    holder_token_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OkxOrderWriteAttempt(Base):
    __tablename__ = "okx_order_write_attempts"
    __table_args__ = (
        CheckConstraint(
            "execution_target_id = 'OKX_DEMO'",
            name="okx_order_write_attempts_target_check",
        ),
        CheckConstraint(
            "operation IN ('SET_LEVERAGE', 'PLACE', 'CANCEL', 'AMEND', 'CLOSE')",
            name="okx_order_write_attempts_operation_check",
        ),
        CheckConstraint(
            "state IN ('PREPARED', 'ACKNOWLEDGED', 'REJECTED', "
            "'RECOVERY_REQUIRED', 'RESIDUAL_CLOSE_REQUIRED', 'RECONCILED')",
            name="okx_order_write_attempts_state_check",
        ),
        CheckConstraint(
            "attempt_count = 1",
            name="okx_order_write_attempts_single_post_check",
        ),
        CheckConstraint(
            "lease_generation >= 1 AND close_sequence >= 0",
            name="okx_order_write_attempts_fencing_sequence_check",
        ),
        CheckConstraint(
            "length(request_digest) = 64",
            name="okx_order_write_attempts_digest_check",
        ),
        UniqueConstraint(
            "execution_target_id",
            "operation",
            "operation_id",
            name="okx_order_write_attempts_operation_identity_unique",
        ),
        Index(
            "okx_order_write_attempts_close_sequence_idx",
            "approval_id",
            "close_sequence",
            unique=True,
            sqlite_where=text("operation = 'CLOSE'"),
            postgresql_where=text("operation = 'CLOSE'"),
        ),
        Index(
            "okx_order_write_attempts_one_unresolved_target_idx",
            "execution_target_id",
            unique=True,
            sqlite_where=text(NONTERMINAL_PREDICATE),
            postgresql_where=text(NONTERMINAL_PREDICATE),
        ),
        Index(
            "okx_order_write_attempts_one_placement_approval_idx",
            "approval_id",
            unique=True,
            sqlite_where=text(PLACEMENT_PREDICATE),
            postgresql_where=text(PLACEMENT_PREDICATE),
        ),
        Index(
            "okx_order_write_attempts_order_created_idx",
            "exchange_order_row_id",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    execution_target_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("execution_scopes.scope_id"),
        nullable=False,
    )
    exchange_order_row_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("exchange_orders.id", ondelete="CASCADE"),
        nullable=False,
    )
    approval_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        nullable=False,
    )
    recovery_grant_database_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        unique=True,
    )
    operation: Mapped[str] = mapped_column(String(16), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(32), nullable=False)
    client_order_id: Mapped[str] = mapped_column(String(32), nullable=False)
    instrument_id: Mapped[str] = mapped_column(String(80), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    safe_request_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    safe_response_snapshot: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    lease_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    parent_attempt_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("okx_order_write_attempts.id", ondelete="RESTRICT"),
    )
    close_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reason_code: Mapped[Optional[str]] = mapped_column(String(80))
    order_state: Mapped[Optional[str]] = mapped_column(String(32))
    last_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
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
