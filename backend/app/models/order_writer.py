from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
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


class OkxDemoCanaryConsentHandoff(Base):
    """Owner-managed one-use consent bridging job 22 to fresh execution evidence."""

    __tablename__ = "okx_demo_canary_consent_handoffs"
    __table_args__ = (
        CheckConstraint(
            "execution_target_id = 'OKX_DEMO'",
            name="okx_demo_canary_consent_target_check",
        ),
        CheckConstraint(
            "instrument_id = 'BTC-USDT-SWAP'",
            name="okx_demo_canary_consent_instrument_check",
        ),
        CheckConstraint(
            "provenance = 'CONTROLLED_CANARY_NON_PRODUCTION'",
            name="okx_demo_canary_consent_provenance_check",
        ),
        CheckConstraint(
            "status IN ('REQUESTED','FINALIZED','GRANT_ISSUED',"
            "'CONSUMED','REVOKED','FAILED','EXPIRED')",
            name="okx_demo_canary_consent_status_check",
        ),
        CheckConstraint(
            "max_notional > 0 AND max_notional <= 20",
            name="okx_demo_canary_consent_risk_check",
        ),
        CheckConstraint(
            "consented_at < consent_deadline_at",
            name="okx_demo_canary_consent_deadline_check",
        ),
        CheckConstraint(
            "length(source_fingerprint) = 64",
            name="okx_demo_canary_consent_source_fingerprint_check",
        ),
        CheckConstraint(
            "length(idempotency_key_digest) = 64 AND length(consent_nonce) = 64 "
            "AND length(consent_payload_digest) = 64 AND length(consent_digest) = 64",
            name="okx_demo_canary_consent_proof_identity_check",
        ),
        CheckConstraint(
            "(status IN ('REQUESTED','EXPIRED') AND runtime_instance_id IS NULL "
            "AND reconciliation_run_id IS NULL AND audit_job_id IS NULL "
            "AND approval_id IS NULL AND grant_id IS NULL AND finalized_at IS NULL) OR "
            "(status IN ('FINALIZED','GRANT_ISSUED','CONSUMED','REVOKED','FAILED','EXPIRED') "
            "AND runtime_instance_id IS NOT NULL "
            "AND reconciliation_run_id IS NOT NULL AND audit_job_id IS NOT NULL "
            "AND approval_id IS NOT NULL AND finalized_at IS NOT NULL)",
            name="okx_demo_canary_consent_status_shape_check",
        ),
        UniqueConstraint(
            "source_job_id",
            name="okx_demo_canary_consent_source_unique",
        ),
        UniqueConstraint(
            "idempotency_key_digest",
            name="okx_demo_canary_consent_idempotency_unique",
        ),
        UniqueConstraint(
            "consent_digest",
            name="okx_demo_canary_consent_digest_unique",
        ),
        UniqueConstraint(
            "consent_nonce",
            name="okx_demo_canary_consent_nonce_unique",
        ),
    )

    handoff_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    execution_target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_job_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey(
            "research_jobs.id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=False,
    )
    source_ancestry: Mapped[list] = mapped_column(JSON, nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    consent_nonce: Mapped[str] = mapped_column(String(64), nullable=False)
    consent_payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    consent_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    provenance: Mapped[str] = mapped_column(String(48), nullable=False)
    instrument_id: Mapped[str] = mapped_column(String(80), nullable=False)
    max_notional: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    runtime_instance_id: Mapped[Optional[str]] = mapped_column(String(64))
    reconciliation_run_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey(
            "reconciliation_runs.id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
    )
    attested_session_id: Mapped[Optional[str]] = mapped_column(String(64))
    snapshot_binding: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    bundle_digest: Mapped[Optional[str]] = mapped_column(String(64))
    bundle_observed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    bundle_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    audit_job_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey(
            "research_jobs.id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
    )
    full_chain_run_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey(
            "full_chain_runs.id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
    )
    approval_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey(
            "approved_executions.id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
    )
    grant_id: Mapped[Optional[str]] = mapped_column(
        String(32),
        ForeignKey(
            "okx_demo_submission_grants.grant_id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        unique=True,
    )
    consented_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consent_deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finalized_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[Optional[str]] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
PLACEMENT_PREDICATE = "operation = 'PLACE'"


class OkxDemoSubmissionGrant(Base):
    """Durable, single-use capability consumed by the canonical writer."""

    __tablename__ = "okx_demo_submission_grants"
    __table_args__ = (
        CheckConstraint(
            "execution_target_id = 'OKX_DEMO'",
            name="okx_demo_submission_grants_target_check",
        ),
        CheckConstraint(
            "status IN ('ACTIVE', 'CONSUMED', 'EXPIRED', 'FAILED')",
            name="okx_demo_submission_grants_status_check",
        ),
        CheckConstraint(
            "length(grant_id) = 32 AND length(canonical_hash) = 64 "
            "AND length(policy_digest) = 64 "
            "AND length(approved_payload_hash) = 64 "
            "AND length(request_digest) = 64",
            name="okx_demo_submission_grants_digest_check",
        ),
        CheckConstraint(
            "provenance = 'CONTROLLED_CANARY_NON_PRODUCTION'",
            name="okx_demo_submission_grants_provenance_check",
        ),
        CheckConstraint(
            "expires_at > issued_at",
            name="okx_demo_submission_grants_time_check",
        ),
        CheckConstraint(
            "canary_quantity > 0 AND canary_notional > 0 "
            "AND canary_notional <= 20",
            name="okx_demo_submission_grants_risk_check",
        ),
        UniqueConstraint(
            "approval_id",
            name="okx_demo_submission_grants_approval_unique",
        ),
        Index(
            "okx_demo_submission_grants_one_active_target_idx",
            "execution_target_id",
            unique=True,
            sqlite_where=text("status = 'ACTIVE'"),
            postgresql_where=text("status = 'ACTIVE'"),
        ),
    )

    grant_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    handoff_id: Mapped[Optional[str]] = mapped_column(
        String(32),
        ForeignKey(
            "okx_demo_canary_consent_handoffs.handoff_id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        unique=True,
    )
    execution_target_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("execution_scopes.scope_id"),
        nullable=False,
    )
    approval_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("approved_executions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    reconciliation_run_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("reconciliation_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    canonical_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    approved_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    client_order_id: Mapped[str] = mapped_column(String(32), nullable=False)
    instrument_id: Mapped[str] = mapped_column(String(80), nullable=False)
    canary_quantity: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    canary_notional: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    provenance: Mapped[str] = mapped_column(String(48), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    writer_instance_id: Mapped[Optional[str]] = mapped_column(String(64))
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class OkxDemoCanaryLifecycle(Base):
    __tablename__ = "okx_demo_canary_lifecycles"
    __table_args__ = (
        CheckConstraint("execution_target_id = 'OKX_DEMO'", name="okx_demo_canary_lifecycle_target_check"),
        CheckConstraint("outcome IN ('PENDING', 'PASSED', 'FAILED')", name="okx_demo_canary_lifecycle_outcome_check"),
        CheckConstraint("cleanup_phase IN ('ARMED','OPENING_SUBMITTED','CANCEL_PENDING','CLEANUP_PENDING','RECOVERY_EXHAUSTED','TERMINAL','REVOKED')", name="okx_demo_canary_lifecycle_phase_check"),
        CheckConstraint("deadline_at > created_at AND max_quantity > 0 AND baseline_position_quantity = 0 AND attributed_fill_quantity >= 0 AND attributed_fill_quantity <= max_quantity", name="okx_demo_canary_lifecycle_risk_check"),
        CheckConstraint("length(lifecycle_id)=32 AND length(baseline_evidence_digest)=64 AND (opening_order_identity_digest IS NULL OR length(opening_order_identity_digest)=64) AND (fill_attribution_digest IS NULL OR length(fill_attribution_digest)=64) AND (final_evidence_digest IS NULL OR length(final_evidence_digest)=64)", name="okx_demo_canary_lifecycle_digest_check"),
        CheckConstraint("(cleanup_phase='ARMED' AND opening_exchange_order_row_id IS NULL AND opening_order_identity_digest IS NULL AND attributed_fill_quantity=0 AND cleanup_exchange_order_row_id IS NULL AND outcome='PENDING') OR cleanup_phase<>'ARMED'", name="okx_demo_canary_lifecycle_armed_shape_check"),
        CheckConstraint("cleanup_phase NOT IN ('OPENING_SUBMITTED','CANCEL_PENDING','CLEANUP_PENDING','RECOVERY_EXHAUSTED') OR (opening_exchange_order_row_id IS NOT NULL AND opening_order_identity_digest IS NOT NULL)", name="okx_demo_canary_lifecycle_active_shape_check"),
        CheckConstraint("cleanup_phase NOT IN ('CLEANUP_PENDING','RECOVERY_EXHAUSTED') OR outcome='FAILED'", name="okx_demo_canary_lifecycle_cleanup_failed_check"),
        CheckConstraint("cleanup_phase<>'TERMINAL' OR (outcome IN ('PASSED','FAILED') AND terminal_at IS NOT NULL AND revoked_at IS NOT NULL AND final_reconciliation_run_id IS NOT NULL AND final_evidence_digest IS NOT NULL)", name="okx_demo_canary_lifecycle_terminal_shape_check"),
        CheckConstraint("cleanup_phase<>'REVOKED' OR (outcome='FAILED' AND opening_exchange_order_row_id IS NULL AND attributed_fill_quantity=0 AND terminal_at IS NOT NULL AND revoked_at IS NOT NULL AND final_reconciliation_run_id IS NOT NULL AND final_evidence_digest IS NOT NULL)", name="okx_demo_canary_lifecycle_revoked_shape_check"),
        CheckConstraint("cleanup_phase IN ('TERMINAL','REVOKED') OR (terminal_at IS NULL AND revoked_at IS NULL)", name="okx_demo_canary_lifecycle_nonterminal_shape_check"),
        CheckConstraint("(outcome='FAILED')=(failure_code IS NOT NULL)", name="okx_demo_canary_lifecycle_failure_code_check"),
        CheckConstraint("attributed_fill_quantity=0 OR (outcome='FAILED' AND fill_attribution_digest IS NOT NULL)", name="okx_demo_canary_lifecycle_fill_failure_check"),
        CheckConstraint("(cleanup_trade_intent_id IS NULL)=(cleanup_approval_id IS NULL) AND (cleanup_exchange_order_row_id IS NULL OR cleanup_trade_intent_id IS NOT NULL)", name="okx_demo_canary_lifecycle_cleanup_pair_check"),
        CheckConstraint("fencing_version>=1", name="okx_demo_canary_lifecycle_fencing_check"),
        UniqueConstraint("submission_grant_id", name="okx_demo_canary_lifecycle_submission_unique"),
        UniqueConstraint("opening_exchange_order_row_id", name="okx_demo_canary_lifecycle_opening_order_unique"),
        UniqueConstraint("cleanup_exchange_order_row_id", name="okx_demo_canary_lifecycle_cleanup_order_unique"),
        Index("okx_demo_canary_lifecycle_one_unfinished_idx", "execution_target_id", unique=True, sqlite_where=text("cleanup_phase != 'TERMINAL' AND cleanup_phase != 'REVOKED'"), postgresql_where=text("cleanup_phase != 'TERMINAL' AND cleanup_phase != 'REVOKED'")),
    )
    lifecycle_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    execution_target_id: Mapped[str] = mapped_column(String(64), ForeignKey("execution_scopes.scope_id"), nullable=False)
    submission_grant_id: Mapped[str] = mapped_column(String(32), ForeignKey("okx_demo_submission_grants.grant_id", ondelete="RESTRICT"), nullable=False)
    opening_approval_id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer,"sqlite"), ForeignKey("approved_executions.id", ondelete="RESTRICT"), nullable=False)
    opening_trade_intent_id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer,"sqlite"), ForeignKey("trade_intents.id", ondelete="RESTRICT"), nullable=False)
    opening_exchange_order_row_id: Mapped[Optional[int]] = mapped_column(BigInteger().with_variant(Integer,"sqlite"), ForeignKey("exchange_orders.id", ondelete="RESTRICT"))
    baseline_reconciliation_run_id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer,"sqlite"), ForeignKey("reconciliation_runs.id", ondelete="RESTRICT"), nullable=False)
    baseline_position_quantity: Mapped[Decimal] = mapped_column(Numeric(36,18), nullable=False)
    baseline_evidence_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    opening_order_identity_digest: Mapped[Optional[str]] = mapped_column(String(64))
    fill_attribution_digest: Mapped[Optional[str]] = mapped_column(String(64))
    attributed_fill_quantity: Mapped[Decimal] = mapped_column(Numeric(36,18), nullable=False)
    max_quantity: Mapped[Decimal] = mapped_column(Numeric(36,18), nullable=False)
    cleanup_trade_intent_id: Mapped[Optional[int]] = mapped_column(BigInteger().with_variant(Integer,"sqlite"), ForeignKey("trade_intents.id", ondelete="RESTRICT"))
    cleanup_approval_id: Mapped[Optional[int]] = mapped_column(BigInteger().with_variant(Integer,"sqlite"), ForeignKey("approved_executions.id", ondelete="RESTRICT"))
    cleanup_exchange_order_row_id: Mapped[Optional[int]] = mapped_column(BigInteger().with_variant(Integer,"sqlite"), ForeignKey("exchange_orders.id", ondelete="RESTRICT"))
    final_reconciliation_run_id: Mapped[Optional[int]] = mapped_column(BigInteger().with_variant(Integer,"sqlite"), ForeignKey("reconciliation_runs.id", ondelete="RESTRICT"))
    final_evidence_digest: Mapped[Optional[str]] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    cleanup_phase: Mapped[str] = mapped_column(String(24), nullable=False)
    failure_code: Mapped[Optional[str]] = mapped_column(String(80))
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    terminal_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    fencing_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


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
