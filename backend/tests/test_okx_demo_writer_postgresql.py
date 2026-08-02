from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
from threading import Barrier
import time
from types import SimpleNamespace
from typing import Optional
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.adapters.okx_demo.write_semantics import OkxDemoWriteBlocked
from app.adapters.okx_demo.models import InstrumentSpec
from app.adapters.okx_demo.writer_models import (
    OrderSubmissionAuthorization,
    normalize_order_command,
)
from app.adapters.okx_demo.writer_repository import SqlAlchemyOrderWriterStore
from app.adapters.okx_demo.writer_state import WriteEvent
from app.db.migrations import (
    CANARY_LINEAGE_WRITE_BASE_VERSION,
    FULL_CHAIN_BASE_VERSION,
    ORDER_WRITER_BASE_VERSION,
    RECONCILIATION_BASE_VERSION,
    RECONCILIATION_INDEX_BASE_VERSION,
    RUNTIME_APP_ACL_BASE_VERSION,
    RUNTIME_APPLICATION_TABLES,
    SCHEMA_VERSION,
    SOAK_BASE_VERSION,
    SchemaMigrationBlocked,
    VERSION_TABLE,
    _add_order_writer,
    schema_problems,
    upgrade_database,
    verify_connection_schema,
    verify_schema,
)
from app.models.execution_lineage import (
    ApprovedExecution,
    ExchangeOrder,
    OkxDemoTrustedSnapshot,
    ReconciliationRun,
    ResearchJobAttempt,
    RiskBudget,
    RiskDecision,
    TradeIntent,
)
from app.models.full_chain import FullChainRun, FullChainStageRun
from app.models.order_writer import OkxDemoSubmissionGrant, OkxOrderWriteAttempt
from app.models.okx_demo_reconciliation import (
    OkxDemoExchangeEvent,
    OkxDemoReconciliationState,
    OkxDemoRecoveryGrant,
)
from app.models.research_job import ResearchJob
from app.repositories.execution_lineage import ensure_execution_scope_catalog
from app.services.risk_chain import (
    _issue_attested_session_capability,
    _normalize_attested_snapshot,
    _write_attested_snapshot,
    canonical_digest,
)
from app.services.okx_demo_reconciliation import (
    OkxDemoReconciliationBlocked,
    OkxDemoReconciliationService,
    SCHEMA_VERSION as RECONCILIATION_EVENT_SCHEMA_VERSION,
)
from app.services.okx_demo_canary_preparation import (
    CANARY_OPERATION,
    OkxDemoCanaryPreparationService,
)
from app.services.okx_demo_submission_grant import (
    acquire_one_shot_runtime_lock,
    CANARY_PROVENANCE,
    OkxDemoSubmissionGrantService,
    release_one_shot_runtime_lock,
    require_canary_reconciliation,
    submission_grant_request_digest,
    try_one_shot_transaction_lock,
)


# PostgreSQL attestation functions validate against statement_timestamp();
# keep integration evidence fresh instead of coupling the suite to one date.
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _canonical_json(value) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def _seed_canary_lineage_boundary(
    session: Session,
    *,
    now: datetime,
    content_mutation: Optional[tuple[str, str, object]] = None,
    reconciliation_mutation: Optional[tuple[str, object]] = None,
):
    ensure_execution_scope_catalog(session)
    capability = _issue_attested_session_capability(
        attestation_hmac_key=b"t" * 32,
        pinned_fingerprint_sha256="a" * 64,
        created_at=now - timedelta(seconds=1),
        expires_at=now + timedelta(minutes=2),
    )
    contents = {
        "instrument": {
            "execution_target": "OKX_DEMO",
            "source": "okx_demo_rest",
            "resource": "instrument",
            "stale": False,
            "instId": "BTC-USDT-SWAP",
            "minSz": "1",
            "lotSz": "1",
            "ctVal": "0.0001",
            "ctValCcy": "BTC",
            "tickSz": "0.1",
            "state": "live",
            "contract_shape": "linear",
        },
        "market": {
            "execution_target": "OKX_DEMO",
            "source": "okx_demo_rest",
            "resource": "market",
            "stale": False,
            "instrument_id": "BTC-USDT-SWAP",
            "reference_price": "57000",
            "bbo": {"ask_price": "57000"},
            "mark": {"price": "57000"},
            "as_of": now.isoformat(),
        },
        "account": {
            "execution_target": "OKX_DEMO",
            "source": "okx_demo_rest",
            "resource": "account",
            "stale": False,
            "authenticated": True,
        },
    }
    for content in contents.values():
        content["expires_at"] = (now + timedelta(seconds=30)).isoformat()
    if content_mutation is not None:
        kind, field, value = content_mutation
        if field == "bbo.ask_price":
            if value is None:
                contents[kind]["bbo"].pop("ask_price", None)
            else:
                contents[kind]["bbo"]["ask_price"] = value
        elif value is None:
            contents[kind].pop(field, None)
        else:
            contents[kind][field] = value
    snapshots = {}
    for kind, content in contents.items():
        normalized = _normalize_attested_snapshot(
            capability,
            kind=kind,
            content=content,
            observed_at=now,
            expires_at=now + timedelta(seconds=30),
        )
        snapshots[kind] = _write_attested_snapshot(
            session, capability, normalized, now=now
        )
    run = ReconciliationRun(
        execution_target_id="OKX_DEMO",
        status="RECONCILED",
        summary_snapshot={},
        database_ids={"order_snapshots": [], "position_snapshots": []},
        artifact_status="READY",
        authoritative_observed_at=now,
        source_type="api_aggregate",
        core_data=True,
        started_at=now,
        completed_at=now,
        created_at=now,
    )
    session.add(run)
    session.flush()
    run.database_ids = dict(run.database_ids, reconciliation_run=[run.id])
    if reconciliation_mutation is not None:
        field, value = reconciliation_mutation
        if field == "database_ids":
            run.database_ids = value
        else:
            setattr(run, field, value)
    state = session.scalars(
        select(OkxDemoReconciliationState).where(
            OkxDemoReconciliationState.execution_target_id == "OKX_DEMO"
        )
    ).one()
    state.status = "RECONCILED"
    state.opening_frozen = False
    state.block_reason = None
    state.last_event_observed_at = now
    state.last_reconciliation_run_id = run.id
    session.commit()
    order = {
        "instrument_id": "BTC-USDT-SWAP",
        "side": "buy",
        "position_side": "long",
        "order_type": "limit",
        "quantity": Decimal("1"),
        "limit_price": Decimal("57000"),
        "reference_price": Decimal("57000"),
        "leverage": Decimal("1"),
        "margin_mode": "isolated",
        "stop_loss": Decimal("54150"),
        "take_profit": Decimal("59850"),
        "reduce_only": False,
        "notional": Decimal("5.7"),
        "expires_at": now + timedelta(seconds=8),
    }
    return snapshots, run.id, order


def _canary_function_payload(
    session: Session,
    *,
    key_digest: str,
    reconciliation_run_id: int,
) -> dict:
    intent = session.scalars(
        select(TradeIntent).where(
            TradeIntent.idempotency_key_digest == key_digest
        )
    ).one()
    decision = session.scalars(
        select(RiskDecision).where(RiskDecision.trade_intent_id == intent.id)
    ).one()
    chain = session.scalars(
        select(FullChainRun).where(FullChainRun.trade_intent_id == intent.id)
    ).one()
    canonical_input = intent.request_snapshot["canonical_input"]
    policy = {
        "provenance": "CONTROLLED_CANARY_NON_PRODUCTION",
        "allowed_instruments": ["BTC-USDT-SWAP"],
        "allowed_sides": ["buy"],
        "allowed_order_types": ["limit"],
        "max_leverage": canonical_input["leverage"],
        "max_order_notional": "20",
        "max_total_exposure": "20",
        "max_positions": 1,
        "max_price_deviation_pct": "0.01",
        "min_strategy_score": "0",
        "scoring_version": "controlled-canary-v1",
    }
    approved_payload = {
        "canonical_input": canonical_input,
        "notional": format(decision.evidence_snapshot["notional"]),
        "provenance": "CONTROLLED_CANARY_NON_PRODUCTION",
    }
    intent_identity = {
        "provenance": "CONTROLLED_CANARY_NON_PRODUCTION",
        "idempotency_key_digest": key_digest,
        "canonical_hash": intent.canonical_hash,
    }
    evidence = intent.request_snapshot["snapshot_evidence"]
    return {
        "execution_target": "OKX_DEMO",
        "provenance": "CONTROLLED_CANARY_NON_PRODUCTION",
        "non_production": True,
        "full_chain_run_id": chain.id,
        "reconciliation_run_id": reconciliation_run_id,
        "intent_id": intent.intent_id,
        "canonical_hash": intent.canonical_hash,
        "policy_digest": intent.policy_digest,
        "approved_payload_hash": intent.approved_payload_hash,
        "idempotency_key_digest": key_digest,
        "client_order_id": intent.client_order_id,
        "instrument_id": intent.instrument_id,
        "side": intent.side,
        "position_side": intent.position_side,
        "order_type": intent.order_type,
        "quantity": canonical_input["quantity"],
        "limit_price": canonical_input["limit_price"],
        "reference_price": canonical_input["reference_price"],
        "leverage": canonical_input["leverage"],
        "margin_mode": intent.margin_mode,
        "stop_loss": canonical_input["stop_loss"],
        "take_profit": canonical_input["take_profit"],
        "reduce_only": intent.reduce_only,
        "notional": decision.evidence_snapshot["notional"],
        "request_snapshot": intent.request_snapshot,
        "expires_at": intent.expires_at.isoformat(),
        "canonical_input_serialized": _canonical_json(canonical_input),
        "policy_serialized": _canonical_json(policy),
        "approved_payload_serialized": _canonical_json(approved_payload),
        "intent_identity_serialized": _canonical_json(intent_identity),
        "instrument_snapshot_id": evidence["instrument"]["snapshot_id"],
        "market_snapshot_id": evidence["market"]["snapshot_id"],
        "account_snapshot_id": evidence["account"]["snapshot_id"],
    }


