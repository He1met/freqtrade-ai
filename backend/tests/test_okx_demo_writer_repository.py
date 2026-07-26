from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.adapters.okx_demo.models import InstrumentSpec
from app.adapters.okx_demo.write_semantics import OkxDemoWriteBlocked
from app.adapters.okx_demo.writer_models import (
    OrderSubmissionAuthorization,
    normalize_order_command,
)
from app.adapters.okx_demo.writer_repository import SqlAlchemyOrderWriterStore
from app.adapters.okx_demo.writer_state import WriteEvent, WriteState
from app.models import Base
from app.models.execution_lineage import (
    ApprovedExecution,
    OkxDemoAttestedSession,
    OkxDemoTrustedSnapshot,
    RiskDecision,
    RiskBudget,
    TradeIntent,
)
from app.models.order_writer import OkxOrderWriteAttempt, OkxOrderWriterLease
from app.repositories.execution_lineage import ensure_execution_scope_catalog
from app.services.risk_chain import (
    _issue_attested_session_capability,
    _normalize_attested_snapshot,
    _write_attested_snapshot,
    canonical_digest,
)


NOW = datetime(2026, 7, 27, tzinfo=timezone.utc)


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine) as session:
        ensure_execution_scope_catalog(session)
        capability = _issue_attested_session_capability(
            attestation_hmac_key=b"t" * 32,
            pinned_fingerprint_sha256="f" * 64,
            created_at=NOW - timedelta(seconds=1),
            expires_at=NOW + timedelta(minutes=10),
        )
        snapshots = {}
        for kind in ("instrument", "market", "account"):
            content = {
                "execution_target": "OKX_DEMO",
                "source": "okx_demo_rest",
                "resource": kind,
                "stale": False,
                "authenticated": kind == "account",
                "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
            }
            normalized = _normalize_attested_snapshot(
                capability,
                kind=kind,
                content=content,
                observed_at=NOW,
                expires_at=NOW + timedelta(minutes=5),
            )
            snapshots[kind] = _write_attested_snapshot(
                session,
                capability,
                normalized,
                now=NOW,
            )
        snapshot_evidence = {
            kind: {
                "snapshot_id": row.snapshot_id,
                "database_id": row.database_id,
                "digest": row.digest,
                "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
            }
            for kind, row in snapshots.items()
        }
        canonical_input = {"request": "repository-test"}
        canonical_hash = canonical_digest(canonical_input)
        lineage = {"test_lineage": True}
        notional = Decimal("11.4")
        approved_payload_hash = canonical_digest(
            {
                "authorization_schema_version": "RISK_V1",
                "canonical_hash": canonical_hash,
                "policy_digest": "3" * 64,
                "lineage": lineage,
                "snapshots": snapshot_evidence,
                "order": {
                    "instrument_id": "BTC-USDT-SWAP",
                    "side": "buy",
                    "position_side": "net",
                    "order_type": "limit",
                    "quantity": Decimal("0.02"),
                    "limit_price": Decimal("57000"),
                    "reference_price": Decimal("57000"),
                    "leverage": Decimal("3"),
                    "margin_mode": "isolated",
                    "stop_loss": Decimal("55000"),
                    "take_profit": Decimal("60000"),
                    "reduce_only": False,
                    "notional": notional,
                },
            }
        )
        intent = TradeIntent(
            execution_target_id="OKX_DEMO",
            authorization_schema_version="RISK_V1",
            intent_id="1" * 64,
            canonical_hash=canonical_hash,
            policy_digest="3" * 64,
            approved_payload_hash=approved_payload_hash,
            idempotency_key_digest="4" * 64,
            client_order_id="WriterOrder001",
            instrument_id="BTC-USDT-SWAP",
            side="buy",
            position_side="net",
            order_type="limit",
            quantity=Decimal("0.02"),
            limit_price=Decimal("57000"),
            reference_price=Decimal("57000"),
            leverage=Decimal("3"),
            margin_mode="isolated",
            stop_loss=Decimal("55000"),
            take_profit=Decimal("60000"),
            reduce_only=False,
            status="APPROVED",
            request_snapshot={
                "canonical_input": canonical_input,
                "snapshot_evidence": snapshot_evidence,
            },
            expires_at=NOW + timedelta(minutes=5),
        )
        session.add(intent)
        session.flush()
        decision = RiskDecision(
            execution_target_id="OKX_DEMO",
            trade_intent_id=intent.id,
            authorization_schema_version="RISK_V1",
            policy_digest=intent.policy_digest,
            decision="APPROVED",
            policy_version="risk-v1",
            evidence_snapshot={
                "lineage": lineage,
                "notional": format(notional, "f"),
            },
        )
        session.add(decision)
        session.flush()
        approval = ApprovedExecution(
            execution_target_id="OKX_DEMO",
            trade_intent_id=intent.id,
            risk_decision_id=decision.id,
            intent_id=intent.intent_id,
            client_order_id=intent.client_order_id,
            authorization_schema_version="RISK_V1",
            canonical_hash=intent.canonical_hash,
            policy_digest=intent.policy_digest,
            approved_payload_hash=intent.approved_payload_hash,
            instrument_snapshot_id=snapshot_evidence["instrument"]["snapshot_id"],
            market_snapshot_id=snapshot_evidence["market"]["snapshot_id"],
            account_snapshot_id=snapshot_evidence["account"]["snapshot_id"],
            decision="APPROVED",
            intent_status="APPROVED",
            reserved_notional=notional,
            order_submission_authorized=False,
            claim_required=True,
            status="ACTIVE",
            expires_at=NOW + timedelta(minutes=5),
            evidence_snapshot={},
            created_at=NOW,
        )
        session.add(approval)
        session.add(
            RiskBudget(
                execution_target_id="OKX_DEMO",
                reserved_notional=notional,
                approved_positions=1,
            )
        )
        session.flush()
        approval_id = approval.id
        session.commit()
        yield session, approval_id
    engine.dispose()


