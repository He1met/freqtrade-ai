from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger,
    LargeBinary,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
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
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.ext.compiler import compiles

from app.models.base import Base


OKX_DEMO_TARGET_ID = "OKX_DEMO"
LOCAL_DRY_RUN_SCOPE_ID = "LOCAL_DRY_RUN"
UNKNOWN_LEGACY_SCOPE_ID = "UNKNOWN_LEGACY"


class ClientOrderIdFormatExpression(ColumnElement):
    inherit_cache = True
    type = Boolean()


class Sha256FormatExpression(ColumnElement):
    inherit_cache = True
    type = Boolean()

    def __init__(self, column_name: str, nullable: bool = True) -> None:
        self.column_name = column_name
        self.nullable = nullable


@compiles(Sha256FormatExpression)
@compiles(Sha256FormatExpression, "postgresql")
def _compile_postgresql_sha256_format(
    element, _compiler, **_kwargs
) -> str:
    predicate = "{} ~ '^[0-9a-f]{{64}}$'".format(element.column_name)
    return (
        "{0} IS NULL OR {1}".format(element.column_name, predicate)
        if element.nullable
        else predicate
    )


@compiles(Sha256FormatExpression, "sqlite")
def _compile_sqlite_sha256_format(element, _compiler, **_kwargs) -> str:
    predicate = (
        "length({0}) = 64 AND {0} NOT GLOB '*[^0-9a-f]*'".format(
            element.column_name
        )
    )
    return (
        "{0} IS NULL OR ({1})".format(element.column_name, predicate)
        if element.nullable
        else predicate
    )


@compiles(ClientOrderIdFormatExpression)
@compiles(ClientOrderIdFormatExpression, "postgresql")
def _compile_postgresql_client_order_id_format(_element, _compiler, **_kwargs) -> str:
    return "client_order_id ~ '^[A-Za-z0-9]{1,32}$'"


@compiles(ClientOrderIdFormatExpression, "sqlite")
def _compile_sqlite_client_order_id_format(_element, _compiler, **_kwargs) -> str:
    return (
        "length(client_order_id) BETWEEN 1 AND 32 "
        "AND client_order_id NOT GLOB '*[^A-Za-z0-9]*'"
    )