@pytest.fixture
def postgres_writer_engine():
    database_url = os.environ.get("POSTGRES_WORKER_URL")
    if not database_url:
        pytest.skip("NOT_RUN: POSTGRES_WORKER_URL is not configured")
    database_name = "test_okx_writer_{}".format(uuid4().hex)
    admin = create_engine(database_url, isolation_level="AUTOCOMMIT")
    engine = None
    role_snapshot = None
    membership_snapshot = None
    extension_snapshot = None

    def membership_rows(connection):
        server_version_num = int(
            connection.execute(text("SHOW server_version_num")).scalar_one()
        )
        option_columns = (
            ", membership.inherit_option, membership.set_option"
            if server_version_num >= 160000
            else ""
        )
        return [
            tuple(row)
            for row in connection.execute(
                text(
                    "SELECT owner.rolname, member.rolname, "
                    "membership.admin_option{} "
                    "FROM pg_auth_members AS membership "
                    "JOIN pg_roles AS owner ON owner.oid = membership.roleid "
                    "JOIN pg_roles AS member ON member.oid = membership.member "
                    "WHERE owner.rolname IN "
                    "('freqtrade', 'freqtrade_ai_attestor') "
                    "OR member.rolname IN "
                    "('freqtrade', 'freqtrade_ai_attestor') "
                    "ORDER BY owner.rolname, member.rolname".format(
                        option_columns
                    )
                )
            ).all()
        ]

    def cleanup_temporary_database() -> None:
        with admin.connect() as connection:
            exists = connection.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_database "
                    "WHERE datname = :database_name)"
                ),
                {"database_name": database_name},
            ).scalar_one()
            if not exists:
                return
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) "
                    "FROM pg_stat_activity "
                    "WHERE datname = :database_name "
                    "AND pid <> pg_backend_pid()"
                ),
                {"database_name": database_name},
            )
            connection.execute(
                text('DROP DATABASE "{}"'.format(database_name))
            )

    try:
        with admin.connect() as connection:
            role_snapshot = [
                tuple(row)
                for row in connection.execute(
                    text(
                        "SELECT rolname, rolcanlogin, rolinherit, rolsuper, "
                        "rolcreaterole, rolcreatedb, rolreplication, "
                        "rolbypassrls FROM pg_roles WHERE rolname IN "
                        "('freqtrade', 'freqtrade_ai_attestor') "
                        "ORDER BY rolname"
                    )
                ).all()
            ]
            membership_snapshot = membership_rows(connection)
            extension_snapshot = [
                tuple(row)
                for row in connection.execute(
                    text(
                        "SELECT extname, extversion FROM pg_extension "
                        "ORDER BY extname"
                    )
                ).all()
            ]
        expected_roles = [
            ("freqtrade", True, True, False, False, False, False, False),
            (
                "freqtrade_ai_attestor",
                False,
                False,
                False,
                False,
                False,
                False,
                False,
            ),
        ]
        if role_snapshot != expected_roles or membership_snapshot:
            pytest.skip(
                "NOT_RUN: protected cluster roles are not already safe"
            )
        try:
            with admin.connect() as connection:
                connection.execute(
                    text(
                        'CREATE DATABASE "{}" TEMPLATE template0'.format(
                            database_name
                        )
                    )
                )
        except SQLAlchemyError:
            cleanup_temporary_database()
            pytest.skip(
                "NOT_RUN: temporary database creation is unavailable"
            )
        engine = create_engine(
            make_url(database_url).set(database=database_name),
            pool_pre_ping=True,
        )
        yield engine
    finally:
        if engine is not None:
            engine.dispose()
        try:
            cleanup_temporary_database()
            if (
                role_snapshot is not None
                and membership_snapshot is not None
                and extension_snapshot is not None
            ):
                with admin.connect() as connection:
                    assert [
                        tuple(row)
                        for row in connection.execute(
                            text(
                                "SELECT rolname, rolcanlogin, rolinherit, "
                                "rolsuper, rolcreaterole, rolcreatedb, "
                                "rolreplication, rolbypassrls FROM pg_roles "
                                "WHERE rolname IN "
                                "('freqtrade', 'freqtrade_ai_attestor') "
                                "ORDER BY rolname"
                            )
                        ).all()
                    ] == role_snapshot
                    assert membership_rows(connection) == membership_snapshot
                    assert [
                        tuple(row)
                        for row in connection.execute(
                            text(
                                "SELECT extname, extversion "
                                "FROM pg_extension ORDER BY extname"
                            )
                        ).all()
                    ] == extension_snapshot
        finally:
            admin.dispose()


def _seed_approved_order(
    session: Session,
    *,
    create_order: bool = True,
    controlled_canary: bool = False,
) -> tuple[int, int]:
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
                min_size="1",
                lot_size="1",
                contract_value=("0.0001" if controlled_canary else "1"),
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
    canonical_input = {"request": "postgresql-writer-test"}
    canonical_hash = canonical_digest(canonical_input)
    lineage = {"test_lineage": True}
    notional = Decimal("5.7" if controlled_canary else "57000")
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
                "quantity": Decimal("1"),
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
        idempotency_key_digest="5" * 64,
        client_order_id="PgWriterOrder001",
        instrument_id="BTC-USDT-SWAP",
        side="buy",
        position_side="long",
        order_type="limit",
        quantity="1",
        limit_price="57000",
        reference_price="57000",
        leverage="3",
        margin_mode="isolated",
        stop_loss="55000",
        take_profit="60000",
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
    session.flush()
    _bind_completed_risk_stage(session, intent, decision, approval)
    order = None
    if create_order:
        order = ExchangeOrder(
            execution_target_id="OKX_DEMO",
            trade_intent_id=intent.id,
            client_order_id=intent.client_order_id,
            status="PREPARED",
            request_snapshot={},
            response_snapshot={},
        )
        session.add(order)
    session.commit()
    return approval.id, order.id if order is not None else 0


def _bind_completed_risk_stage(
    session: Session,
    intent: TradeIntent,
    decision: RiskDecision,
    approval: ApprovedExecution,
) -> FullChainRun:
    identity = uuid4().hex
    job = ResearchJob(
        execution_scope_id="LOCAL_DRY_RUN",
        job_type="deepseek_backtest",
        operation="strategy_generation.deepseek_backtest_loop",
        idempotency_key_digest=canonical_digest({"job": identity}),
        request_hash=canonical_digest({"request": identity}),
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
            idempotency_key_digest=canonical_digest({"risk": identity}),
            input_digest=canonical_digest({"input": identity}),
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


def test_postgresql_fresh_schema_and_any_predicate_verify(
    postgres_writer_engine,
) -> None:
    assert upgrade_database(postgres_writer_engine) == SCHEMA_VERSION
    readiness = verify_schema(postgres_writer_engine)

    assert readiness.ready is True
    assert readiness.problems == ()


def test_postgresql_canary_lineage_function_is_the_only_runtime_write_boundary(
    postgres_writer_engine,
) -> None:
    upgrade_database(postgres_writer_engine)
    with postgres_writer_engine.connect() as connection:
        function_acl = connection.execute(
            text(
                "SELECT owner.rolname, function.prosecdef, function.proconfig, "
                "has_function_privilege('freqtrade', function.oid, 'EXECUTE'), "
                "EXISTS (SELECT 1 FROM aclexplode(function.proacl) acl "
                "WHERE acl.grantee = 0 AND acl.privilege_type = 'EXECUTE') "
                "FROM pg_proc function JOIN pg_roles owner "
                "ON owner.oid = function.proowner "
                "WHERE function.oid = "
                "'create_okx_demo_canary_lineage(jsonb)'::regprocedure"
            )
        ).one()
        assert tuple(function_acl) == (
            "freqtrade_ai_attestor",
            True,
            ["search_path=pg_catalog"],
            True,
            False,
        )
        for table_name in (
            "trade_intents",
            "risk_decisions",
            "approved_executions",
        ):
            assert connection.execute(
                text(
                    "SELECT has_table_privilege('freqtrade', :table, 'INSERT'), "
                    "has_table_privilege('freqtrade', :table, 'UPDATE'), "
                    "has_table_privilege('freqtrade', :table, 'DELETE')"
                ),
                {"table": table_name},
            ).one() == (False, False, False)
            sequence_name = connection.execute(
                text("SELECT pg_get_serial_sequence(:table, 'id')"),
                {"table": table_name},
            ).scalar_one()
            assert connection.execute(
                text(
                    "SELECT has_sequence_privilege('freqtrade', :sequence, 'USAGE'), "
                    "has_sequence_privilege('freqtrade', :sequence, 'SELECT'), "
                    "has_sequence_privilege('freqtrade', :sequence, 'UPDATE')"
                ),
                {"sequence": sequence_name},
            ).one() == (False, False, False)

    for statement in (
        "INSERT INTO trade_intents DEFAULT VALUES",
        "UPDATE trade_intents SET status = status",
        "SELECT nextval(pg_get_serial_sequence('trade_intents', 'id'))",
    ):
        with pytest.raises(SQLAlchemyError):
            with postgres_writer_engine.begin() as connection:
                connection.execute(text("SET LOCAL ROLE freqtrade"))
                connection.execute(text(statement))


def test_postgresql_upgrade_25_to_26_installs_canary_lineage_boundary(
    postgres_writer_engine,
) -> None:
    upgrade_database(postgres_writer_engine)
    with postgres_writer_engine.begin() as connection:
        connection.execute(
            text("DROP FUNCTION create_okx_demo_canary_lineage(jsonb)")
        )
        connection.execute(text("DELETE FROM {}".format(VERSION_TABLE)))
        connection.execute(
            text(
                "INSERT INTO {} (version) VALUES (:version)".format(
                    VERSION_TABLE
                )
            ),
            {"version": CANARY_LINEAGE_WRITE_BASE_VERSION},
        )
    assert upgrade_database(postgres_writer_engine) == SCHEMA_VERSION
    assert verify_schema(postgres_writer_engine).ready is True


def test_postgresql_canary_lineage_function_body_tamper_fails_readiness(
    postgres_writer_engine,
) -> None:
    upgrade_database(postgres_writer_engine)
    with postgres_writer_engine.begin() as connection:
        connection.execute(
            text(
                "CREATE OR REPLACE FUNCTION create_okx_demo_canary_lineage(p_payload jsonb) "
                "RETURNS jsonb LANGUAGE sql SECURITY DEFINER "
                "SET search_path = pg_catalog AS $$ SELECT '{}'::jsonb $$"
            )
        )
    assert "controlled canary lineage function boundary mismatch" in schema_problems(
        postgres_writer_engine
    )


@pytest.mark.parametrize(
    "content_mutation",
    (
        ("instrument", "instId", None),
        ("account", "authenticated", None),
        ("instrument", "minSz", "not-a-number"),
        ("market", "bbo.ask_price", None),
    ),
)
def test_postgresql_canary_lineage_rejects_missing_or_malformed_snapshot_content(
    postgres_writer_engine,
    content_mutation,
) -> None:
    upgrade_database(postgres_writer_engine)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with Session(postgres_writer_engine) as admin_session:
        snapshots, reconciliation_run_id, order = _seed_canary_lineage_boundary(
            admin_session,
            now=now,
            content_mutation=(
                None
                if content_mutation == ("account", "authenticated", None)
                else content_mutation
            ),
        )
        snapshot_ids = {kind: row.database_id for kind, row in snapshots.items()}
    if content_mutation == ("account", "authenticated", None):
        with postgres_writer_engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE okx_demo_trusted_snapshots DISABLE TRIGGER USER")
            )
            connection.execute(
                text(
                    "UPDATE okx_demo_trusted_snapshots "
                    "SET content_json = (content_json::jsonb - 'authenticated')::json "
                    "WHERE kind = 'account'"
                )
            )
            connection.execute(
                text("ALTER TABLE okx_demo_trusted_snapshots ENABLE TRIGGER USER")
            )
    with pytest.raises(SQLAlchemyError):
        with Session(postgres_writer_engine) as runtime_session:
            runtime_session.execute(text("SET LOCAL ROLE freqtrade"))
            runtime_snapshots = {
                kind: runtime_session.get(OkxDemoTrustedSnapshot, database_id)
                for kind, database_id in snapshot_ids.items()
            }
            OkxDemoCanaryPreparationService(
                runtime_session,
                now_provider=lambda: now,
            )._persist_lineage(
                key_digest=hashlib.sha256(repr(content_mutation).encode()).hexdigest(),
                now=now,
                reconciliation_run_id=reconciliation_run_id,
                snapshots=runtime_snapshots,
                order=order,
            )
    with postgres_writer_engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM trade_intents")).scalar_one() == 0
        assert connection.execute(text("SELECT count(*) FROM risk_decisions")).scalar_one() == 0
        assert connection.execute(text("SELECT count(*) FROM approved_executions")).scalar_one() == 0


@pytest.mark.parametrize(
    "reconciliation_mutation",
    (
        ("authoritative_observed_at", None),
        ("completed_at", NOW - timedelta(seconds=31)),
        ("authoritative_observed_at", NOW + timedelta(seconds=6)),
        (
            "database_ids",
            {"reconciliation_run": [], "order_snapshots": [], "position_snapshots": []},
        ),
        (
            "database_ids",
            {"reconciliation_run": [1], "order_snapshots": [1], "position_snapshots": []},
        ),
        (
            "database_ids",
            {"reconciliation_run": [1], "order_snapshots": [], "position_snapshots": [1]},
        ),
    ),
)
def test_postgresql_canary_lineage_rejects_incomplete_reconciliation_contract(
    postgres_writer_engine,
    reconciliation_mutation,
) -> None:
    upgrade_database(postgres_writer_engine)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    field, value = reconciliation_mutation
    if isinstance(value, datetime):
        delta = value - NOW
        value = now + delta
    with Session(postgres_writer_engine) as admin_session:
        snapshots, reconciliation_run_id, order = _seed_canary_lineage_boundary(
            admin_session,
            now=now,
            reconciliation_mutation=(field, value),
        )
        snapshot_ids = {kind: row.database_id for kind, row in snapshots.items()}
    with pytest.raises(SQLAlchemyError):
        with Session(postgres_writer_engine) as runtime_session:
            runtime_session.execute(text("SET LOCAL ROLE freqtrade"))
            runtime_snapshots = {
                kind: runtime_session.get(OkxDemoTrustedSnapshot, database_id)
                for kind, database_id in snapshot_ids.items()
            }
            OkxDemoCanaryPreparationService(
                runtime_session,
                now_provider=lambda: now,
            )._persist_lineage(
                key_digest=hashlib.sha256(repr(reconciliation_mutation).encode()).hexdigest(),
                now=now,
                reconciliation_run_id=reconciliation_run_id,
                snapshots=runtime_snapshots,
                order=order,
            )
    with postgres_writer_engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM trade_intents")).scalar_one() == 0


