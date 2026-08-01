from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

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
from app.models.full_chain import FullChainRun, FullChainStageRun
from app.models.execution_lineage import (
    ApprovedExecution,
    OkxDemoAttestedSession,
    OkxDemoTrustedSnapshot,
    ResearchJobAttempt,
    ReconciliationRun,
    RiskDecision,
    RiskBudget,
    TradeIntent,
)
from app.models.order_writer import (
    OkxDemoSubmissionGrant,
    OkxOrderWriteAttempt,
    OkxOrderWriterLease,
)
from app.models.okx_demo_reconciliation import OkxDemoReconciliationState
from app.models.research_job import ResearchJob
from app.repositories.execution_lineage import ensure_execution_scope_catalog
from app.services.risk_chain import (
    _issue_attested_session_capability,
    _normalize_attested_snapshot,
    _write_attested_snapshot,
    canonical_digest,
)
from app.services.okx_demo_submission_grant import (
    CANARY_PROVENANCE,
    OkxDemoSubmissionGrantBlocked,
    OkxDemoSubmissionGrantService,
    submission_grant_request_digest,
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
            if kind == "instrument":
                content.update(
                    instrument_id="BTC-USDT-SWAP",
                    min_size="0.02",
                    lot_size="0.01",
                    contract_value="0.01",
                    state="live",
                    contract_shape="linear",
                )
            elif kind == "market":
                content.update(
                    instrument_id="BTC-USDT-SWAP",
                    reference_price="57000",
                    bbo={"ask_price": "57000"},
                    mark={"price": "57000"},
                    as_of=NOW.isoformat(),
                )
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
                    "position_side": "long",
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
            position_side="long",
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
        _bind_completed_risk_stage(session, intent, decision, approval)
        approval_id = approval.id
        session.commit()
        yield session, approval_id
    engine.dispose()


def _bind_completed_risk_stage(
    session: Session,
    intent: TradeIntent,
    decision: RiskDecision,
    approval: ApprovedExecution,
) -> FullChainRun:
    job = ResearchJob(
        execution_scope_id="LOCAL_DRY_RUN",
        job_type="deepseek_backtest",
        operation="strategy_generation.deepseek_backtest_loop",
        idempotency_key_digest="8" * 64,
        request_hash="9" * 64,
        request_payload={},
        status="RUNNING",
        stage="EXECUTION",
        attempt_count=1,
        max_attempts=1,
    )
    session.add(job)
    session.flush()
    attempt = ResearchJobAttempt(
        research_job_id=job.id,
        attempt_number=1,
        execution_scope_id="LOCAL_DRY_RUN",
        status="RUNNING",
    )
    session.add(attempt)
    session.flush()
    chain = FullChainRun(
        research_job_id=job.id,
        research_job_attempt_id=attempt.id,
        research_scope_id="LOCAL_DRY_RUN",
        execution_target_id="OKX_DEMO",
        status="EXECUTING",
        current_stage="EXECUTION",
        trade_intent_id=intent.id,
        risk_decision_id=decision.id,
        approved_execution_id=approval.id,
        started_at=NOW,
    )
    session.add(chain)
    session.flush()
    session.add(
        FullChainStageRun(
            full_chain_run_id=chain.id,
            stage="RISK",
            status="SUCCESS",
            idempotency_key_digest="a" * 64,
            input_digest="b" * 64,
            input_snapshot={"approval_id": approval.id},
            output_snapshot={"status": "APPROVED"},
            database_ids={
                "trade_intent_id": intent.id,
                "risk_decision_id": decision.id,
                "approved_execution_id": approval.id,
            },
            prepared_at=NOW,
            completed_at=NOW,
        )
    )
    session.flush()
    return chain


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


def _add_empty_reconciliation(session, *, observed_at=NOW, order_ids=None):
    run = ReconciliationRun(
        execution_target_id="OKX_DEMO",
        status="RECONCILED",
        summary_snapshot={},
        database_ids={
            "order_snapshots": [] if order_ids is None else order_ids,
            "position_snapshots": [],
        },
        artifact_status="READY",
        authoritative_observed_at=observed_at,
        source_type="api_aggregate",
        core_data=True,
        started_at=observed_at,
        completed_at=observed_at,
        created_at=observed_at,
    )
    session.add(run)
    session.flush()
    run.database_ids = dict(run.database_ids, reconciliation_run=[run.id])
    session.add(
        OkxDemoReconciliationState(
            execution_target_id="OKX_DEMO",
            status="RECONCILED",
            opening_frozen=False,
            last_event_observed_at=observed_at,
            last_reconciliation_run_id=run.id,
        )
    )
    session.commit()
    return run.id


def submission_grant(
    approval_id,
    writer="WriterInstance01",
    expires=None,
    canonical_hash="2" * 64,
    policy_digest="3" * 64,
    approved_payload_hash="5" * 64,
):
    return OrderSubmissionAuthorization(
        grant_id="1" * 32,
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
        client_order_id="WriterOrder001",
        issued_at=NOW - timedelta(seconds=1),
        expires_at=expires or NOW + timedelta(seconds=10),
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
        grant_id="1" * 32,
        authorization_mode="MANIFEST",
        writer_instance_id=writer_instance_id,
        approval_id=approval_id,
        canonical_hash=canonical_hash,
        policy_digest=policy_digest,
        approved_payload_hash=approved_payload_hash,
        now=now,
        expires_at=expires_at,
    )


def test_sqlite_claim_blocks_until_full_chain_risk_stage_is_successful(db) -> None:
    session, approval_id = db
    approval = session.get(ApprovedExecution, approval_id)
    chain = session.scalars(
        select(FullChainRun).where(
            FullChainRun.trade_intent_id == approval.trade_intent_id
        )
    ).one()
    checkpoint = session.scalars(
        select(FullChainStageRun).where(
            FullChainStageRun.full_chain_run_id == chain.id,
            FullChainStageRun.stage == "RISK",
        )
    ).one()
    chain.current_stage = "RISK"
    checkpoint.status = "PREPARED"
    checkpoint.database_ids = {}
    session.commit()

    store = SqlAlchemyOrderWriterStore(session, now_provider=lambda: NOW)
    with pytest.raises(OkxDemoWriteBlocked, match="could not be verified"):
        store.load_approved_execution(approval_id)

    assert session.scalars(select(OkxOrderWriterLease)).all() == []
    assert session.scalars(select(OkxOrderWriteAttempt)).all() == []


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


def test_one_shot_grant_is_consumed_with_prepared_journal(db) -> None:
    session, approval_id = db
    store = SqlAlchemyOrderWriterStore(session, now_provider=lambda: NOW)
    claimed = store.load_approved_execution(approval_id)
    run = ReconciliationRun(
        execution_target_id="OKX_DEMO",
        status="RECONCILED",
        summary_snapshot={},
        database_ids={"order_snapshots": [], "position_snapshots": []},
        artifact_status="READY",
        authoritative_observed_at=NOW,
        source_type="api_aggregate",
        core_data=True,
        started_at=NOW,
        completed_at=NOW,
        created_at=NOW,
    )
    session.add(run)
    session.flush()
    run.database_ids = dict(run.database_ids, reconciliation_run=[run.id])
    session.add(
        OkxDemoReconciliationState(
            execution_target_id="OKX_DEMO",
            status="RECONCILED",
            opening_frozen=False,
            last_event_observed_at=NOW,
            last_reconciliation_run_id=run.id,
        )
    )
    request_digest = submission_grant_request_digest(
        approval_id=approval_id,
        reconciliation_run_id=run.id,
        canonical_hash=claimed.canonical_hash,
        policy_digest=claimed.policy_digest,
        approved_payload_hash=claimed.approved_payload_hash,
        client_order_id=claimed.client_order_id,
        instrument_id="BTC-USDT-SWAP",
        canary_quantity=Decimal("0.02"),
        canary_notional=Decimal("11.4"),
    )
    grant = OkxDemoSubmissionGrant(
        grant_id="9" * 32,
        execution_target_id="OKX_DEMO",
        approval_id=approval_id,
        reconciliation_run_id=run.id,
        canonical_hash=claimed.canonical_hash,
        policy_digest=claimed.policy_digest,
        approved_payload_hash=claimed.approved_payload_hash,
        client_order_id=claimed.client_order_id,
        instrument_id="BTC-USDT-SWAP",
        canary_quantity=Decimal("0.02"),
        canary_notional=Decimal("11.4"),
        request_digest=request_digest,
        provenance=CANARY_PROVENANCE,
        status="ACTIVE",
        issued_at=NOW - timedelta(seconds=1),
        expires_at=NOW + timedelta(seconds=10),
    )
    session.add(grant)
    session.commit()
    grant_id = "9" * 32
    authorization = submission_grant(
        approval_id,
        expires=NOW + timedelta(seconds=10),
        canonical_hash=claimed.canonical_hash,
        policy_digest=claimed.policy_digest,
        approved_payload_hash=claimed.approved_payload_hash,
    ).model_copy(
        update={
            "authorization_mode": "ONE_SHOT",
            "grant_id": grant_id,
            "client_order_id": claimed.client_order_id,
        }
    )
    command = normalize_order_command(
        claimed,
        submission_grant=authorization,
        instrument=instrument(),
        now=NOW,
    )
    store.acquire_lease(
        grant_id=grant_id,
        authorization_mode="ONE_SHOT",
        writer_instance_id="WriterInstance01",
        approval_id=approval_id,
        canonical_hash=claimed.canonical_hash,
        policy_digest=claimed.policy_digest,
        approved_payload_hash=claimed.approved_payload_hash,
        now=NOW,
        expires_at=NOW + timedelta(seconds=10),
    )
    assert session.get(OkxDemoSubmissionGrant, grant_id).status == "ACTIVE"
    session.commit()

    _, attempt = store.prepare_place(
        command,
        operation="PLACE",
        operation_id=command.client_order_id,
        request_digest="8" * 64,
        safe_request_snapshot=command.request_body,
    )

    persisted = session.get(OkxDemoSubmissionGrant, grant_id)
    assert persisted.status == "CONSUMED"
    assert persisted.writer_instance_id == "WriterInstance01"
    assert persisted.consumed_at.replace(tzinfo=timezone.utc) == NOW
    assert session.get(OkxOrderWriteAttempt, attempt.attempt_id) is not None


def test_restart_claims_exact_unresolved_attempt_after_lease_expiry(db) -> None:
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
    _, prepared = first.prepare_place(
        command,
        operation="PLACE",
        operation_id=command.client_order_id,
        request_digest="6" * 64,
        safe_request_snapshot=command.request_body,
    )
    restarted_at = NOW + timedelta(seconds=11)
    restarted = SqlAlchemyOrderWriterStore(
        session,
        now_provider=lambda: restarted_at,
    )

    claimed = restarted.claim_unresolved_for_reconciliation(
        prepared.attempt_id,
        now=restarted_at,
        expires_at=restarted_at + timedelta(seconds=10),
    )

    assert claimed.attempt_id == prepared.attempt_id
    assert claimed.operation == "PLACE"
    assert claimed.lease_generation == 2


def test_unresolved_claim_rejects_wrong_identity_and_active_other_holder(db) -> None:
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
    _, prepared = first.prepare_place(
        command,
        operation="PLACE",
        operation_id=command.client_order_id,
        request_digest="6" * 64,
        safe_request_snapshot=command.request_body,
    )
    contender = SqlAlchemyOrderWriterStore(session, now_provider=lambda: NOW)

    with pytest.raises(OkxDemoWriteBlocked, match="matching unresolved"):
        contender.claim_unresolved_for_reconciliation(
            prepared.attempt_id + 1,
            now=NOW,
            expires_at=NOW + timedelta(seconds=10),
        )
    with pytest.raises(OkxDemoWriteBlocked, match="another OKX_DEMO writer"):
        contender.claim_unresolved_for_reconciliation(
            prepared.attempt_id,
            now=NOW,
            expires_at=NOW + timedelta(seconds=10),
        )


def test_service_arms_run_bound_non_production_grant(db, monkeypatch) -> None:
    session, approval_id = db
    run_id = _add_empty_reconciliation(session)
    approval = session.get(ApprovedExecution, approval_id)
    monkeypatch.setattr(
        "app.services.okx_demo_submission_grant.get_settings",
        lambda: SimpleNamespace(
            demo_automation_policy=SimpleNamespace(
                demo_risk_policy=SimpleNamespace(
                    allowed_instruments=("BTC-USDT-SWAP",)
                )
            ),
            execution_target_manifest=SimpleNamespace(
                active_target_id="OKX_DEMO",
                active_target=SimpleNamespace(
                    simulated_trading=True,
                    allow_real_funds=False,
                    order_submission_enabled=False,
                ),
            )
        ),
    )

    grant = OkxDemoSubmissionGrantService(
        session,
        now_provider=lambda: NOW,
    ).arm(
        approval_id=approval_id,
        canonical_hash=approval.canonical_hash,
        policy_digest=approval.policy_digest,
        approved_payload_hash=approval.approved_payload_hash,
        client_order_id=approval.client_order_id,
    )

    assert grant.reconciliation_run_id == run_id
    assert grant.provenance == CANARY_PROVENANCE
    assert grant.request_digest == submission_grant_request_digest(
        approval_id=approval_id,
        reconciliation_run_id=run_id,
        canonical_hash=approval.canonical_hash,
        policy_digest=approval.policy_digest,
        approved_payload_hash=approval.approved_payload_hash,
        client_order_id=approval.client_order_id,
        instrument_id="BTC-USDT-SWAP",
        canary_quantity=Decimal("0.02"),
        canary_notional=Decimal("11.4"),
    )
    assert grant.expires_at.replace(tzinfo=timezone.utc) == NOW + timedelta(
        seconds=10
    )


@pytest.mark.parametrize(
    ("observed_at", "order_ids"),
    [
        (NOW - timedelta(seconds=31), None),
        (NOW, [901]),
    ],
)
def test_service_rejects_stale_or_nonempty_reconciliation(
    db,
    monkeypatch,
    observed_at,
    order_ids,
) -> None:
    session, approval_id = db
    _add_empty_reconciliation(
        session,
        observed_at=observed_at,
        order_ids=order_ids,
    )
    approval = session.get(ApprovedExecution, approval_id)
    monkeypatch.setattr(
        "app.services.okx_demo_submission_grant.get_settings",
        lambda: SimpleNamespace(
            demo_automation_policy=SimpleNamespace(
                demo_risk_policy=SimpleNamespace(
                    allowed_instruments=("BTC-USDT-SWAP",)
                )
            ),
            execution_target_manifest=SimpleNamespace(
                active_target_id="OKX_DEMO",
                active_target=SimpleNamespace(
                    simulated_trading=True,
                    allow_real_funds=False,
                    order_submission_enabled=False,
                ),
            )
        ),
    )

    with pytest.raises(OkxDemoSubmissionGrantBlocked):
        OkxDemoSubmissionGrantService(
            session,
            now_provider=lambda: NOW,
        ).arm(
            approval_id=approval_id,
            canonical_hash=approval.canonical_hash,
            policy_digest=approval.policy_digest,
            approved_payload_hash=approval.approved_payload_hash,
            client_order_id=approval.client_order_id,
        )


def test_service_rejects_any_unresolved_writer_attempt(db, monkeypatch) -> None:
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
    _add_empty_reconciliation(session)
    approval = session.get(ApprovedExecution, approval_id)
    monkeypatch.setattr(
        "app.services.okx_demo_submission_grant.get_settings",
        lambda: SimpleNamespace(
            demo_automation_policy=SimpleNamespace(
                demo_risk_policy=SimpleNamespace(
                    allowed_instruments=("BTC-USDT-SWAP",)
                )
            ),
            execution_target_manifest=SimpleNamespace(
                active_target_id="OKX_DEMO",
                active_target=SimpleNamespace(
                    simulated_trading=True,
                    allow_real_funds=False,
                    order_submission_enabled=False,
                ),
            ),
        ),
    )

    with pytest.raises(
        OkxDemoSubmissionGrantBlocked,
        match="unresolved writer attempt",
    ):
        OkxDemoSubmissionGrantService(session, now_provider=lambda: NOW).arm(
            approval_id=approval_id,
            canonical_hash=approval.canonical_hash,
            policy_digest=approval.policy_digest,
            approved_payload_hash=approval.approved_payload_hash,
            client_order_id=approval.client_order_id,
        )


@pytest.mark.parametrize(
    "mutation",
    (
        "snapshot_digest",
        "session",
        "evidence_ref",
        "non_finite",
        "malformed_time",
        "future_market",
    ),
)
def test_service_revalidates_exact_attested_risk_bundle(
    db,
    monkeypatch,
    mutation,
) -> None:
    session, approval_id = db
    _add_empty_reconciliation(session)
    approval = session.get(ApprovedExecution, approval_id)
    intent = session.get(TradeIntent, approval.trade_intent_id)
    market = session.scalars(
        select(OkxDemoTrustedSnapshot).where(
            OkxDemoTrustedSnapshot.snapshot_id == approval.market_snapshot_id
        )
    ).one()
    if mutation == "snapshot_digest":
        market.digest = "0" * 64
    elif mutation == "session":
        attested_session = session.get(
            OkxDemoAttestedSession,
            market.attested_session_id,
        )
        attested_session.revoked_at = NOW
        attested_session.revoke_reason = "WRITE_FAILURE"
    elif mutation == "evidence_ref":
        request_snapshot = dict(intent.request_snapshot)
        evidence = dict(request_snapshot["snapshot_evidence"])
        evidence["market"] = dict(evidence["market"], digest="0" * 64)
        request_snapshot["snapshot_evidence"] = evidence
        intent.request_snapshot = request_snapshot
    else:
        content = dict(market.content_json)
        if mutation == "non_finite":
            content["reference_price"] = "NaN"
        elif mutation == "malformed_time":
            content["as_of"] = "not-a-time"
        else:
            content["as_of"] = (NOW + timedelta(minutes=1)).isoformat()
        market.content_json = content
        market.digest = canonical_digest(content)
    monkeypatch.setattr(
        "app.services.okx_demo_submission_grant.get_settings",
        lambda: SimpleNamespace(
            demo_automation_policy=SimpleNamespace(
                demo_risk_policy=SimpleNamespace(
                    allowed_instruments=("BTC-USDT-SWAP",)
                )
            ),
            execution_target_manifest=SimpleNamespace(
                active_target_id="OKX_DEMO",
                active_target=SimpleNamespace(
                    simulated_trading=True,
                    allow_real_funds=False,
                    order_submission_enabled=False,
                ),
            ),
        ),
    )

    with pytest.raises(OkxDemoSubmissionGrantBlocked):
        OkxDemoSubmissionGrantService(session, now_provider=lambda: NOW).arm(
            approval_id=approval_id,
            canonical_hash=approval.canonical_hash,
            policy_digest=approval.policy_digest,
            approved_payload_hash=approval.approved_payload_hash,
            client_order_id=approval.client_order_id,
        )


def test_reconciliation_drift_freezes_place_but_allows_cancel(db) -> None:
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
        exchange_order_id="exchange-order-freeze",
    )
    store.transition(
        acknowledged,
        event=WriteEvent.RECONCILE,
        order_state="live",
    )
    session.add(
        OkxDemoReconciliationState(
            execution_target_id="OKX_DEMO",
            status="DRIFTED",
            opening_frozen=True,
            block_reason="POSITION_DRIFT",
        )
    )
    session.commit()
    with pytest.raises(OkxDemoWriteBlocked):
        store.prepare_place(
            command,
            operation="PLACE",
            operation_id="SecondOpeningRisk01",
            request_digest="b" * 64,
            safe_request_snapshot=command.request_body,
        )
    canceled = store.prepare_existing(
        order,
        operation="CANCEL",
        operation_id=order.client_order_id,
        request_digest="c" * 64,
        safe_request_snapshot={
            "instId": order.instrument_id,
            "clOrdId": order.client_order_id,
        },
    )
    assert canceled.operation == "CANCEL"


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

    retained = session.get(ApprovedExecution, approval_id)
    assert retained is not None and retained.status == "EXPIRED"
    assert session.get(TradeIntent, intent_id).status == "APPROVED"
    assert session.get(RiskDecision, decision_id).decision == "APPROVED"
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
            grant_id="1" * 32,
            authorization_mode="MANIFEST",
            writer_instance_id="WriterInstance01",
            approval_id=approval_id,
            canonical_hash=claimed.canonical_hash,
            policy_digest=claimed.policy_digest,
            approved_payload_hash=claimed.approved_payload_hash,
            now=NOW,
            expires_at=NOW + timedelta(minutes=1),
        )

    retained = session.get(ApprovedExecution, approval_id)
    assert retained is not None and retained.status == "EXPIRED"
    assert session.get(TradeIntent, intent_id).status == "APPROVED"
    assert session.get(RiskDecision, decision_id).decision == "APPROVED"
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
            grant_id="1" * 32,
            authorization_mode="MANIFEST",
            writer_instance_id="WriterInstance01",
            approval_id=approval_id,
            canonical_hash=claimed.canonical_hash,
            policy_digest=claimed.policy_digest,
            approved_payload_hash=claimed.approved_payload_hash,
            now=NOW,
            expires_at=NOW + timedelta(minutes=1),
        )

    retained = session.get(ApprovedExecution, approval_id)
    assert retained is not None and retained.status == "EXPIRED"
    persisted = session.get(OkxOrderWriteAttempt, prepared.attempt_id)
    assert persisted is not None
    assert persisted.approval_id == approval_id
    assert persisted.state == "PREPARED"