class ExecutionScope(Base):
    __tablename__ = "execution_scopes"
    __table_args__ = (
        CheckConstraint(
            "scope_kind IN ('EXCHANGE_TARGET', 'NON_EXCHANGE', 'LEGACY')",
            name="execution_scopes_kind_check",
        ),
        CheckConstraint(
            "scope_id = 'OKX_DEMO' AND scope_kind = 'EXCHANGE_TARGET' "
            "AND exchange_capable = TRUE AND executable = FALSE "
            "AND exchange_writes = FALSE AND order_submission_authorized = FALSE OR "
            "scope_id = 'LOCAL_DRY_RUN' AND scope_kind = 'NON_EXCHANGE' "
            "AND exchange_capable = FALSE AND executable = TRUE "
            "AND exchange_writes = FALSE AND order_submission_authorized = FALSE OR "
            "scope_id = 'UNKNOWN_LEGACY' AND scope_kind = 'LEGACY' "
            "AND exchange_capable = FALSE AND executable = FALSE "
            "AND exchange_writes = FALSE AND order_submission_authorized = FALSE",
            name="execution_scopes_known_contract_check",
        ),
    )

    scope_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    exchange_capable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    executable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    exchange_writes: Mapped[bool] = mapped_column(Boolean, nullable=False)
    order_submission_authorized: Mapped[bool] = mapped_column(Boolean, nullable=False)
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
            ClientOrderIdFormatExpression(),
            name="trade_intents_client_order_id_format_check",
        ),
        UniqueConstraint(
            "execution_target_id",
            "client_order_id",
            name="trade_intents_target_client_order_unique",
        ),
        UniqueConstraint(
            "execution_target_id",
            "idempotency_key_digest",
            name="trade_intents_target_idempotency_unique",
        ),
        UniqueConstraint(
            "id",
            "intent_id",
            "client_order_id",
            "status",
            name="trade_intents_approval_identity_unique",
        ),
        UniqueConstraint(
            "id",
            "authorization_schema_version",
            "canonical_hash",
            "policy_digest",
            "approved_payload_hash",
            name="trade_intents_approved_payload_unique",
        ),
        CheckConstraint(
            "status IN ('UNKNOWN_LEGACY', 'PENDING_RISK', 'APPROVED', "
            "'REJECTED', 'BLOCKED', 'EXPIRED')",
            name="trade_intents_status_check",
        ),
        CheckConstraint(
            "authorization_schema_version IN ('LEGACY', 'RISK_V1')",
            name="trade_intents_authorization_schema_check",
        ),
        CheckConstraint(
            "authorization_" "schema_version = 'LEGACY' "
            "AND policy_digest IS NULL "
            "AND status IN ('UNKNOWN_LEGACY', 'BLOCKED') "
            "OR authorization_" "schema_version = 'RISK_V1' "
            "AND policy_digest IS NOT NULL "
            "AND canonical_hash IS NOT NULL "
            "AND idempotency_key_digest IS NOT NULL "
            "AND intent_id IS NOT NULL",
            name="trade_intents_scope_contract_check",
        ),
        CheckConstraint(
            Sha256FormatExpression("intent_id"),
            name="trade_intents_intent_id_format_check",
        ),
        CheckConstraint(
            Sha256FormatExpression("canonical_hash"),
            name="trade_intents_canonical_hash_format_check",
        ),
        CheckConstraint(
            Sha256FormatExpression("policy_digest"),
            name="trade_intents_policy_digest_format_check",
        ),
        CheckConstraint(
            Sha256FormatExpression("idempotency_key_digest"),
            name="trade_intents_idempotency_digest_format_check",
        ),
        CheckConstraint(
            "authorization_" "schema_version = 'LEGACY' OR status = 'BLOCKED' "
            "OR side IN ('buy', 'sell')",
            name="trade_intents_side_check",
        ),
        CheckConstraint(
            "authorization_" "schema_version = 'LEGACY' OR status = 'BLOCKED' "
            "OR position_side = 'net'",
            name="trade_intents_position_side_check",
        ),
        CheckConstraint(
            "authorization_" "schema_version = 'LEGACY' OR status = 'BLOCKED' "
            "OR margin_mode = 'isolated'",
            name="trade_intents_margin_mode_check",
        ),
        CheckConstraint(
            "authorization_" "schema_version = 'LEGACY' OR status = 'BLOCKED' "
            "OR order_type IN ('limit', 'market')",
            name="trade_intents_order_type_check",
        ),
        CheckConstraint(
            "authorization_" "schema_version = 'LEGACY' OR status = 'BLOCKED' "
            "OR order_type = 'market' AND limit_price IS NULL "
            "OR order_type = 'limit' AND limit_price > 0",
            name="trade_intents_order_combo_check",
        ),
        Index("trade_intents_target_status_idx", "execution_target_id", "status"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    execution_target_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("execution_scopes.scope_id"), nullable=False
    )
    authorization_schema_version: Mapped[str] = mapped_column(
        String(16), nullable=False, default="LEGACY"
    )
    intent_id: Mapped[Optional[str]] = mapped_column(String(64), unique=True)
    canonical_hash: Mapped[Optional[str]] = mapped_column(String(64))
    policy_digest: Mapped[Optional[str]] = mapped_column(String(64))
    approved_payload_hash: Mapped[Optional[str]] = mapped_column(String(64))
    idempotency_key_digest: Mapped[Optional[str]] = mapped_column(String(64))
    client_order_id: Mapped[str] = mapped_column(String(32), nullable=False)
    strategy_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), ForeignKey("strategies.id")
    )
    strategy_version_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("strategy_versions.id", ondelete="SET NULL"),
    )
    backtest_run_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), ForeignKey("backtest_runs.id")
    )
    backtest_result_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), ForeignKey("backtest_results.id")
    )
    strategy_score_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), ForeignKey("strategy_scores.id")
    )
    instrument_id: Mapped[Optional[str]] = mapped_column(String(80))
    side: Mapped[Optional[str]] = mapped_column(String(16))
    position_side: Mapped[Optional[str]] = mapped_column(String(16))
    order_type: Mapped[Optional[str]] = mapped_column(String(32))
    quantity: Mapped[Optional[Decimal]] = mapped_column(Numeric(36, 18))
    limit_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(36, 18))
    reference_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(36, 18))
    leverage: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 8))
    margin_mode: Mapped[Optional[str]] = mapped_column(String(16))
    stop_loss: Mapped[Optional[Decimal]] = mapped_column(Numeric(36, 18))
    take_profit: Mapped[Optional[Decimal]] = mapped_column(Numeric(36, 18))
    reduce_only: Mapped[Optional[bool]] = mapped_column(Boolean)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="UNKNOWN_LEGACY"
    )
    request_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
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
        CheckConstraint(
            "authorization_" "schema_version = 'RISK_V1' OR "
            "authorization_" "schema_version = 'LEGACY' AND decision = 'BLOCKED'",
            name="risk_decisions_authorization_schema_check",
        ),
        CheckConstraint(
            Sha256FormatExpression("policy_digest"),
            name="risk_decisions_policy_digest_format_check",
        ),
        CheckConstraint(
            "decision IN ('APPROVED', 'REJECTED', 'BLOCKED', 'EXPIRED')",
            name="risk_decisions_decision_check",
        ),
        UniqueConstraint("trade_intent_id", name="risk_decisions_trade_intent_unique"),
        UniqueConstraint(
            "id",
            "trade_intent_id",
            "decision",
            "authorization_schema_version",
            "policy_digest",
            name="risk_decisions_id_intent_unique",
        ),
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
    authorization_schema_version: Mapped[str] = mapped_column(
        String(16), nullable=False, default="LEGACY"
    )
    policy_digest: Mapped[Optional[str]] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(80), nullable=False)
    evidence_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RiskBudget(Base):
    __tablename__ = "risk_budgets"
    __table_args__ = (
        CheckConstraint(
            "reserved_notional >= 0 AND approved_positions >= 0",
            name="risk_budgets_nonnegative_check",
        ),
    )

    execution_target_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("execution_scopes.scope_id"), primary_key=True
    )
    reserved_notional: Mapped[Decimal] = mapped_column(
        Numeric(36, 18), nullable=False, default=Decimal("0")
    )
    approved_positions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ApprovedExecution(Base):
    __tablename__ = "approved_executions"
    __table_args__ = (
        UniqueConstraint("trade_intent_id", name="approved_executions_intent_unique"),
        UniqueConstraint("risk_decision_id", name="approved_executions_decision_unique"),
        ForeignKeyConstraint(
            ["trade_intent_id", "intent_id", "client_order_id", "intent_status"],
            [
                "trade_intents.id",
                "trade_intents.intent_id",
                "trade_intents.client_order_id",
                "trade_intents.status",
            ],
            name="approved_executions_intent_identity_fkey",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["instrument_snapshot_id"],
            ["okx_demo_trusted_snapshots.snapshot_id"],
            name="approved_executions_instrument_snapshot_fkey",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["market_snapshot_id"],
            ["okx_demo_trusted_snapshots.snapshot_id"],
            name="approved_executions_market_snapshot_fkey",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["account_snapshot_id"],
            ["okx_demo_trusted_snapshots.snapshot_id"],
            name="approved_executions_account_snapshot_fkey",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "risk_decision_id",
                "trade_intent_id",
                "decision",
                "authorization_schema_version",
                "policy_digest",
            ],
            [
                "risk_decisions.id",
                "risk_decisions.trade_intent_id",
                "risk_decisions.decision",
                "risk_decisions.authorization_schema_version",
                "risk_decisions.policy_digest",
            ],
            name="approved_executions_decision_intent_fkey",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            [
                "trade_intent_id",
                "authorization_schema_version",
                "canonical_hash",
                "policy_digest",
                "approved_payload_hash",
            ],
            [
                "trade_intents.id",
                "trade_intents.authorization_schema_version",
                "trade_intents.canonical_hash",
                "trade_intents.policy_digest",
                "trade_intents.approved_payload_hash",
            ],
            name="approved_executions_payload_identity_fkey",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "execution_target_id = 'OKX_DEMO'",
            name="approved_executions_okx_demo_target_check",
        ),
        CheckConstraint(
            "authorization_" "schema_version = 'RISK_V1'",
            name="approved_executions_authorization_schema_check",
        ),
        CheckConstraint(
            "order_submission_authorized = FALSE",
            name="approved_executions_no_submission_check",
        ),
        CheckConstraint(
            "claim_required = TRUE",
            name="approved_executions_claim_required_check",
        ),
        CheckConstraint(
            "status IN ('ACTIVE', 'EXPIRED')",
            name="approved_executions_status_check",
        ),
        CheckConstraint(
            "decision = 'APPROVED' AND intent_status = 'APPROVED'",
            name="approved_executions_approved_state_check",
        ),
        CheckConstraint(
            "reserved_notional > 0",
            name="approved_executions_reservation_check",
        ),
        CheckConstraint(
            ClientOrderIdFormatExpression(),
            name="approved_executions_client_order_id_format_check",
        ),
        CheckConstraint(
            Sha256FormatExpression("intent_id", nullable=False),
            name="approved_executions_intent_id_format_check",
        ),
        CheckConstraint(
            Sha256FormatExpression("canonical_hash", nullable=False),
            name="approved_executions_canonical_hash_format_check",
        ),
        CheckConstraint(
            Sha256FormatExpression("policy_digest", nullable=False),
            name="approved_executions_policy_digest_format_check",
        ),
        CheckConstraint(
            Sha256FormatExpression("approved_payload_hash", nullable=False),
            name="approved_executions_payload_hash_format_check",
        ),
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
    risk_decision_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("risk_decisions.id", ondelete="CASCADE"),
        nullable=False,
    )
    intent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    client_order_id: Mapped[str] = mapped_column(String(32), nullable=False)
    authorization_schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    canonical_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    approved_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    instrument_snapshot_id: Mapped[str] = mapped_column(String(80), nullable=False)
    market_snapshot_id: Mapped[str] = mapped_column(String(80), nullable=False)
    account_snapshot_id: Mapped[str] = mapped_column(String(80), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False, default="APPROVED")
    intent_status: Mapped[str] = mapped_column(String(32), nullable=False, default="APPROVED")
    reserved_notional: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    order_submission_authorized: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    claim_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    evidence_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class OkxDemoAttestedSession(Base):
    __tablename__ = "okx_demo_attested_sessions"
    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "execution_target_id",
            "pinned_fingerprint_sha256",
            "expires_at",
            name="okx_demo_attested_sessions_snapshot_identity_unique",
        ),
        UniqueConstraint(
            "attestation_nonce",
            name="okx_demo_attested_sessions_nonce_unique",
        ),
        CheckConstraint(
            "execution_target_id = 'OKX_DEMO'",
            name="okx_demo_attested_sessions_target_check",
        ),
        CheckConstraint(
            Sha256FormatExpression("pinned_fingerprint_sha256", nullable=False),
            name="okx_demo_attested_sessions_fingerprint_format_check",
        ),
        CheckConstraint(
            Sha256FormatExpression("capability_proof_digest", nullable=False),
            name="okx_demo_attested_sessions_proof_format_check",
        ),
        CheckConstraint(
            "created_at < expires_at AND "
            "(revoked_at IS NULL AND revoke_reason IS NULL OR "
            "revoked_at >= created_at AND revoke_reason IN "
            "('IDENTITY_DRIFT', 'EXPIRED', 'FACTORY_CLOSE', 'WRITE_FAILURE'))",
            name="okx_demo_attested_sessions_time_check",
        ),
    )

    session_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    execution_target_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("execution_scopes.scope_id"), nullable=False
    )
    pinned_fingerprint_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    capability_proof_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    attestation_nonce: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    revoke_reason: Mapped[Optional[str]] = mapped_column(String(32))