def test_postgresql_canary_lineage_function_atomic_idempotency_and_mismatch_rollback(
    postgres_writer_engine,
) -> None:
    upgrade_database(postgres_writer_engine)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with Session(postgres_writer_engine) as admin_session:
        snapshots, reconciliation_run_id, order = _seed_canary_lineage_boundary(
            admin_session,
            now=now,
        )
        snapshot_database_ids = {
            kind: row.database_id for kind, row in snapshots.items()
        }

    key_digest = hashlib.sha256(b"postgres-canary-lineage").hexdigest()
    with Session(postgres_writer_engine) as runtime_session:
        runtime_session.execute(text("SET LOCAL ROLE freqtrade"))
        snapshots = {
            kind: runtime_session.get(OkxDemoTrustedSnapshot, database_id)
            for kind, database_id in snapshot_database_ids.items()
        }
        result = OkxDemoCanaryPreparationService(
            runtime_session,
            now_provider=lambda: now,
        )._persist_lineage(
            key_digest=key_digest,
            now=now,
            reconciliation_run_id=reconciliation_run_id,
            snapshots=snapshots,
            order=order,
        )
        runtime_session.commit()
        expected_ids = {
            "trade_intent_id": result.trade_intent_id,
            "risk_decision_id": result.risk_decision_id,
            "approved_execution_id": result.approval_id,
        }

    with Session(postgres_writer_engine) as admin_session:
        payload = _canary_function_payload(
            admin_session,
            key_digest=key_digest,
            reconciliation_run_id=reconciliation_run_id,
        )
        baseline = tuple(
            admin_session.execute(
                text(
                    "SELECT (SELECT count(*) FROM trade_intents), "
                    "(SELECT count(*) FROM risk_decisions), "
                    "(SELECT count(*) FROM approved_executions)"
                )
            ).one()
        )

    lock_holder = postgres_writer_engine.connect()
    lock_transaction = lock_holder.begin()
    try:
        lock_holder.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": 0x4654414F4E455348},
        )
        with pytest.raises(SQLAlchemyError, match="coordination lock is busy"):
            with postgres_writer_engine.begin() as connection:
                connection.execute(text("SET LOCAL ROLE freqtrade"))
                connection.execute(
                    text(
                        "SELECT create_okx_demo_canary_lineage("
                        "CAST(:payload AS jsonb))"
                    ),
                    {"payload": json.dumps(payload, sort_keys=True)},
                )
    finally:
        lock_transaction.rollback()
        lock_holder.close()

    with postgres_writer_engine.begin() as connection:
        connection.execute(text("SET LOCAL ROLE freqtrade"))
        replay = connection.execute(
            text(
                "SELECT create_okx_demo_canary_lineage(CAST(:payload AS jsonb))"
            ),
            {"payload": json.dumps(payload, sort_keys=True)},
        ).scalar_one()
        assert replay == expected_ids

    with postgres_writer_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE risk_decisions SET evidence_snapshot = "
                "jsonb_set(evidence_snapshot::jsonb, '{provenance}', "
                "'\"TAMPERED\"'::jsonb)::json"
            )
        )
    with pytest.raises(SQLAlchemyError, match="idempotency conflict"):
        with postgres_writer_engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE freqtrade"))
            connection.execute(
                text(
                    "SELECT create_okx_demo_canary_lineage(CAST(:payload AS jsonb))"
                ),
                {"payload": json.dumps(payload, sort_keys=True)},
            )
    with postgres_writer_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE risk_decisions SET evidence_snapshot = "
                "jsonb_set(evidence_snapshot::jsonb, '{provenance}', "
                "'\"CONTROLLED_CANARY_NON_PRODUCTION\"'::jsonb)::json"
            )
        )
        connection.execute(
            text("ALTER TABLE approved_executions DISABLE TRIGGER USER")
        )
        connection.execute(
            text(
                "UPDATE approved_executions SET evidence_snapshot = "
                "jsonb_set(evidence_snapshot::jsonb, '{non_production}', "
                "'false'::jsonb)::json"
            )
        )
        connection.execute(
            text("ALTER TABLE approved_executions ENABLE TRIGGER USER")
        )
    with pytest.raises(SQLAlchemyError, match="idempotency conflict"):
        with postgres_writer_engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE freqtrade"))
            connection.execute(
                text(
                    "SELECT create_okx_demo_canary_lineage(CAST(:payload AS jsonb))"
                ),
                {"payload": json.dumps(payload, sort_keys=True)},
            )
    with postgres_writer_engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE approved_executions DISABLE TRIGGER USER")
        )
        connection.execute(
            text(
                "UPDATE approved_executions SET evidence_snapshot = "
                "jsonb_set(evidence_snapshot::jsonb, '{non_production}', "
                "'true'::jsonb)::json"
            )
        )
        connection.execute(
            text("ALTER TABLE approved_executions ENABLE TRIGGER USER")
        )

    mutations = {
        "target": ("execution_target", "OKX_LIVE"),
        "provenance": ("provenance", "DEEPSEEK"),
        "hash": ("canonical_hash", "f" * 64),
        "ttl": (
            "expires_at",
            (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        ),
    }
    for _name, (field, value) in mutations.items():
        altered = dict(payload)
        altered[field] = value
        with pytest.raises(SQLAlchemyError):
            with postgres_writer_engine.begin() as connection:
                connection.execute(text("SET LOCAL ROLE freqtrade"))
                connection.execute(
                    text(
                        "SELECT create_okx_demo_canary_lineage("
                        "CAST(:payload AS jsonb))"
                    ),
                    {"payload": json.dumps(altered, sort_keys=True)},
                )

    altered_snapshot = json.loads(json.dumps(payload))
    altered_snapshot["request_snapshot"]["snapshot_evidence"]["market"][
        "digest"
    ] = "0" * 64
    with pytest.raises(SQLAlchemyError):
        with postgres_writer_engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE freqtrade"))
            connection.execute(
                text(
                    "SELECT create_okx_demo_canary_lineage(CAST(:payload AS jsonb))"
                ),
                {"payload": json.dumps(altered_snapshot, sort_keys=True)},
            )

    with postgres_writer_engine.begin() as connection:
        connection.execute(
            text("UPDATE full_chain_runs SET status = 'BLOCKED' WHERE id = :id"),
            {"id": payload["full_chain_run_id"]},
        )
    with pytest.raises(SQLAlchemyError):
        with postgres_writer_engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE freqtrade"))
            connection.execute(
                text(
                    "SELECT create_okx_demo_canary_lineage(CAST(:payload AS jsonb))"
                ),
                {"payload": json.dumps(payload, sort_keys=True)},
            )

    with postgres_writer_engine.connect() as connection:
        assert tuple(
            connection.execute(
                text(
                    "SELECT (SELECT count(*) FROM trade_intents), "
                    "(SELECT count(*) FROM risk_decisions), "
                    "(SELECT count(*) FROM approved_executions)"
                )
            ).one()
        ) == baseline


def test_postgresql_upgrade_from_09_installs_fail_closed_reconciliation(
    postgres_writer_engine,
) -> None:
    assert upgrade_database(postgres_writer_engine) == SCHEMA_VERSION
    with postgres_writer_engine.begin() as connection:
        for table_name in (
            "okx_demo_recovery_grants",
            "okx_demo_order_snapshots",
            "okx_demo_fill_snapshots",
            "okx_demo_position_snapshots",
            "okx_demo_account_snapshots",
            "okx_demo_exchange_events",
            "okx_demo_reconciliation_states",
            "okx_demo_recovery_batches",
        ):
            connection.execute(
                text("DROP TABLE {} CASCADE".format(table_name))
            )
        connection.execute(
            text(
                "ALTER TABLE reconciliation_runs "
                "DROP CONSTRAINT reconciliation_runs_status_check, "
                "DROP CONSTRAINT reconciliation_runs_artifact_status_check, "
                "DROP COLUMN database_ids, DROP COLUMN artifact_path, "
                "DROP COLUMN artifact_sha256, DROP COLUMN artifact_status, "
                "DROP COLUMN authoritative_observed_at, "
                "DROP COLUMN source_type, DROP COLUMN core_data"
            )
        )
        connection.execute(text("DELETE FROM {}".format(VERSION_TABLE)))
        connection.execute(
            text(
                "INSERT INTO {} (version) VALUES (:version)".format(
                    VERSION_TABLE
                )
            ),
            {"version": RECONCILIATION_BASE_VERSION},
        )
    assert upgrade_database(postgres_writer_engine) == SCHEMA_VERSION
    assert schema_problems(postgres_writer_engine) == []
    with Session(postgres_writer_engine) as db:
        state = db.query(OkxDemoReconciliationState).one()
        assert state.execution_target_id == "OKX_DEMO"
        assert state.status == "UNKNOWN"
        assert state.opening_frozen is True


def test_postgresql_reconciliation_acl_and_fk_tamper_fail_readiness(
    postgres_writer_engine,
) -> None:
    assert upgrade_database(postgres_writer_engine) == SCHEMA_VERSION
    with postgres_writer_engine.begin() as connection:
        connection.execute(
            text(
                "GRANT DELETE ON okx_demo_position_snapshots TO freqtrade"
            )
        )
    assert any(
        "runtime writer has unsafe DML privilege: "
        "okx_demo_position_snapshots" in problem
        for problem in schema_problems(postgres_writer_engine)
    )
    with postgres_writer_engine.begin() as connection:
        connection.execute(
            text(
                "REVOKE DELETE ON okx_demo_position_snapshots FROM freqtrade"
            )
        )
        connection.execute(
            text(
                "ALTER TABLE okx_demo_order_snapshots DROP CONSTRAINT "
                "okx_demo_order_snapshots_event_database_id_fkey"
            )
        )
    assert any(
        "missing foreign key: okx_demo_order_snapshots" in problem
        for problem in schema_problems(postgres_writer_engine)
    )


def test_connection_schema_requires_exact_runtime_role(
    postgres_writer_engine,
) -> None:
    assert upgrade_database(postgres_writer_engine) == SCHEMA_VERSION
    with postgres_writer_engine.connect() as connection:
        admin_readiness = verify_connection_schema(connection)
        assert admin_readiness.ready is False
        assert any(
            "writer connection role mismatch" in item
            for item in admin_readiness.problems
        )
        connection.rollback()
        transaction = connection.begin()
        connection.execute(text("SET LOCAL ROLE freqtrade"))
        runtime_readiness = verify_connection_schema(connection)
        assert runtime_readiness.ready is True
        transaction.rollback()


@pytest.mark.parametrize(
    ("mutation", "problem"),
    [
        (
            "GRANT DELETE ON okx_order_writer_leases TO freqtrade",
            "runtime writer has unsafe DML privilege",
        ),
        (
            "GRANT UPDATE ON okx_order_write_attempts TO PUBLIC",
            "PUBLIC writer table privilege is not revoked",
        ),
        (
            "GRANT UPDATE (holder_token_digest) "
            "ON okx_order_writer_leases TO PUBLIC",
            "PUBLIC writer column DML is not revoked",
        ),
        (
            "GRANT UPDATE (holder_token_digest) "
            "ON okx_order_writer_leases TO freqtrade",
            "runtime writer column DML is not revoked",
        ),
        (
            "ALTER TABLE okx_order_writer_leases OWNER TO CURRENT_USER",
            "writer table owner mismatch",
        ),
        (
            "ALTER SCHEMA public OWNER TO freqtrade",
            "writer schema owner mismatch",
        ),
        (
            "GRANT UPDATE ON SEQUENCE "
            "okx_order_write_attempts_id_seq TO freqtrade",
            "writer attempt sequence ACL mismatch",
        ),
        (
            "ALTER TABLE okx_order_writer_leases DROP CONSTRAINT "
            "okx_order_writer_leases_generation_check",
            "missing check constraint",
        ),
        (
            "ALTER TABLE okx_order_write_attempts DROP CONSTRAINT "
            "okx_order_write_attempts_exchange_order_row_id_fkey",
            "missing foreign key",
        ),
        (
            "ALTER TABLE okx_order_write_attempts ADD CONSTRAINT "
            "unexpected_writer_approval_fkey FOREIGN KEY (approval_id) "
            "REFERENCES approved_executions(id) ON DELETE RESTRICT",
            "unexpected foreign key",
        ),
    ],
)
def test_connection_schema_rejects_writer_security_tampering(
    postgres_writer_engine,
    mutation,
    problem,
) -> None:
    assert upgrade_database(postgres_writer_engine) == SCHEMA_VERSION
    with postgres_writer_engine.connect() as connection:
        transaction = connection.begin()
        connection.execute(text(mutation))

        readiness = verify_connection_schema(connection)

        assert readiness.ready is False
        assert any(problem in item for item in readiness.problems)
        transaction.rollback()
    assert verify_schema(postgres_writer_engine).ready is True


def test_postgresql_upgrade_from_reconciliation_schema_adds_full_chain_tables(
    postgres_writer_engine,
) -> None:
    assert upgrade_database(postgres_writer_engine) == SCHEMA_VERSION
    full_chain_tables = (
        "full_chain_signal_snapshots",
        "full_chain_stage_runs",
        "strategy_candidate_approvals",
        "full_chain_runs",
    )
    with postgres_writer_engine.begin() as connection:
        for table_name in full_chain_tables:
            connection.execute(text('DROP TABLE "{}" CASCADE'.format(table_name)))
        connection.execute(text("DELETE FROM {}".format(VERSION_TABLE)))
        connection.execute(
            text(
                "INSERT INTO {} (version) VALUES (:version)".format(
                    VERSION_TABLE
                )
            ),
            {"version": FULL_CHAIN_BASE_VERSION},
        )

    assert upgrade_database(postgres_writer_engine) == SCHEMA_VERSION
    assert verify_schema(postgres_writer_engine).ready is True
    assert set(full_chain_tables).issubset(
        set(inspect(postgres_writer_engine).get_table_names())
    )


def test_connection_schema_rejects_role_that_can_set_runtime_writer(
    postgres_writer_engine,
) -> None:
    assert upgrade_database(postgres_writer_engine) == SCHEMA_VERSION
    delegated_role = "test_writer_delegate_{}".format(uuid4().hex)
    with postgres_writer_engine.connect() as connection:
        transaction = connection.begin()
        connection.execute(text('CREATE ROLE "{}" NOLOGIN'.format(delegated_role)))
        connection.execute(
            text('GRANT freqtrade TO "{}"'.format(delegated_role))
        )

        readiness = verify_connection_schema(connection)

        assert readiness.ready is False
        assert any(
            "protected role has delegated member" in item
            for item in readiness.problems
        )
        transaction.rollback()
    assert verify_schema(postgres_writer_engine).ready is True


def test_connection_schema_rejects_unexpected_column_writer(
    postgres_writer_engine,
) -> None:
    assert upgrade_database(postgres_writer_engine) == SCHEMA_VERSION
    delegated_role = "test_writer_column_{}".format(uuid4().hex)
    with postgres_writer_engine.connect() as connection:
        transaction = connection.begin()
        connection.execute(text('CREATE ROLE "{}" NOLOGIN'.format(delegated_role)))
        connection.execute(
            text(
                'GRANT UPDATE (state) ON okx_order_write_attempts TO "{}"'.format(
                    delegated_role
                )
            )
        )

        readiness = verify_connection_schema(connection)

        assert readiness.ready is False
        assert any(
            "unexpected role has writer column DML" in item
            for item in readiness.problems
        )
        transaction.rollback()
    assert verify_schema(postgres_writer_engine).ready is True


def test_connection_schema_rejects_foreign_key_action_tampering(
    postgres_writer_engine,
) -> None:
    assert upgrade_database(postgres_writer_engine) == SCHEMA_VERSION
    with postgres_writer_engine.connect() as connection:
        transaction = connection.begin()
        connection.execute(
            text(
                "ALTER TABLE okx_order_write_attempts DROP CONSTRAINT "
                "okx_order_write_attempts_exchange_order_row_id_fkey"
            )
        )
        connection.execute(
            text(
                "ALTER TABLE okx_order_write_attempts ADD CONSTRAINT "
                "okx_order_write_attempts_exchange_order_row_id_fkey "
                "FOREIGN KEY (exchange_order_row_id) "
                "REFERENCES exchange_orders(id) ON DELETE RESTRICT "
                "DEFERRABLE INITIALLY DEFERRED"
            )
        )

        readiness = verify_connection_schema(connection)

        assert readiness.ready is False
        assert any(
            "missing foreign key" in item
            and "ondelete=CASCADE" in item
            and "deferrable=False" in item
            for item in readiness.problems
        )
        assert any(
            "unexpected foreign key" in item
            and "ondelete=RESTRICT" in item
            and "deferrable=True" in item
            and "initially=DEFERRED" in item
            for item in readiness.problems
        )
        transaction.rollback()
    assert verify_schema(postgres_writer_engine).ready is True


def test_connection_schema_rejects_weakened_fencing_definitions(
    postgres_writer_engine,
) -> None:
    assert upgrade_database(postgres_writer_engine) == SCHEMA_VERSION
    with postgres_writer_engine.connect() as connection:
        transaction = connection.begin()
        connection.execute(
            text(
                "ALTER TABLE okx_order_writer_leases DROP CONSTRAINT "
                "okx_order_writer_leases_generation_check"
            )
        )
        connection.execute(
            text(
                "ALTER TABLE okx_order_writer_leases ADD CONSTRAINT "
                "okx_order_writer_leases_generation_check "
                "CHECK (generation >= 0)"
            )
        )
        connection.execute(
            text(
                "ALTER TABLE okx_order_write_attempts DROP CONSTRAINT "
                "okx_order_write_attempts_fencing_sequence_check"
            )
        )
        connection.execute(
            text(
                "ALTER TABLE okx_order_write_attempts ADD CONSTRAINT "
                "okx_order_write_attempts_fencing_sequence_check "
                "CHECK (lease_generation >= 0 AND close_sequence >= 0)"
            )
        )

        readiness = verify_connection_schema(connection)

        assert readiness.ready is False
        assert any(
            "check definition mismatch: "
            "okx_order_writer_leases."
            "okx_order_writer_leases_generation_check" in item
            for item in readiness.problems
        )
        assert any(
            "check definition mismatch: "
            "okx_order_write_attempts."
            "okx_order_write_attempts_fencing_sequence_check" in item
            for item in readiness.problems
        )
        transaction.rollback()
    assert verify_schema(postgres_writer_engine).ready is True


def test_connection_local_search_path_cannot_borrow_ready_engine_schema(
    postgres_writer_engine,
) -> None:
    assert upgrade_database(postgres_writer_engine) == SCHEMA_VERSION
    with postgres_writer_engine.connect() as connection:
        transaction = connection.begin()
        connection.execute(text("CREATE TEMP TABLE initialize_pg_temp (id integer)"))
        connection.execute(text("SET LOCAL search_path TO pg_temp"))

        readiness = verify_connection_schema(connection)

        assert readiness.ready is False
        assert readiness.schema_version is None
        assert "migration version table is missing" in readiness.problems
        transaction.rollback()


def test_postgresql_upgrade_from_446_drops_old_writer_tables_first(
    postgres_writer_engine,
) -> None:
    assert upgrade_database(postgres_writer_engine) == SCHEMA_VERSION
    with Session(postgres_writer_engine) as session:
        ensure_execution_scope_catalog(session)
        session.commit()
    with postgres_writer_engine.begin() as connection:
        connection.execute(text("DROP TABLE okx_order_write_attempts CASCADE"))
        connection.execute(text("DROP TABLE okx_order_writer_leases CASCADE"))
        connection.execute(text("DELETE FROM {}".format(VERSION_TABLE)))
        connection.execute(
            text(
                "INSERT INTO {} (version) VALUES (:version)".format(
                    VERSION_TABLE
                )
            ),
            {"version": ORDER_WRITER_BASE_VERSION},
        )

    assert upgrade_database(postgres_writer_engine) == SCHEMA_VERSION
    assert verify_schema(postgres_writer_engine).ready is True


def test_postgresql_upgrade_refuses_nonempty_prerelease_writer_journal(
    postgres_writer_engine,
) -> None:
    assert upgrade_database(postgres_writer_engine) == SCHEMA_VERSION
    with Session(postgres_writer_engine) as session:
        ensure_execution_scope_catalog(session)
        session.commit()
    with postgres_writer_engine.begin() as connection:
        connection.execute(text("DELETE FROM {}".format(VERSION_TABLE)))
        connection.execute(
            text(
                "INSERT INTO {} (version) VALUES (:version)".format(
                    VERSION_TABLE
                )
            ),
            {"version": ORDER_WRITER_BASE_VERSION},
        )
        connection.execute(
            text(
                "INSERT INTO okx_order_writer_leases ("
                "execution_target_id, holder_token_digest, generation, "
                "acquired_at, heartbeat_at, expires_at"
                ") VALUES ('OKX_DEMO', :digest, 1, :now, :now, :expires)"
            ),
            {
                "digest": "a" * 64,
                "now": NOW,
                "expires": NOW + timedelta(minutes=1),
            },
        )

    with pytest.raises(SchemaMigrationBlocked, match="non-empty"):
        upgrade_database(postgres_writer_engine)

    with postgres_writer_engine.connect() as connection:
        assert connection.execute(
            text("SELECT count(*) FROM okx_order_writer_leases")
        ).scalar_one() == 1


def test_order_writer_migration_refuses_multiple_effective_schemas(
    postgres_writer_engine,
) -> None:
    assert upgrade_database(postgres_writer_engine) == SCHEMA_VERSION
    fallback_schema = "writer_fallback_{}".format(uuid4().hex)
    with postgres_writer_engine.begin() as connection:
        current_schema = connection.execute(
            text("SELECT current_schema()")
        ).scalar_one()
        quoted_fallback = connection.dialect.identifier_preparer.quote(
            fallback_schema
        )
        quoted_current = connection.dialect.identifier_preparer.quote(
            current_schema
        )
        connection.execute(text("CREATE SCHEMA {}".format(quoted_fallback)))
        connection.execute(
            text(
                "SET LOCAL search_path TO {}, {}".format(
                    quoted_current,
                    quoted_fallback,
                )
            )
        )

        with pytest.raises(SchemaMigrationBlocked, match="exactly one"):
            _add_order_writer(connection)


def test_order_writer_migration_refuses_unknown_dependencies(
    postgres_writer_engine,
) -> None:
    assert upgrade_database(postgres_writer_engine) == SCHEMA_VERSION
    with postgres_writer_engine.begin() as connection:
        connection.execute(text("DELETE FROM {}".format(VERSION_TABLE)))
        connection.execute(
            text(
                "INSERT INTO {} (version) VALUES (:version)".format(
                    VERSION_TABLE
                )
            ),
            {"version": ORDER_WRITER_BASE_VERSION},
        )
        connection.execute(
            text(
                "CREATE VIEW writer_lease_dependency AS "
                "SELECT execution_target_id FROM okx_order_writer_leases"
            )
        )

    with pytest.raises(SchemaMigrationBlocked, match="dependencies"):
        upgrade_database(postgres_writer_engine)

    with postgres_writer_engine.connect() as connection:
        assert connection.execute(
            text("SELECT to_regclass('writer_lease_dependency')")
        ).scalar_one() == "writer_lease_dependency"
        assert connection.execute(
            text("SELECT to_regclass('okx_order_writer_leases')")
        ).scalar_one() == "okx_order_writer_leases"


def test_postgresql_partial_unique_rejects_second_unresolved_attempt(
    postgres_writer_engine,
) -> None:
    upgrade_database(postgres_writer_engine)
    with Session(postgres_writer_engine) as session:
        approval_id, order_id = _seed_approved_order(session)
        session.add(
            OkxOrderWriteAttempt(
                execution_target_id="OKX_DEMO",
                exchange_order_row_id=order_id,
                approval_id=approval_id,
                operation="CANCEL",
                operation_id="PgCancel001",
                client_order_id="PgWriterOrder001",
                instrument_id="BTC-USDT-SWAP",
                state="RECOVERY_REQUIRED",
                request_digest="a" * 64,
                safe_request_snapshot={},
                safe_response_snapshot={},
                attempt_count=1,
                lease_generation=1,
                close_sequence=0,
                last_attempt_at=NOW,
            )
        )
        session.commit()
        session.add(
            OkxOrderWriteAttempt(
                execution_target_id="OKX_DEMO",
                exchange_order_row_id=order_id,
                approval_id=approval_id,
                operation="AMEND",
                operation_id="PgAmend001",
                client_order_id="PgWriterOrder001",
                instrument_id="BTC-USDT-SWAP",
                state="PREPARED",
                request_digest="b" * 64,
                safe_request_snapshot={},
                safe_response_snapshot={},
                attempt_count=1,
                lease_generation=1,
                close_sequence=0,
                last_attempt_at=NOW,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_postgresql_partial_predicate_tamper_is_detected(
    postgres_writer_engine,
) -> None:
    upgrade_database(postgres_writer_engine)
    with postgres_writer_engine.begin() as connection:
        connection.execute(
            text("DROP INDEX okx_order_write_attempts_one_unresolved_target_idx")
        )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX "
                "okx_order_write_attempts_one_unresolved_target_idx "
                "ON okx_order_write_attempts (execution_target_id) "
                "WHERE state = 'PREPARED'"
            )
        )

    problems = schema_problems(postgres_writer_engine)
    assert any("missing index" in problem for problem in problems)
    assert any("unexpected unique index" in problem for problem in problems)


def test_postgresql_concurrent_lease_has_one_winner(
    postgres_writer_engine,
) -> None:
    upgrade_database(postgres_writer_engine)
    with Session(postgres_writer_engine) as session:
        approval_id, _order_id = _seed_approved_order(session)
        approval = session.get(ApprovedExecution, approval_id)
        identity = {
            "approval_id": approval_id,
            "canonical_hash": approval.canonical_hash,
            "policy_digest": approval.policy_digest,
            "approved_payload_hash": approval.approved_payload_hash,
        }
    barrier = Barrier(2)

    def contend(name: str) -> str:
        with Session(postgres_writer_engine) as session:
            store = SqlAlchemyOrderWriterStore(session, now_provider=lambda: NOW)
            barrier.wait()
            try:
                store.acquire_lease(
                    writer_instance_id=name,
                    **identity,
                    now=NOW,
                    expires_at=NOW + timedelta(seconds=30),
                )
                return "ACQUIRED"
            except OkxDemoWriteBlocked:
                return "BLOCKED"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(contend, ("WriterInstance01", "WriterInstance02"))
        )

    assert sorted(results) == ["ACQUIRED", "BLOCKED"]