def instrument():
    return InstrumentSpec(
        inst_id="BTC-USDT-SWAP",
        inst_type="SWAP",
        base_ccy="BTC",
        quote_ccy="USDT",
        settle_ccy="USDT",
        contract_type="linear",
        contract_value=Decimal("0.01"),
        contract_value_ccy="BTC",
        lot_size=Decimal("0.01"),
        min_size=Decimal("0.01"),
        tick_size=Decimal("0.1"),
        state="live",
    )


def submission_grant(
    approval_id,
    writer="WriterInstance01",
    expires=None,
    canonical_hash="2" * 64,
    policy_digest="3" * 64,
    approved_payload_hash="5" * 64,
):
    return OrderSubmissionAuthorization(
        execution_target_id="OKX_DEMO",
        authorization_schema_version="RISK_V1",
        canonical_hash=canonical_hash,
        policy_digest=policy_digest,
        approved_payload_hash=approved_payload_hash,
        allow_real_funds=False,
        simulated_trading=True,
        order_submission_enabled=True,
        writer_instance_id=writer,
        approval_id=approval_id,
        expires_at=expires or NOW + timedelta(minutes=1),
    )


def claimed_command(store, approval_id):
    claimed = store.load_approved_execution(approval_id)
    return normalize_order_command(
        claimed,
        submission_grant=submission_grant(
            approval_id,
            canonical_hash=claimed.canonical_hash,
            policy_digest=claimed.policy_digest,
            approved_payload_hash=claimed.approved_payload_hash,
        ),
        instrument=instrument(),
        now=NOW,
    )


def acquire(
    store,
    session,
    approval_id,
    *,
    writer_instance_id,
    now,
    expires_at,
):
    approval = session.get(ApprovedExecution, approval_id)
    canonical_hash = approval.canonical_hash
    policy_digest = approval.policy_digest
    approved_payload_hash = approval.approved_payload_hash
    session.commit()
    store.acquire_lease(
        writer_instance_id=writer_instance_id,
        approval_id=approval_id,
        canonical_hash=canonical_hash,
        policy_digest=policy_digest,
        approved_payload_hash=approved_payload_hash,
        now=now,
        expires_at=expires_at,
    )


def test_sqlite_claim_is_approval_backed_and_transitions_are_committed(db) -> None:
    session, approval_id = db
    store = SqlAlchemyOrderWriterStore(session, now_provider=lambda: NOW)
    acquire(
        store,
        session,
        approval_id,
        writer_instance_id="WriterInstance01",
        now=NOW,
        expires_at=NOW + timedelta(minutes=1),
    )
    command = claimed_command(store, approval_id)

    order, prepared = store.prepare_place(
        command,
        operation="PLACE",
        operation_id=command.client_order_id,
        request_digest="a" * 64,
        safe_request_snapshot=command.request_body,
    )
    acknowledged = store.transition(
        prepared,
        event=WriteEvent.ACKNOWLEDGE,
        exchange_order_id="exchange-order-1",
        safe_response_snapshot={"sCode": "0"},
    )
    reconciled = store.transition(
        acknowledged,
        event=WriteEvent.RECONCILE,
        order_state="live",
        safe_response_snapshot={"state": "live"},
    )

    assert order.approval_id == approval_id
    assert reconciled.state == WriteState.RECONCILED
    session.expire_all()
    persisted = session.scalars(select(OkxOrderWriteAttempt)).one()
    assert persisted.state == "RECONCILED"
    assert persisted.attempt_count == 1
    assert persisted.safe_response_snapshot == {"sCode": "0", "state": "live"}


