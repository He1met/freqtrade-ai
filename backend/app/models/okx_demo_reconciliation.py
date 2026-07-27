from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base


TARGET_CHECK = "execution_target_id = 'OKX_DEMO'"
STATUS_CHECK = (
    "status IN ('RECONCILED', 'DRIFTED', 'STALE', 'UNKNOWN', 'RECOVERED')"
)


class OkxDemoExchangeEvent(Base):
    __tablename__ = "okx_demo_exchange_events"
    __table_args__ = (
        CheckConstraint(TARGET_CHECK, name="okx_demo_exchange_events_target_check"),
        CheckConstraint(
            "source IN ('REST', 'WS')",
            name="okx_demo_exchange_events_source_check",
        ),
        CheckConstraint(
            "source = 'REST' OR source_sequence IS NOT NULL",
            name="okx_demo_exchange_events_ws_sequence_check",
        ),
        CheckConstraint(
            "entity_kind IN ('ORDER', 'FILL', 'POSITION', 'ACCOUNT')",
            name="okx_demo_exchange_events_kind_check",
        ),
        CheckConstraint(
            "length(event_key) = 64 AND length(payload_digest) = 64",
            name="okx_demo_exchange_events_digest_check",
        ),
        UniqueConstraint(
            "execution_target_id",
            "event_key",
            name="okx_demo_exchange_events_target_key_unique",
        ),
        UniqueConstraint(
            "execution_target_id",
            "source",
            "entity_kind",
            "stream_generation",
            "source_sequence",
            name="okx_demo_exchange_events_stream_sequence_unique",
        ),
        UniqueConstraint(
            "execution_target_id",
            "source",
            "entity_kind",
            "entity_key",
            "stream_generation",
            "observed_at",
            name="okx_demo_exchange_events_entity_time_unique",
        ),
        Index(
            "okx_demo_exchange_events_entity_observed_idx",
            "execution_target_id",
            "entity_kind",
            "entity_key",
            "observed_at",
        ),
    )

    database_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    execution_target_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("execution_scopes.scope_id"), nullable=False
    )
    event_key: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(8), nullable=False)
    entity_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    entity_key: Mapped[str] = mapped_column(String(160), nullable=False)
    source_sequence: Mapped[Optional[int]] = mapped_column(BigInteger)
    stream_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recovery_batch_id: Mapped[Optional[str]] = mapped_column(String(64))
    recovery_batch_database_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("okx_demo_recovery_batches.database_id", ondelete="RESTRICT"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class OkxDemoOrderSnapshot(Base):
    __tablename__ = "okx_demo_order_snapshots"
    __table_args__ = (
        CheckConstraint(TARGET_CHECK, name="okx_demo_order_snapshots_target_check"),
        CheckConstraint(
            "status IN ('live', 'partially_filled', 'filled', 'canceled', 'mmp_canceled')",
            name="okx_demo_order_snapshots_status_check",
        ),
        UniqueConstraint(
            "execution_target_id",
            "event_database_id",
            name="okx_demo_order_snapshots_event_unique",
        ),
        Index(
            "okx_demo_order_snapshots_order_observed_idx",
            "execution_target_id",
            "exchange_order_id",
            "observed_at",
        ),
    )

    database_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    execution_target_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("execution_scopes.scope_id"), nullable=False
    )
    event_database_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("okx_demo_exchange_events.database_id", ondelete="RESTRICT"),
        nullable=False,
    )
    exchange_order_id: Mapped[str] = mapped_column(String(128), nullable=False)
    client_order_id: Mapped[Optional[str]] = mapped_column(String(32))
    instrument_id: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    filled_quantity: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    average_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(36, 18))
    reduce_only: Mapped[bool] = mapped_column(Boolean, nullable=False)
    authoritative_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OkxDemoFillSnapshot(Base):
    __tablename__ = "okx_demo_fill_snapshots"
    __table_args__ = (
        CheckConstraint(TARGET_CHECK, name="okx_demo_fill_snapshots_target_check"),
        UniqueConstraint(
            "execution_target_id",
            "event_database_id",
            name="okx_demo_fill_snapshots_event_unique",
        ),
    )

    database_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    execution_target_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("execution_scopes.scope_id"), nullable=False
    )
    event_database_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("okx_demo_exchange_events.database_id", ondelete="RESTRICT"),
        nullable=False,
    )
    exchange_fill_id: Mapped[str] = mapped_column(String(128), nullable=False)
    exchange_order_id: Mapped[str] = mapped_column(String(128), nullable=False)
    instrument_id: Mapped[str] = mapped_column(String(80), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    fee: Mapped[Optional[Decimal]] = mapped_column(Numeric(36, 18))
    authoritative_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OkxDemoPositionSnapshot(Base):
    __tablename__ = "okx_demo_position_snapshots"
    __table_args__ = (
        CheckConstraint(TARGET_CHECK, name="okx_demo_position_snapshots_target_check"),
        UniqueConstraint(
            "execution_target_id",
            "event_database_id",
            name="okx_demo_position_snapshots_event_unique",
        ),
        Index(
            "okx_demo_position_snapshots_identity_observed_idx",
            "execution_target_id",
            "instrument_id",
            "position_side",
            "observed_at",
        ),
    )

    database_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    execution_target_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("execution_scopes.scope_id"), nullable=False
    )
    event_database_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("okx_demo_exchange_events.database_id", ondelete="RESTRICT"),
        nullable=False,
    )
    instrument_id: Mapped[str] = mapped_column(String(80), nullable=False)
    position_side: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    average_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(36, 18))
    authoritative_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OkxDemoAccountSnapshot(Base):
    __tablename__ = "okx_demo_account_snapshots"
    __table_args__ = (
        CheckConstraint(TARGET_CHECK, name="okx_demo_account_snapshots_target_check"),
        UniqueConstraint(
            "execution_target_id",
            "event_database_id",
            name="okx_demo_account_snapshots_event_unique",
        ),
        Index(
            "okx_demo_account_snapshots_observed_idx",
            "execution_target_id",
            "observed_at",
        ),
    )

    database_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    execution_target_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("execution_scopes.scope_id"), nullable=False
    )
    event_database_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("okx_demo_exchange_events.database_id", ondelete="RESTRICT"),
        nullable=False,
    )
    account_fingerprint_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    equity: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    available_balance: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    margin_balance: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    authoritative_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OkxDemoReconciliationState(Base):
    __tablename__ = "okx_demo_reconciliation_states"
    __table_args__ = (
        CheckConstraint(TARGET_CHECK, name="okx_demo_reconciliation_states_target_check"),
        CheckConstraint(STATUS_CHECK, name="okx_demo_reconciliation_states_status_check"),
        UniqueConstraint(
            "execution_target_id",
            name="okx_demo_reconciliation_states_target_unique",
        ),
    )

    database_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    execution_target_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("execution_scopes.scope_id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    opening_frozen: Mapped[bool] = mapped_column(Boolean, nullable=False)
    block_reason: Mapped[Optional[str]] = mapped_column(String(160))
    last_event_observed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    last_reconciliation_run_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("reconciliation_runs.id", ondelete="SET NULL"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class OkxDemoRecoveryBatch(Base):
    __tablename__ = "okx_demo_recovery_batches"
    __table_args__ = (
        CheckConstraint(TARGET_CHECK, name="okx_demo_recovery_batches_target_check"),
        CheckConstraint(
            "authenticated = TRUE AND pagination_complete = TRUE",
            name="okx_demo_recovery_batches_complete_check",
        ),
        CheckConstraint(
            "observed_at <= completed_at",
            name="okx_demo_recovery_batches_time_check",
        ),
        UniqueConstraint(
            "execution_target_id",
            "recovery_batch_id",
            name="okx_demo_recovery_batches_target_batch_unique",
        ),
    )

    database_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    execution_target_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("execution_scopes.scope_id"), nullable=False
    )
    recovery_batch_id: Mapped[str] = mapped_column(String(64), nullable=False)
    authenticated: Mapped[bool] = mapped_column(Boolean, nullable=False)
    pagination_complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    complete_streams: Mapped[list] = mapped_column(JSON, nullable=False)
    high_watermarks: Mapped[dict] = mapped_column(JSON, nullable=False)
    overlap_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_count: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_digest: Mapped[str] = mapped_column(String(64), nullable=False)


class OkxDemoRecoveryGrant(Base):
    __tablename__ = "okx_demo_recovery_grants"
    __table_args__ = (
        CheckConstraint(TARGET_CHECK, name="okx_demo_recovery_grants_target_check"),
        CheckConstraint(
            "action IN ('CANCEL', 'REDUCE_ONLY')",
            name="okx_demo_recovery_grants_action_check",
        ),
        CheckConstraint(
            "status IN ('ACTIVE', 'CONSUMED', 'EXPIRED')",
            name="okx_demo_recovery_grants_status_check",
        ),
        CheckConstraint(
            "length(grant_digest) = 64 AND max_quantity >= 0",
            name="okx_demo_recovery_grants_digest_quantity_check",
        ),
        UniqueConstraint(
            "execution_target_id",
            "grant_digest",
            name="okx_demo_recovery_grants_target_digest_unique",
        ),
        Index(
            "okx_demo_recovery_grants_status_expiry_idx",
            "execution_target_id",
            "status",
            "expires_at",
        ),
    )

    database_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    execution_target_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("execution_scopes.scope_id"), nullable=False
    )
    reconciliation_run_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("reconciliation_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    exchange_order_row_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("exchange_orders.id", ondelete="RESTRICT"),
    )
    grant_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    instrument_id: Mapped[str] = mapped_column(String(80), nullable=False)
    position_side: Mapped[str] = mapped_column(String(16), nullable=False)
    max_quantity: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