def test_postgresql_one_shot_advisory_lock_fences_api_transaction(
    postgres_writer_engine,
) -> None:
    with Session(postgres_writer_engine) as runtime_session, Session(
        postgres_writer_engine
    ) as api_session:
        assert acquire_one_shot_runtime_lock(runtime_session) is True
        assert try_one_shot_transaction_lock(api_session) is False
        api_session.rollback()
        assert release_one_shot_runtime_lock(runtime_session) is True
        runtime_session.commit()
        assert try_one_shot_transaction_lock(api_session) is True
        api_session.rollback()


def test_postgresql_one_shot_grant_has_one_atomic_journal_winner(
    postgres_writer_engine,
) -> None:
    upgrade_database(postgres_writer_engine)
    with Session(postgres_writer_engine) as session:
        approval_id, _order_id = _seed_approved_order(
            session,
            create_order=False,
            controlled_canary=True,
        )
        approval = session.get(ApprovedExecution, approval_id)
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
        state = session.scalars(
            select(OkxDemoReconciliationState)
            .where(
                OkxDemoReconciliationState.execution_target_id == "OKX_DEMO"
            )
            .with_for_update()
        ).first()
        if state is None:
            state = OkxDemoReconciliationState(execution_target_id="OKX_DEMO")
            session.add(state)
        state.status = "RECONCILED"
        state.opening_frozen = False
        state.block_reason = None
        state.last_event_observed_at = NOW
        state.last_reconciliation_run_id = run.id
        grant_id = uuid4().hex
        session.add(
            OkxDemoSubmissionGrant(
                grant_id=grant_id,
                execution_target_id="OKX_DEMO",
                approval_id=approval_id,
                reconciliation_run_id=run.id,
                canonical_hash=approval.canonical_hash,
                policy_digest=approval.policy_digest,
                approved_payload_hash=approval.approved_payload_hash,
                client_order_id=approval.client_order_id,
                instrument_id="BTC-USDT-SWAP",
                canary_quantity=Decimal("1"),
                canary_notional=Decimal("5.7"),
                request_digest=submission_grant_request_digest(
                    approval_id=approval_id,
                    reconciliation_run_id=run.id,
                    canonical_hash=approval.canonical_hash,
                    policy_digest=approval.policy_digest,
                    approved_payload_hash=approval.approved_payload_hash,
                    client_order_id=approval.client_order_id,
                    instrument_id="BTC-USDT-SWAP",
                    canary_quantity=Decimal("1"),
                    canary_notional=Decimal("5.7"),
                ),
                provenance=CANARY_PROVENANCE,
                status="ACTIVE",
                issued_at=NOW - timedelta(seconds=1),
                expires_at=NOW + timedelta(seconds=10),
            )
        )
        client_order_id = approval.client_order_id
        session.commit()

    barrier = Barrier(2)
    instrument = InstrumentSpec(
        inst_id="BTC-USDT-SWAP",
        inst_type="SWAP",
        base_ccy="BTC",
        quote_ccy="USDT",
        settle_ccy="USDT",
        contract_type="linear",
        contract_value="0.0001",
        contract_value_ccy="BTC",
        lot_size="1",
        min_size="1",
        tick_size="0.1",
        state="live",
    )

    def contend(sequence: int) -> str:
        with Session(postgres_writer_engine) as session:
            store = SqlAlchemyOrderWriterStore(session, now_provider=lambda: NOW)
            claimed = store.load_approved_execution(approval_id)
            authorization = OrderSubmissionAuthorization(
                grant_id=grant_id,
                authorization_mode="ONE_SHOT",
                execution_target_id="OKX_DEMO",
                authorization_schema_version="RISK_V1",
                canonical_hash=claimed.canonical_hash,
                policy_digest=claimed.policy_digest,
                approved_payload_hash=claimed.approved_payload_hash,
                allow_real_funds=False,
                simulated_trading=True,
                order_submission_enabled=True,
                writer_instance_id="PgGrantWriter{:02d}".format(sequence),
                approval_id=approval_id,
                client_order_id=claimed.client_order_id,
                issued_at=NOW - timedelta(seconds=1),
                expires_at=NOW + timedelta(seconds=10),
            )
            command = normalize_order_command(
                claimed,
                submission_grant=authorization,
                instrument=instrument,
                now=NOW,
            )
            barrier.wait()
            try:
                store.acquire_lease(
                    grant_id=grant_id,
                    authorization_mode="ONE_SHOT",
                    writer_instance_id=authorization.writer_instance_id,
                    approval_id=approval_id,
                    canonical_hash=claimed.canonical_hash,
                    policy_digest=claimed.policy_digest,
                    approved_payload_hash=claimed.approved_payload_hash,
                    now=NOW,
                    expires_at=authorization.expires_at,
                )
                store.prepare_place(
                    command,
                    operation="PLACE",
                    operation_id=client_order_id,
                    request_digest="7" * 64,
                    safe_request_snapshot=command.request_body,
                )
                return "PREPARED"
            except OkxDemoWriteBlocked as exc:
                return "BLOCKED: {}".format(exc)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(contend, (1, 2)))

    assert sum(result == "PREPARED" for result in results) == 1, results
    assert sum(result.startswith("BLOCKED: ") for result in results) == 1, results
    with postgres_writer_engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT status FROM okx_demo_submission_grants "
                "WHERE grant_id = :grant_id"
            ),
            {"grant_id": grant_id},
        ).scalar_one() == "CONSUMED"
        assert connection.execute(
            text(
                "SELECT count(*) FROM okx_order_write_attempts "
                "WHERE approval_id = :approval_id AND operation = 'PLACE'"
            ),
            {"approval_id": approval_id},
        ).scalar_one() == 1
    with pytest.raises(SQLAlchemyError):
        with postgres_writer_engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE freqtrade"))
            connection.execute(
                text(
                    "UPDATE okx_demo_submission_grants "
                    "SET canonical_hash = :tampered WHERE grant_id = :grant_id"
                ),
                {"tampered": "f" * 64, "grant_id": grant_id},
            )
    with pytest.raises(SQLAlchemyError):
        with postgres_writer_engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE freqtrade"))
            connection.execute(
                text(
                    "UPDATE okx_demo_submission_grants "
                    "SET status = 'ACTIVE', consumed_at = NULL "
                    "WHERE grant_id = :grant_id"
                ),
                {"grant_id": grant_id},
            )


