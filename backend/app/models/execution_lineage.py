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
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base


OKX_DEMO_TARGET_ID = "OKX_DEMO"
LOCAL_DRY_RUN_SCOPE_ID = "LOCAL_DRY_RUN"
UNKNOWN_LEGACY_SCOPE_ID = "UNKNOWN_LEGACY"


class ExecutionScope(Base):
    __tablename__ = "execution_scopes"
    __table_args__ = (
        CheckConstraint(
            "scope_kind IN ('EXCHANGE_TARGET', 'NON_EXCHANGE', 'LEGACY')",
            name="execution_scopes_kind_check",
        ),
        CheckConstraint(
            "(scope_id = 'OKX_DEMO' AND scope_kind = 'EXCHANGE_TARGET' "
            "AND executable = TRUE AND exchange_writes = TRUE) OR "
            "(scope_id = 'LOCAL_DRY_RUN' AND scope_kind = 'NON_EXCHANGE' "
            "AND executable = TRUE AND exchange_writes = FALSE) OR "
            "(scope_id = 'UNKNOWN_LEGACY' AND scope_kind = 'LEGACY' "
            "AND executable = FALSE AND exchange_writes = FALSE)",
            name="execution_scopes_known_contract_check",
        ),
    )

    scope_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    executable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    exchange_writes: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ResearchJobAttempt(Base):
    __tablename__ = "research_job_attempts"
    __table_args__ = (
        CheckConstraint("attempt_number >= 1", name="research_job_attempts_number_check"),
        UniqueConstraint(
            "research_job_id",
            "attempt_number",
            name="research_job_attempts_job_number_unique",
        ),
        Index("research_job_attempts_scope_idx", "execution_scope_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    research_job_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("research_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    execution_scope_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("execution_scopes.scope_id"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="RUNNING")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    evidence_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TradeIntent(Base):
    __tablename__ = "trade_intents"
    __table_args__ = (
        CheckConstraint(
            "execution_target_id = 'OKX_DEMO'",
            name="trade_intents_okx_demo_target_check",
        ),
        CheckConstraint(
            "length(client_order_id) BETWEEN 1 AND 32",
            name="trade_intents_client_order_id_length_check",
        ),
        UniqueConstraint(
            "execution_target_id",
            "client_order_id",
            name="trade_intents_target_client_order_unique",
        ),
        Index("trade_intents_target_status_idx", "execution_target_id", "status"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    execution_target_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("execution_scopes.scope_id"), nullable=False
    )
    client_order_id: Mapped[str] = mapped_column(String(32), nullable=False)
    strategy_version_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("strategy_versions.id", ondelete="SET NULL"),
    )
    instrument_id: Mapped[str] = mapped_column(String(80), nullable=False)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    position_side: Mapped[str] = mapped_column(String(16), nullable=False)
    order_type: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    limit_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(36, 18))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING_RISK")
    request_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RiskDecision(Base):
    __tablename__ = "risk_decisions"
    __table_args__ = (
        CheckConstraint(
            "execution_target_id = 'OKX_DEMO'",
            name="risk_decisions_okx_demo_target_check",
        ),
        UniqueConstraint("trade_intent_id", name="risk_decisions_trade_intent_unique"),
        Index("risk_decisions_target_decision_idx", "execution_target_id", "decision"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    execution_target_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("execution_scopes.scope_id"), nullable=False
    )
    trade_intent_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("trade_intents.id", ondelete="CASCADE"),
        nullable=False,
    )
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(80), nullable=False)
    evidence_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ExchangeOrder(Base):
    __tablename__ = "exchange_orders"
    __table_args__ = (
        CheckConstraint(
            "execution_target_id = 'OKX_DEMO'",
            name="exchange_orders_okx_demo_target_check",
        ),
        CheckConstraint(
            "length(client_order_id) BETWEEN 1 AND 32",
            name="exchange_orders_client_order_id_length_check",
        ),
        UniqueConstraint(
            "execution_target_id",
            "client_order_id",
            name="exchange_orders_target_client_order_unique",
        ),
        UniqueConstraint(
            "execution_target_id",
            "exchange_order_id",
            name="exchange_orders_target_exchange_order_unique",
        ),
        Index("exchange_orders_target_status_idx", "execution_target_id", "status"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    execution_target_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("execution_scopes.scope_id"), nullable=False
    )
    trade_intent_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("trade_intents.id", ondelete="RESTRICT"),
        nullable=False,
    )
    client_order_id: Mapped[str] = mapped_column(String(32), nullable=False)
    exchange_order_id: Mapped[Optional[str]] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    request_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    response_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ExchangeFill(Base):
    __tablename__ = "exchange_fills"
    __table_args__ = (
        CheckConstraint(
            "execution_target_id = 'OKX_DEMO'",
            name="exchange_fills_okx_demo_target_check",
        ),
        UniqueConstraint(
            "execution_target_id",
            "exchange_fill_id",
            name="exchange_fills_target_fill_unique",
        ),
        Index("exchange_fills_target_created_idx", "execution_target_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    execution_target_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("execution_scopes.scope_id"), nullable=False
    )
    exchange_order_row_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("exchange_orders.id", ondelete="CASCADE"),
        nullable=False,
    )
    exchange_fill_id: Mapped[str] = mapped_column(String(128), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    fee: Mapped[Optional[Decimal]] = mapped_column(Numeric(36, 18))
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ExchangePosition(Base):
    __tablename__ = "exchange_positions"
    __table_args__ = (
        CheckConstraint(
            "execution_target_id = 'OKX_DEMO'",
            name="exchange_positions_okx_demo_target_check",
        ),
        UniqueConstraint(
            "execution_target_id",
            "instrument_id",
            "position_side",
            name="exchange_positions_target_instrument_side_unique",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    execution_target_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("execution_scopes.scope_id"), nullable=False
    )
    instrument_id: Mapped[str] = mapped_column(String(80), nullable=False)
    position_side: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    average_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(36, 18))
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ReconciliationRun(Base):
    __tablename__ = "reconciliation_runs"
    __table_args__ = (
        CheckConstraint(
            "execution_target_id = 'OKX_DEMO'",
            name="reconciliation_runs_okx_demo_target_check",
        ),
        Index("reconciliation_runs_target_created_idx", "execution_target_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    execution_target_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("execution_scopes.scope_id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    summary_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ExecutionManifest(Base):
    __tablename__ = "execution_manifests"
    __table_args__ = (
        CheckConstraint(
            "execution_scope_id <> 'UNKNOWN_LEGACY' OR executable_evidence = FALSE",
            name="execution_manifests_legacy_not_executable_check",
        ),
        Index("execution_manifests_scope_kind_idx", "execution_scope_id", "manifest_kind"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    execution_scope_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("execution_scopes.scope_id"), nullable=False
    )
    manifest_kind: Mapped[str] = mapped_column(String(80), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    artifact_path: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    database_ids: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    executable_evidence: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