def test_consumed_approval_cannot_create_a_second_placement(db) -> None:
    session, approval_id = db
    store = SqlAlchemyOrderWriterStore(session, now_provider=lambda: NOW)
    acquire(
        store,
        session,
        approval_id,
        writer_instance_id="WriterInstance01",
        now=NOW,
        expires_at=NOW + timedelta(minutes=1),
    )
    command = claimed_command(store, approval_id)
    store.prepare_place(
        command,
        operation="PLACE",
        operation_id=command.client_order_id,
        request_digest="a" * 64,
        safe_request_snapshot=command.request_body,
    )

    with pytest.raises(OkxDemoWriteBlocked, match="claimed"):
        store.prepare_place(
            command,
            operation="PLACE",
            operation_id=command.client_order_id,
            request_digest="a" * 64,
            safe_request_snapshot=command.request_body,
        )

    assert len(session.scalars(select(OkxOrderWriteAttempt)).all()) == 1


def test_residual_cleanup_cannot_exceed_original_approved_quantity(db) -> None:
    session, approval_id = db
    store = SqlAlchemyOrderWriterStore(session, now_provider=lambda: NOW)
    command = claimed_command(store, approval_id)
    approved, intent, decision = store._approval_lineage(
        approval_id,
        for_update=False,
    )
    oversized_cleanup = command.model_copy(
        update={
            "side": intent.side,
            "order_type": "market",
            "contracts": intent.quantity + Decimal("0.01"),
            "limit_price": None,
            "reduce_only": True,
        }
    )

    with pytest.raises(OkxDemoWriteBlocked, match="lineage"):
        store._validate_cleanup_command(
            oversized_cleanup,
            approved,
            intent,
            decision,
        )


def test_database_lease_blocks_contender_and_allows_expired_takeover(db) -> None:
    session, approval_id = db
    first = SqlAlchemyOrderWriterStore(session, now_provider=lambda: NOW)
    acquire(
        first,
        session,
        approval_id,
        writer_instance_id="WriterInstance01",
        now=NOW,
        expires_at=NOW + timedelta(seconds=10),
    )

    contender = SqlAlchemyOrderWriterStore(session, now_provider=lambda: NOW)
    with pytest.raises(OkxDemoWriteBlocked, match="another"):
        acquire(
            contender,
            session,
            approval_id,
            writer_instance_id="WriterInstance01",
            now=NOW,
            expires_at=NOW + timedelta(seconds=20),
        )

    acquire(
        contender,
        session,
        approval_id,
        writer_instance_id="WriterInstance02",
        now=NOW + timedelta(seconds=11),
        expires_at=NOW + timedelta(seconds=30),
    )
    lease = session.get(OkxOrderWriterLease, "OKX_DEMO")
    assert lease.generation == 2


def test_stale_lease_holder_cannot_transition_after_fenced_takeover(db) -> None:
    session, approval_id = db
    first = SqlAlchemyOrderWriterStore(session, now_provider=lambda: NOW)
    acquire(
        first,
        session,
        approval_id,
        writer_instance_id="WriterInstance01",
        now=NOW,
        expires_at=NOW + timedelta(seconds=10),
    )
    command = claimed_command(first, approval_id)
    _order, prepared = first.prepare_place(
        command,
        operation="PLACE",
        operation_id=command.client_order_id,
        request_digest="a" * 64,
        safe_request_snapshot=command.request_body,
    )

    contender = SqlAlchemyOrderWriterStore(
        session,
        now_provider=lambda: NOW + timedelta(seconds=11),
    )
    acquire(
        contender,
        session,
        approval_id,
        writer_instance_id="WriterInstance01",
        now=NOW + timedelta(seconds=11),
        expires_at=NOW + timedelta(seconds=30),
    )
    adopted = contender.unresolved()
    assert adopted.lease_generation == 2

    with pytest.raises(OkxDemoWriteBlocked, match="lease"):
        first.transition(prepared, event=WriteEvent.ACKNOWLEDGE)