def test_postgresql_runtime_role_can_validate_canary_lineage_without_update_acl(
    postgres_writer_engine,
    monkeypatch,
) -> None:
    """Canary read checks use advisory serialization, not table UPDATE ACL."""

    upgrade_database(postgres_writer_engine)
    with Session(postgres_writer_engine) as session:
        approval_id, _order_id = _seed_approved_order(
            session,
            create_order=False,
            controlled_canary=True,
        )
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
        state = session.scalars(
            select(OkxDemoReconciliationState).where(
                OkxDemoReconciliationState.execution_target_id == "OKX_DEMO"
            )
        ).one()
        state.status = "RECONCILED"
        state.opening_frozen = False
        state.block_reason = None
        state.last_event_observed_at = NOW
        state.last_reconciliation_run_id = run.id
        session.commit()
        reconciliation_run_id = run.id
        approval = session.get(ApprovedExecution, approval_id)

    # Keep the least-privilege boundary explicit: these tables are readable by
    # the runtime role but do not grant table-level UPDATE merely for a read
    # lock.  The one-shot grant table permits only its narrow transition
    # columns, which is checked separately by schema verification.
    with postgres_writer_engine.connect() as connection:
        for table_name in (
            "okx_demo_reconciliation_states",
            "reconciliation_runs",
            "approved_executions",
            "trade_intents",
            "risk_decisions",
            "okx_demo_submission_grants",
        ):
            assert connection.execute(
                text(
                    "SELECT has_table_privilege('freqtrade', :table_name, 'SELECT')"
                ),
                {"table_name": table_name},
            ).scalar_one() is True
            assert connection.execute(
                text(
                    "SELECT has_table_privilege('freqtrade', :table_name, 'UPDATE')"
                ),
                {"table_name": table_name},
            ).scalar_one() is False

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

    with Session(postgres_writer_engine) as runtime_session:
        runtime_session.execute(text("SET LOCAL ROLE freqtrade"))
        # This is the exact refresh-preparation read path.  It must not emit
        # SELECT ... FOR UPDATE against the reconciliation tables.
        preparation = OkxDemoCanaryPreparationService(
            runtime_session,
            now_provider=lambda: NOW,
        )
        assert preparation._fresh_empty_reconciliation(NOW) == reconciliation_run_id
        assert require_canary_reconciliation(
            runtime_session,
            reconciliation_run_id=reconciliation_run_id,
            now=NOW,
            for_update=True,
        ).id == run.id

        # The grant arm traverses approved/intent/decision lineage, the state
        # row, and the run row under the same transaction-scoped advisory lock.
        # Before #616 the first FOR UPDATE failed here with
        # InsufficientPrivilege; this call now exercises the real runtime role.
        grant = OkxDemoSubmissionGrantService(
            runtime_session,
            now_provider=lambda: NOW,
        ).arm(
            approval_id=approval_id,
            canonical_hash=approval.canonical_hash,
            policy_digest=approval.policy_digest,
            approved_payload_hash=approval.approved_payload_hash,
            client_order_id=approval.client_order_id,
        )
        assert grant.execution_target_id == "OKX_DEMO"
        assert grant.status == "ACTIVE"

        # The writer's one-shot lineage recheck uses its own advisory writer
        # key and must likewise avoid FOR UPDATE on the grant table.
        runtime_session.execute(text("SET LOCAL ROLE freqtrade"))
        runtime_approval = runtime_session.get(ApprovedExecution, approval_id)
        writer_store = SqlAlchemyOrderWriterStore(
            runtime_session,
            now_provider=lambda: NOW,
        )
        writer_store._lock_lease_key()
        validated = writer_store._validate_one_shot_grant(
            grant_id=grant.grant_id,
            approved=runtime_approval,
            canonical_hash=grant.canonical_hash,
            policy_digest=grant.policy_digest,
            approved_payload_hash=grant.approved_payload_hash,
            now=NOW,
        )
        assert validated.grant_id == grant.grant_id