class OkxDemoAttestationSecret(Base):
    __tablename__ = "okx_demo_attestation_secrets"
    __table_args__ = (
        CheckConstraint(
            "secret_" "id = 'ACTIVE' AND octet_length(hmac_key) = 32",
            name="okx_demo_attestation_secrets_contract_check",
        ),
    )

    secret_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    hmac_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class OkxDemoTrustedSnapshot(Base):
    __tablename__ = "okx_demo_trusted_snapshots"
    __table_args__ = (
        UniqueConstraint("snapshot_id", name="okx_demo_trusted_snapshots_id_unique"),
        CheckConstraint(
            "kind IN ('instrument', 'market', 'account')",
            name="okx_demo_trusted_snapshots_kind_check",
        ),
        CheckConstraint(
            "execution_target_id = 'OKX_DEMO'",
            name="okx_demo_trusted_snapshots_target_check",
        ),
        CheckConstraint(
            "source_type = 'api_aggregate' AND core_data = TRUE",
            name="okx_demo_trusted_snapshots_source_check",
        ),
        CheckConstraint(
            Sha256FormatExpression("digest", nullable=False),
            name="okx_demo_trusted_snapshots_digest_format_check",
        ),
        CheckConstraint(
            Sha256FormatExpression("attestation_fingerprint_sha256", nullable=False),
            name="okx_demo_trusted_snapshots_fingerprint_format_check",
        ),
        CheckConstraint(
            "observed_at < expires_at AND expires_at <= attested_session_expires_at",
            name="okx_demo_trusted_snapshots_time_check",
        ),
        ForeignKeyConstraint(
            [
                "attested_session_id",
                "execution_target_id",
                "attestation_fingerprint_sha256",
                "attested_session_expires_at",
            ],
            [
                "okx_demo_attested_sessions.session_id",
                "okx_demo_attested_sessions.execution_target_id",
                "okx_demo_attested_sessions.pinned_fingerprint_sha256",
                "okx_demo_attested_sessions.expires_at",
            ],
            name="okx_demo_trusted_snapshots_session_fkey",
            ondelete="RESTRICT",
        ),
    )

    database_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    snapshot_id: Mapped[str] = mapped_column(String(80), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    execution_target_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("execution_scopes.scope_id"), nullable=False
    )
    content_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="api_aggregate"
    )
    core_data: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    attested_session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    attestation_fingerprint_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    attested_session_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
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
            ClientOrderIdFormatExpression(),
            name="exchange_orders_client_order_id_format_check",
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
            "execution_scope_id = 'LOCAL_DRY_RUN' OR executable_evidence = FALSE",
            name="execution_manifests_authorization_check",
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