def test_expired_approval_is_rejected_before_claim(db) -> None:
    session, approval_id = db
    approval = session.get(ApprovedExecution, approval_id)
    intent_id = approval.trade_intent_id
    decision_id = approval.risk_decision_id
    approval.expires_at = NOW
    session.commit()
    store = SqlAlchemyOrderWriterStore(session, now_provider=lambda: NOW)

    with pytest.raises(OkxDemoWriteBlocked, match="no longer active"):
        store.load_approved_execution(approval_id)

    assert session.get(ApprovedExecution, approval_id) is None
    assert session.get(TradeIntent, intent_id).status == "EXPIRED"
    assert session.get(RiskDecision, decision_id).decision == "EXPIRED"
    budget = session.get(RiskBudget, "OKX_DEMO")
    assert budget.reserved_notional == 0
    assert budget.approved_positions == 0
    assert session.scalars(select(OkxOrderWriterLease)).all() == []
    assert session.scalars(select(OkxOrderWriteAttempt)).all() == []


def test_revoked_attested_session_invalidates_claim_before_lease(db) -> None:
    session, approval_id = db
    store = SqlAlchemyOrderWriterStore(session, now_provider=lambda: NOW)
    claimed = store.load_approved_execution(approval_id)
    approval = session.get(ApprovedExecution, approval_id)
    intent_id = approval.trade_intent_id
    decision_id = approval.risk_decision_id
    snapshot = session.scalars(
        select(OkxDemoTrustedSnapshot).where(
            OkxDemoTrustedSnapshot.snapshot_id
            == approval.instrument_snapshot_id
        )
    ).one()
    attested_session = session.get(
        OkxDemoAttestedSession,
        snapshot.attested_session_id,
    )
    attested_session.revoked_at = NOW
    attested_session.revoke_reason = "FACTORY_CLOSE"
    session.commit()

    with pytest.raises(OkxDemoWriteBlocked, match="no longer active"):
        store.acquire_lease(
            writer_instance_id="WriterInstance01",
            approval_id=approval_id,
            canonical_hash=claimed.canonical_hash,
            policy_digest=claimed.policy_digest,
            approved_payload_hash=claimed.approved_payload_hash,
            now=NOW,
            expires_at=NOW + timedelta(minutes=1),
        )

    assert session.get(ApprovedExecution, approval_id) is None
    assert session.get(TradeIntent, intent_id).status == "BLOCKED"
    assert session.get(RiskDecision, decision_id).decision == "BLOCKED"
    budget = session.get(RiskBudget, "OKX_DEMO")
    assert budget.reserved_notional == 0
    assert budget.approved_positions == 0
    assert session.scalars(select(OkxOrderWriterLease)).all() == []
    assert session.scalars(select(OkxOrderWriteAttempt)).all() == []


def test_invalidated_approval_preserves_existing_write_journal(db) -> None:
    session, approval_id = db
    store = SqlAlchemyOrderWriterStore(session, now_provider=lambda: NOW)
    claimed = store.load_approved_execution(approval_id)
    command = normalize_order_command(
        claimed,
        submission_grant=submission_grant(
            approval_id,
            canonical_hash=claimed.canonical_hash,
            policy_digest=claimed.policy_digest,
            approved_payload_hash=claimed.approved_payload_hash,
        ),
        instrument=instrument(),
        now=NOW,
    )
    acquire(
        store,
        session,
        approval_id,
        writer_instance_id="WriterInstance01",
        now=NOW,
        expires_at=NOW + timedelta(minutes=1),
    )
    _order, prepared = store.prepare_place(
        command,
        operation="PLACE",
        operation_id=command.client_order_id,
        request_digest="a" * 64,
        safe_request_snapshot=command.request_body,
    )
    approval = session.get(ApprovedExecution, approval_id)
    snapshot = session.scalars(
        select(OkxDemoTrustedSnapshot).where(
            OkxDemoTrustedSnapshot.snapshot_id
            == approval.instrument_snapshot_id
        )
    ).one()
    attested_session = session.get(
        OkxDemoAttestedSession,
        snapshot.attested_session_id,
    )
    attested_session.revoked_at = NOW
    attested_session.revoke_reason = "FACTORY_CLOSE"
    session.commit()

    with pytest.raises(OkxDemoWriteBlocked, match="no longer active"):
        store.acquire_lease(
            writer_instance_id="WriterInstance01",
            approval_id=approval_id,
            canonical_hash=claimed.canonical_hash,
            policy_digest=claimed.policy_digest,
            approved_payload_hash=claimed.approved_payload_hash,
            now=NOW,
            expires_at=NOW + timedelta(minutes=1),
        )

    assert session.get(ApprovedExecution, approval_id) is None
    persisted = session.get(OkxOrderWriteAttempt, prepared.attempt_id)
    assert persisted is not None
    assert persisted.approval_id == approval_id
    assert persisted.state == "PREPARED"