def _postgres_position_event(quantity: str, sequence: int) -> dict:
    return {
        "schema_version": RECONCILIATION_EVENT_SCHEMA_VERSION,
        "execution_target": "OKX_DEMO",
        "source": "WS",
        "entity_kind": "POSITION",
        "entity_key": "BTC-USDT-SWAP:long",
        "source_sequence": sequence,
        "stream_generation": 1,
        "observed_at": NOW.isoformat(),
        "received_at": (NOW + timedelta(seconds=1)).isoformat(),
        "payload": {
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "pos": quantity,
            "avgPx": "50000" if quantity != "0" else "",
        },
    }


def test_postgresql_concurrent_same_timestamp_different_digest_has_one_winner(
    postgres_writer_engine,
) -> None:
    upgrade_database(postgres_writer_engine)
    barrier = Barrier(2)

    def ingest(quantity: str, sequence: int) -> str:
        with Session(postgres_writer_engine) as session:
            service = OkxDemoReconciliationService(session)
            barrier.wait()
            try:
                service.ingest_event(
                    _postgres_position_event(quantity, sequence)
                )
                session.commit()
                return "PERSISTED"
            except OkxDemoReconciliationBlocked:
                session.commit()
                return "BLOCKED"

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(ingest, "0", 1),
            executor.submit(ingest, "1", 2),
        ]
        results = [future.result(timeout=20) for future in futures]

    assert sorted(results) == ["BLOCKED", "PERSISTED"]
    with Session(postgres_writer_engine) as session:
        rows = session.scalars(select(OkxDemoExchangeEvent)).all()
        assert len(rows) == 1
        assert rows[0].payload["pos"] in {"0", "1"}


def test_postgresql_rejects_ws_null_sequence_at_database_boundary(
    postgres_writer_engine,
) -> None:
    upgrade_database(postgres_writer_engine)
    with pytest.raises(IntegrityError):
        with postgres_writer_engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE freqtrade"))
            connection.execute(
                text(
                    """
                    INSERT INTO okx_demo_exchange_events (
                        execution_target_id, event_key, source, entity_kind,
                        entity_key, source_sequence, stream_generation,
                        payload, payload_digest, observed_at, received_at
                    ) VALUES (
                        'OKX_DEMO', :event_key, 'WS', 'ACCOUNT', 'account',
                        NULL, 1, '{}'::jsonb, :payload_digest, :observed_at,
                        :received_at
                    )
                    """
                ),
                {
                    "event_key": "a" * 64,
                    "payload_digest": "b" * 64,
                    "observed_at": NOW,
                    "received_at": NOW,
                },
            )


def test_postgresql_reconciliation_evidence_is_append_only_for_runtime_role(
    postgres_writer_engine,
) -> None:
    upgrade_database(postgres_writer_engine)
    with Session(postgres_writer_engine) as session:
        event = OkxDemoReconciliationService(session).ingest_event(
            _postgres_position_event("0", 1)
        )
        session.commit()
        event_database_id = event.database_id

    with postgres_writer_engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT has_table_privilege("
                "'freqtrade', 'okx_demo_exchange_events', 'UPDATE')"
            )
        ).scalar_one() is False
        assert connection.execute(
            text(
                "SELECT has_table_privilege("
                "'freqtrade', 'okx_demo_position_snapshots', 'DELETE')"
            )
        ).scalar_one() is False

    with pytest.raises(SQLAlchemyError):
        with postgres_writer_engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE freqtrade"))
            connection.execute(
                text(
                    "UPDATE okx_demo_exchange_events "
                    "SET entity_key = 'tampered' "
                    "WHERE database_id = :database_id"
                ),
                {"database_id": event_database_id},
            )

    with pytest.raises(SQLAlchemyError):
        with postgres_writer_engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE freqtrade"))
            connection.execute(
                text(
                    "DELETE FROM okx_demo_position_snapshots "
                    "WHERE event_database_id = :database_id"
                ),
                {"database_id": event_database_id},
            )

    with Session(postgres_writer_engine) as session:
        row = session.get(OkxDemoExchangeEvent, event_database_id)
        assert row is not None
        assert row.entity_key == "BTC-USDT-SWAP:long"


def test_postgresql_runtime_cannot_bypass_controlled_gate_or_run_provenance(
    postgres_writer_engine,
    tmp_path,
) -> None:
    upgrade_database(postgres_writer_engine)
    with postgres_writer_engine.begin() as connection:
        connection.execute(text("SET LOCAL ROLE freqtrade"))
        assert connection.execute(
            text(
                "SELECT count(*) FROM execution_scopes "
                "WHERE scope_id IN "
                "('OKX_DEMO', 'LOCAL_DRY_RUN', 'UNKNOWN_LEGACY')"
            )
        ).scalar_one() == 3
        for table_name in (
            "exchange_orders",
            "exchange_fills",
            "exchange_positions",
        ):
            assert connection.execute(
                text(
                    "SELECT has_table_privilege("
                    "'freqtrade', :table_name, 'SELECT')"
                ),
                {"table_name": table_name},
            ).scalar_one() is True
            assert connection.execute(
                text(
                    "SELECT has_table_privilege("
                    "'freqtrade', :table_name, 'INSERT') "
                    "OR has_table_privilege("
                    "'freqtrade', :table_name, 'UPDATE') "
                    "OR has_table_privilege("
                    "'freqtrade', :table_name, 'DELETE')"
                ),
                {"table_name": table_name},
            ).scalar_one() is False
    for statement in (
        "INSERT INTO execution_scopes ("
        "scope_id, scope_kind, exchange_capable, executable, "
        "exchange_writes, order_submission_authorized"
        ") VALUES ('FORGED', 'LEGACY', FALSE, FALSE, FALSE, FALSE)",
        "UPDATE execution_scopes SET executable = TRUE "
        "WHERE scope_id = 'OKX_DEMO'",
        "DELETE FROM execution_scopes WHERE scope_id = 'OKX_DEMO'",
        "UPDATE exchange_orders SET client_order_id = 'tampered' WHERE FALSE",
        "DELETE FROM exchange_fills WHERE FALSE",
        "INSERT INTO exchange_positions ("
        "execution_target_id, instrument_id, position_side, quantity, "
        "snapshot, observed_at"
        ") VALUES ('OKX_DEMO', 'BTC-USDT-SWAP', 'long', 0, "
        "'{}'::jsonb, CURRENT_TIMESTAMP)",
    ):
        with pytest.raises(SQLAlchemyError):
            with postgres_writer_engine.begin() as connection:
                connection.execute(text("SET LOCAL ROLE freqtrade"))
                connection.execute(text(statement))
    with Session(postgres_writer_engine) as session:
        session.execute(text("SET LOCAL ROLE freqtrade"))
        # PostgreSQL CURRENT_TIMESTAMP is fixed when this transaction begins.
        # A real authenticated REST capture completes later in the same
        # transaction, so the gate must compare it with the wall clock.
        time.sleep(0.05)
        now = datetime.now(timezone.utc)
        historical_business_time = now - timedelta(days=1)
        position_event = {
            **_postgres_position_event("0", 1),
            "source": "REST",
            "observed_at": historical_business_time.isoformat(),
            "received_at": now.isoformat(),
        }
        account_event = {
            "schema_version": RECONCILIATION_EVENT_SCHEMA_VERSION,
            "execution_target": "OKX_DEMO",
            "source": "REST",
            "entity_kind": "ACCOUNT",
            "entity_key": "account",
            "source_sequence": 2,
            "stream_generation": 1,
            "observed_at": historical_business_time.isoformat(),
            "received_at": now.isoformat(),
            "payload": {
                "accountFingerprint": "a" * 64,
                "equity": "10000",
                "availableBalance": "9000",
                "marginBalance": "1000",
            },
        }
        service = OkxDemoReconciliationService(
            session,
            evidence_root=tmp_path / "managed" / "reconciliation",
            allowed_evidence_root=tmp_path / "managed",
        )
        service.ingest_recovery_batch(
            [position_event, account_event],
            recovery_batch_id="runtime-controlled-gate",
            high_watermarks={
                "ORDER": "orders-end",
                "FILL": "fills-end",
                "POSITION": "positions-end",
                "ACCOUNT": "account-end",
            },
            overlap_started_at=now - timedelta(seconds=5),
            observed_at=now,
            completed_at=now,
        )
        result = service.reconcile(now=now)
        session.commit()
        assert result.status == "RECONCILED"
        assert result.opening_frozen is False
        ready_run_id = result.reconciliation_run_database_id

    with Session(postgres_writer_engine) as session:
        state = session.query(OkxDemoReconciliationState).one()
        ready_run = session.get(ReconciliationRun, ready_run_id)
        assert state.status == "RECONCILED"
        assert state.opening_frozen is False
        assert ready_run.artifact_status == "READY"
        assert ready_run.authoritative_observed_at == historical_business_time
        ready_digest = ready_run.artifact_sha256

    for statement in (
        "UPDATE okx_demo_reconciliation_states "
        "SET status = 'RECONCILED', opening_frozen = FALSE",
        "UPDATE reconciliation_runs SET status = 'RECONCILED', "
        "artifact_sha256 = repeat('0', 64) WHERE id = {}".format(
            ready_run_id
        ),
    ):
        with pytest.raises(SQLAlchemyError):
            with postgres_writer_engine.begin() as connection:
                connection.execute(text("SET LOCAL ROLE freqtrade"))
                connection.execute(text(statement))

    with postgres_writer_engine.begin() as connection:
        connection.execute(text("SET LOCAL ROLE freqtrade"))
        pending_run_id = connection.execute(
            text(
                """
                INSERT INTO reconciliation_runs (
                    execution_target_id, status, summary_snapshot,
                    database_ids, artifact_status,
                    authoritative_observed_at, source_type, core_data,
                    started_at, completed_at
                ) VALUES (
                    'OKX_DEMO', 'UNKNOWN', '{}'::jsonb, '{}'::jsonb,
                    'PENDING', :now, 'api_aggregate', TRUE, :now, :now
                ) RETURNING id
                """
            ),
            {"now": now},
        ).scalar_one()

    with pytest.raises(SQLAlchemyError):
        with postgres_writer_engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE freqtrade"))
            connection.execute(
                text(
                    "SELECT apply_okx_demo_reconciliation_gate(:run_id)"
                ),
                {"run_id": pending_run_id},
            )

    with pytest.raises(SQLAlchemyError):
        with postgres_writer_engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE freqtrade"))
            connection.execute(
                text(
                    """
                    SELECT finalize_okx_demo_reconciliation_run(
                        :run_id, '{}'::jsonb, '{}'::jsonb,
                        '/tmp/forged.json', :digest
                    )
                    """
                ),
                {"run_id": pending_run_id, "digest": "0" * 64},
            )

    with pytest.raises(SQLAlchemyError):
        with postgres_writer_engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE freqtrade"))
            connection.execute(
                text(
                    """
                    SELECT finalize_okx_demo_reconciliation_run(
                        :run_id, '{}'::jsonb, '{}'::jsonb,
                        '/tmp/forged.json', :digest
                    )
                    """
                ),
                {"run_id": ready_run_id, "digest": "0" * 64},
            )

    with Session(postgres_writer_engine) as session:
        state = session.query(OkxDemoReconciliationState).one()
        ready_run = session.execute(
            text(
                "SELECT status, artifact_status, artifact_sha256 "
                "FROM reconciliation_runs WHERE id = :run_id"
            ),
            {"run_id": ready_run_id},
        ).one()
        assert state.status == "RECONCILED"
        assert state.opening_frozen is False
        assert tuple(ready_run) == ("RECONCILED", "READY", ready_digest)


def test_postgresql_runtime_role_completes_real_writer_happy_lifecycle(
    postgres_writer_engine,
) -> None:
    upgrade_database(postgres_writer_engine)
    with Session(postgres_writer_engine) as admin_session:
        approval_id, _ = _seed_approved_order(
            admin_session,
            create_order=False,
        )
        approval = admin_session.get(ApprovedExecution, approval_id)
        identity = {
            "canonical_hash": approval.canonical_hash,
            "policy_digest": approval.policy_digest,
            "approved_payload_hash": approval.approved_payload_hash,
        }
        admin_session.execute(
            text(
                "UPDATE okx_demo_reconciliation_states "
                "SET status = 'RECONCILED', opening_frozen = FALSE, "
                "block_reason = NULL"
            )
        )
        admin_session.commit()

    instrument = InstrumentSpec(
        inst_id="BTC-USDT-SWAP",
        inst_type="SWAP",
        base_ccy="BTC",
        quote_ccy="USDT",
        settle_ccy="USDT",
        contract_type="linear",
        contract_value="1",
        contract_value_ccy="BTC",
        lot_size="1",
        min_size="1",
        tick_size="0.1",
        state="live",
    )
    with Session(postgres_writer_engine) as session:
        store = SqlAlchemyOrderWriterStore(
            session,
            now_provider=lambda: NOW,
        )
        session.execute(text("SET LOCAL ROLE freqtrade"))
        claimed = store.load_approved_execution(approval_id)
        authorization = OrderSubmissionAuthorization(
            grant_id="1" * 32,
            execution_target_id="OKX_DEMO",
            authorization_schema_version="RISK_V1",
            canonical_hash=claimed.canonical_hash,
            policy_digest=claimed.policy_digest,
            approved_payload_hash=claimed.approved_payload_hash,
            allow_real_funds=False,
            simulated_trading=True,
            order_submission_enabled=True,
            writer_instance_id="PgRuntimeWriter01",
            approval_id=approval_id,
            client_order_id=claimed.client_order_id,
            issued_at=NOW - timedelta(seconds=1),
            expires_at=NOW + timedelta(seconds=10),
        )
        command = normalize_order_command(
            claimed,
            submission_grant=authorization,
            instrument=instrument,
            now=NOW,
        )

        session.execute(text("SET LOCAL ROLE freqtrade"))
        store.acquire_lease(
            writer_instance_id="PgRuntimeWriter01",
            approval_id=approval_id,
            now=NOW,
            expires_at=NOW + timedelta(minutes=1),
            **identity,
        )
        session.execute(text("SET LOCAL ROLE freqtrade"))
        assert store.unresolved() is None

        session.execute(text("SET LOCAL ROLE freqtrade"))
        order, prepared = store.prepare_place(
            command,
            operation="PLACE",
            operation_id=command.client_order_id,
            request_digest="a" * 64,
            safe_request_snapshot=command.request_body,
        )
        assert order.exchange_order_row_id > 0
        session.execute(text("SET LOCAL ROLE freqtrade"))
        acknowledged = store.transition(
            prepared,
            event=WriteEvent.ACKNOWLEDGE,
            exchange_order_id="okx-demo-order-1001",
        )
        session.execute(text("SET LOCAL ROLE freqtrade"))
        reconciled = store.transition(
            acknowledged,
            event=WriteEvent.RECONCILE,
            order_state="live",
            safe_response_snapshot={
                "order_id": "okx-demo-order-1001",
                "state": "live",
            },
        )
        assert reconciled.state.value == "RECONCILED"

    with Session(postgres_writer_engine) as session:
        order = session.scalar(
            select(ExchangeOrder).where(
                ExchangeOrder.client_order_id == "PgWriterOrder001"
            )
        )
        assert order is not None
        assert order.execution_target_id == "OKX_DEMO"
        assert order.exchange_order_id == "okx-demo-order-1001"
        assert order.status == "live"
        order_id = order.id

    for statement, parameters in (
        (
            "UPDATE exchange_orders SET status = 'FORGED' "
            "WHERE id = :order_id",
            {"order_id": order_id},
        ),
        (
            "UPDATE exchange_orders "
            "SET exchange_order_id = 'replacement-order-id' "
            "WHERE id = :order_id",
            {"order_id": order_id},
        ),
    ):
        with pytest.raises(SQLAlchemyError):
            with postgres_writer_engine.begin() as connection:
                connection.execute(text("SET LOCAL ROLE freqtrade"))
                connection.execute(text(statement), parameters)

    with pytest.raises(SQLAlchemyError):
        with postgres_writer_engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE freqtrade"))
            connection.execute(
                text(
                    """
                    INSERT INTO exchange_orders (
                        execution_target_id, trade_intent_id,
                        client_order_id, exchange_order_id, status,
                        request_snapshot, response_snapshot
                    )
                    SELECT 'OKX_DEMO', trade_intent_id,
                           'PgForgedOrder001', 'prebound-order',
                           'PREPARED', '{}'::json, '{}'::json
                    FROM approved_executions
                    WHERE id = :approval_id
                    """
                ),
                {"approval_id": approval_id},
            )


def test_postgresql_runtime_role_releases_expired_approval_budget(
    postgres_writer_engine,
) -> None:
    upgrade_database(postgres_writer_engine)
    with postgres_writer_engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT bool_and(owner.rolname = 'freqtrade_ai_attestor') "
                "FROM pg_class AS relation JOIN pg_roles AS owner "
                "ON owner.oid = relation.relowner "
                "WHERE relation.relname IN ("
                "'approved_executions','trade_intents','risk_decisions')"
            )
        ).scalar_one() is True
        assert connection.execute(
            text(
                "SELECT has_column_privilege('freqtrade_ai_attestor', "
                "'full_chain_runs', 'status', 'UPDATE') AND "
                "has_column_privilege('freqtrade_ai_attestor', "
                "'risk_budgets', 'reserved_notional', 'UPDATE') AND "
                "NOT EXISTS (SELECT 1 FROM pg_class AS relation "
                "CROSS JOIN LATERAL aclexplode(COALESCE("
                "relation.relacl, acldefault('r', relation.relowner))) AS acl "
                "WHERE relation.relname IN ('full_chain_runs','risk_budgets') "
                "AND acl.grantee = (SELECT oid FROM pg_roles "
                "WHERE rolname = 'freqtrade_ai_attestor') "
                "AND acl.privilege_type IN "
                "('SELECT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER'))"
            )
        ).scalar_one() is True
    with Session(postgres_writer_engine) as session:
        approval_id, _ = _seed_approved_order(
            session,
            create_order=False,
        )
        approval = session.get(ApprovedExecution, approval_id)
        intent_id = approval.trade_intent_id
        decision_id = approval.risk_decision_id
        session.add(
            RiskBudget(
                execution_target_id="OKX_DEMO",
                reserved_notional=approval.reserved_notional,
                approved_positions=1,
            )
        )
        session.execute(
            text(
                "UPDATE approved_executions SET expires_at = :expired "
                "WHERE id = :approval_id"
            ),
            {"expired": NOW - timedelta(seconds=1), "approval_id": approval_id},
        )
        session.commit()

    with Session(postgres_writer_engine) as session:
        session.execute(text("SET LOCAL ROLE freqtrade"))
        store = SqlAlchemyOrderWriterStore(
            session,
            now_provider=lambda: datetime.now(timezone.utc),
        )
        with pytest.raises(
            OkxDemoWriteBlocked,
            match="no longer active",
        ):
            store.load_approved_execution(approval_id)

    with Session(postgres_writer_engine) as session:
        retained = session.get(ApprovedExecution, approval_id)
        assert retained is not None and retained.status == "EXPIRED"
        assert session.get(TradeIntent, intent_id).status == "APPROVED"
        assert session.get(RiskDecision, decision_id).decision == "APPROVED"
        budget = session.get(RiskBudget, "OKX_DEMO")
        assert budget.reserved_notional == 0
        assert budget.approved_positions == 0


def test_postgresql_writer_blocks_incomplete_full_chain_risk_checkpoint(
    postgres_writer_engine,
) -> None:
    upgrade_database(postgres_writer_engine)
    with Session(postgres_writer_engine) as session:
        approval_id, _ = _seed_approved_order(
            session,
            create_order=False,
        )
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

    with Session(postgres_writer_engine) as session:
        session.execute(text("SET LOCAL ROLE freqtrade"))
        store = SqlAlchemyOrderWriterStore(
            session,
            now_provider=lambda: datetime.now(timezone.utc),
        )
        with pytest.raises(
            OkxDemoWriteBlocked,
            match="full-chain RISK checkpoint is incomplete",
        ):
            store.load_approved_execution(approval_id)
        assert session.scalar(
            select(func.count()).select_from(OkxOrderWriteAttempt)
        ) == 0


def test_postgresql_recovery_grant_and_cancel_attempt_commit_together(
    postgres_writer_engine,
    tmp_path,
) -> None:
    upgrade_database(postgres_writer_engine)
    with Session(postgres_writer_engine) as session:
        _approval_id, order_id = _seed_approved_order(session)
        order = session.get(ExchangeOrder, order_id)
        order.exchange_order_id = "okx-recovery-order-1"
        order.status = "live"
        session.commit()

    order_event = {
        "schema_version": RECONCILIATION_EVENT_SCHEMA_VERSION,
        "execution_target": "OKX_DEMO",
        "source": "REST",
        "entity_kind": "ORDER",
        "entity_key": "okx-recovery-order-1",
        "source_sequence": 1,
        "stream_generation": 1,
        "observed_at": NOW.isoformat(),
        "received_at": NOW.isoformat(),
        "payload": {
            "ordId": "okx-recovery-order-1",
            "clOrdId": "PgWriterOrder001",
            "instId": "BTC-USDT-SWAP",
            "state": "live",
            "sz": "1",
            "accFillSz": "0",
            "avgPx": "",
            "reduceOnly": False,
        },
    }
    position_event = {
        **_postgres_position_event("1", 2),
        "source": "REST",
        "observed_at": NOW.isoformat(),
        "received_at": NOW.isoformat(),
    }
    account_event = {
        "schema_version": RECONCILIATION_EVENT_SCHEMA_VERSION,
        "execution_target": "OKX_DEMO",
        "source": "REST",
        "entity_kind": "ACCOUNT",
        "entity_key": "account",
        "source_sequence": 3,
        "stream_generation": 1,
        "observed_at": NOW.isoformat(),
        "received_at": NOW.isoformat(),
        "payload": {
            "accountFingerprint": "a" * 64,
            "equity": "10000",
            "availableBalance": "9000",
            "marginBalance": "1000",
        },
    }
    with Session(postgres_writer_engine) as session:
        session.execute(text("SET LOCAL ROLE freqtrade"))
        service = OkxDemoReconciliationService(
            session,
            evidence_root=tmp_path / "managed" / "reconciliation",
            allowed_evidence_root=tmp_path / "managed",
        )
        service.ingest_recovery_batch(
            [order_event, position_event, account_event],
            recovery_batch_id="atomic-recovery-writer",
            high_watermarks={
                "ORDER": "orders-end",
                "FILL": "fills-end",
                "POSITION": "positions-end",
                "ACCOUNT": "account-end",
            },
            overlap_started_at=NOW - timedelta(seconds=5),
            observed_at=NOW,
            completed_at=NOW,
        )
        result = service.reconcile(now=NOW)
        session.commit()
        assert result.status == "DRIFTED"

    with Session(postgres_writer_engine) as session:
        cancel_grant = session.scalar(
            select(OkxDemoRecoveryGrant).where(
                OkxDemoRecoveryGrant.action == "CANCEL"
            )
        )
        assert cancel_grant is not None
        grant_id = cancel_grant.database_id

    with Session(postgres_writer_engine) as session:
        session.execute(text("SET LOCAL ROLE freqtrade"))
        store = SqlAlchemyOrderWriterStore(
            session,
            now_provider=lambda: NOW,
        )
        store.acquire_recovery_lease(grant_id, now=NOW)
        managed = store.load_recovery_order(grant_id)
        prepared = store.prepare_existing(
            managed,
            operation="CANCEL",
            operation_id=managed.client_order_id,
            request_digest="a" * 64,
            safe_request_snapshot={
                "instId": managed.instrument_id,
                "clOrdId": managed.client_order_id,
            },
            recovery_grant_database_id=grant_id,
        )
        assert prepared.recovery_grant_database_id == grant_id

    with Session(postgres_writer_engine) as session:
        persisted_grant = session.get(OkxDemoRecoveryGrant, grant_id)
        persisted_attempt = session.scalar(
            select(OkxOrderWriteAttempt).where(
                OkxOrderWriteAttempt.recovery_grant_database_id == grant_id
            )
        )
        assert persisted_grant.status == "CONSUMED"
        assert persisted_attempt is not None
        assert persisted_attempt.state == "PREPARED"
        persisted_attempt.state = "RECONCILED"
        session.execute(text("DELETE FROM okx_order_writer_leases"))
        reduce_grant = session.scalar(
            select(OkxDemoRecoveryGrant).where(
                OkxDemoRecoveryGrant.action == "REDUCE_ONLY"
            )
        )
        reduce_grant_id = reduce_grant.database_id
        session.commit()

    with Session(postgres_writer_engine) as session:
        session.execute(text("SET LOCAL ROLE freqtrade"))
        store = SqlAlchemyOrderWriterStore(
            session,
            now_provider=lambda: NOW,
        )
        store.acquire_recovery_lease(reduce_grant_id, now=NOW)
        close_order, close_attempt, body = store.prepare_recovery_close(
            reduce_grant_id
        )
        assert close_attempt.recovery_grant_database_id == reduce_grant_id
        assert close_order.client_order_id == "rcv{:020d}".format(
            reduce_grant_id
        )
        assert body["reduceOnly"] is True
        assert body["side"] == "sell"
        assert body["posSide"] == "long"

    with Session(postgres_writer_engine) as session:
        assert session.get(
            OkxDemoRecoveryGrant,
            reduce_grant_id,
        ).status == "CONSUMED"
        assert session.scalar(
            select(OkxOrderWriteAttempt).where(
                OkxOrderWriteAttempt.recovery_grant_database_id
                == reduce_grant_id
            )
        ) is not None


def test_soak_base_version_upgrades_in_place_and_is_append_only(
    postgres_writer_engine,
) -> None:
    assert upgrade_database(postgres_writer_engine) == SCHEMA_VERSION
    with postgres_writer_engine.begin() as connection:
        for table_name in (
            "okx_demo_soak_events",
            "okx_demo_soak_probes",
            "okx_demo_soak_runs",
        ):
            connection.execute(text("DROP TABLE {}".format(table_name)))
        connection.execute(text("DELETE FROM {}".format(VERSION_TABLE)))
        connection.execute(
            text(
                "INSERT INTO {} (version) VALUES (:version)".format(
                    VERSION_TABLE
                )
            ),
            {"version": SOAK_BASE_VERSION},
        )

    assert upgrade_database(postgres_writer_engine) == SCHEMA_VERSION
    assert verify_schema(postgres_writer_engine).ready is True
    with postgres_writer_engine.connect() as connection:
        privileges = {
            table_name: {
                privilege: connection.execute(
                    text(
                        "SELECT has_table_privilege("
                        "'freqtrade', :table_name, :privilege)"
                    ),
                    {"table_name": table_name, "privilege": privilege},
                ).scalar_one()
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE")
            }
            for table_name in (
                "okx_demo_soak_runs",
                "okx_demo_soak_probes",
                "okx_demo_soak_events",
            )
        }
    assert privileges["okx_demo_soak_runs"] == {
        "SELECT": True,
        "INSERT": True,
        "UPDATE": True,
        "DELETE": False,
    }
    for table_name in ("okx_demo_soak_probes", "okx_demo_soak_events"):
        assert privileges[table_name] == {
            "SELECT": True,
            "INSERT": True,
            "UPDATE": False,
            "DELETE": False,
        }


def test_runtime_application_acl_upgrades_in_place_without_widening_sensitive_tables(
    postgres_writer_engine,
) -> None:
    assert upgrade_database(postgres_writer_engine) == SCHEMA_VERSION
    privileges = ("SELECT", "INSERT", "UPDATE", "DELETE")
    with postgres_writer_engine.begin() as connection:
        specialized_tables = tuple(
            sorted(
                set(inspect(connection).get_table_names())
                - set(RUNTIME_APPLICATION_TABLES)
                - {VERSION_TABLE}
            )
        )
        sensitive_before = {
            table_name: tuple(
                connection.execute(
                    text(
                        "SELECT has_table_privilege("
                        "'freqtrade', :table_name, :privilege)"
                    ),
                    {"table_name": table_name, "privilege": privilege},
                ).scalar_one()
                for privilege in privileges
            )
            for table_name in specialized_tables
        }
        for table_name in RUNTIME_APPLICATION_TABLES:
            connection.execute(
                text(
                    "REVOKE ALL ON TABLE {} FROM freqtrade".format(table_name)
                )
            )
            sequence_identity = (
                connection.execute(
                    text("SELECT pg_get_serial_sequence(:table_name, 'id')"),
                    {"table_name": table_name},
                ).scalar_one()
                if "id"
                in {
                    column["name"]
                    for column in inspect(connection).get_columns(table_name)
                }
                else None
            )
            if sequence_identity:
                connection.execute(
                    text(
                        "REVOKE ALL ON SEQUENCE {} FROM freqtrade".format(
                            sequence_identity
                        )
                    )
                )
        connection.execute(
            text(
                "GRANT SELECT (name) ON TABLE strategies TO PUBLIC; "
                "GRANT UPDATE (name) ON TABLE strategies TO freqtrade"
            )
        )
        connection.execute(text("DELETE FROM {}".format(VERSION_TABLE)))
        connection.execute(
            text(
                "INSERT INTO {} (version) VALUES (:version)".format(
                    VERSION_TABLE
                )
            ),
            {"version": RUNTIME_APP_ACL_BASE_VERSION},
        )

    assert upgrade_database(postgres_writer_engine) == SCHEMA_VERSION
    assert verify_schema(postgres_writer_engine).ready is True
    with postgres_writer_engine.connect() as connection:
        for table_name in RUNTIME_APPLICATION_TABLES:
            assert {
                privilege: connection.execute(
                    text(
                        "SELECT has_table_privilege("
                        "'freqtrade', :table_name, :privilege)"
                    ),
                    {"table_name": table_name, "privilege": privilege},
                ).scalar_one()
                for privilege in privileges
            } == {
                "SELECT": True,
                "INSERT": True,
                "UPDATE": True,
                "DELETE": False,
            }
            sequence_identity = (
                connection.execute(
                    text("SELECT pg_get_serial_sequence(:table_name, 'id')"),
                    {"table_name": table_name},
                ).scalar_one()
                if "id"
                in {
                    column["name"]
                    for column in inspect(connection).get_columns(table_name)
                }
                else None
            )
            if sequence_identity:
                assert connection.execute(
                    text(
                        "SELECT has_sequence_privilege("
                        "'freqtrade', :sequence_name, 'USAGE') "
                        "AND has_sequence_privilege("
                        "'freqtrade', :sequence_name, 'SELECT')"
                    ),
                    {"sequence_name": sequence_identity},
                ).scalar_one() is True
        sensitive_after = {
            table_name: tuple(
                connection.execute(
                    text(
                        "SELECT has_table_privilege("
                        "'freqtrade', :table_name, :privilege)"
                    ),
                    {"table_name": table_name, "privilege": privilege},
                ).scalar_one()
                for privilege in privileges
            )
            for table_name in specialized_tables
        }
        explicit_application_column_acl = connection.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_attribute AS attribute
                    CROSS JOIN LATERAL aclexplode(attribute.attacl) AS acl
                    WHERE attribute.attrelid = 'strategies'::regclass
                      AND attribute.attname = 'name'
                      AND acl.grantee IN (
                          0,
                          (SELECT oid FROM pg_roles
                           WHERE rolname = 'freqtrade')
                      )
                )
                """
            )
        ).scalar_one()
    assert sensitive_after == sensitive_before
    assert explicit_application_column_acl is False

    with postgres_writer_engine.begin() as connection:
        connection.execute(
            text("REVOKE SELECT ON TABLE research_jobs FROM freqtrade")
        )
    readiness = verify_schema(postgres_writer_engine)
    assert readiness.ready is False
    assert "runtime application DML privilege missing: research_jobs" in (
        readiness.problems
    )
    with postgres_writer_engine.begin() as connection:
        connection.execute(text("DELETE FROM {}".format(VERSION_TABLE)))
        connection.execute(
            text(
                "INSERT INTO {} (version) VALUES (:version)".format(
                    VERSION_TABLE
                )
            ),
            {"version": RUNTIME_APP_ACL_BASE_VERSION},
        )
    assert upgrade_database(postgres_writer_engine) == SCHEMA_VERSION
    assert upgrade_database(postgres_writer_engine) == SCHEMA_VERSION
    assert verify_schema(postgres_writer_engine).ready is True


def test_reconciliation_batch_index_upgrades_in_place_and_supports_runtime_query(
    postgres_writer_engine,
) -> None:
    index_name = "okx_demo_exchange_events_batch_observed_idx"
    assert upgrade_database(postgres_writer_engine) == SCHEMA_VERSION
    with postgres_writer_engine.begin() as connection:
        connection.execute(text("DROP INDEX {}".format(index_name)))
        connection.execute(text("DELETE FROM {}".format(VERSION_TABLE)))
        connection.execute(
            text(
                "INSERT INTO {} (version) VALUES (:version)".format(
                    VERSION_TABLE
                )
            ),
            {"version": RECONCILIATION_INDEX_BASE_VERSION},
        )

    assert upgrade_database(postgres_writer_engine) == SCHEMA_VERSION
    assert verify_schema(postgres_writer_engine).ready is True

    indexes = {
        item["name"]: item
        for item in inspect(postgres_writer_engine).get_indexes(
            "okx_demo_exchange_events"
        )
    }
    assert indexes[index_name]["column_names"] == [
        "execution_target_id",
        "recovery_batch_database_id",
        "observed_at",
        "database_id",
    ]
    assert indexes[index_name]["unique"] is False

    with postgres_writer_engine.connect() as connection:
        connection.execute(text("SET LOCAL enable_seqscan = off"))
        plan = "\n".join(
            row[0]
            for row in connection.execute(
                text(
                    """
                    EXPLAIN
                    SELECT observed_at
                    FROM okx_demo_exchange_events
                    WHERE execution_target_id = 'OKX_DEMO'
                      AND recovery_batch_database_id = -1
                    ORDER BY observed_at DESC, database_id DESC
                    LIMIT 1
                    """
                )
            )
        )
    assert index_name in plan
