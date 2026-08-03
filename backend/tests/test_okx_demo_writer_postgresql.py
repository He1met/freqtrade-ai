from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import importlib.util
import json
import os
from pathlib import Path
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

from app.adapters.okx_demo.errors import OkxReadAdapterError
from app.adapters.okx_demo.write_semantics import OkxDemoWriteBlocked
from app.adapters.okx_demo.models import InstrumentSpec, OkxReadSnapshot, SnapshotMetadata
from app.adapters.okx_demo.order_writer import OkxDemoOrderWriter
from app.adapters.okx_demo.writer_models import (
    OrderSubmissionAuthorization,
    normalize_order_command,
)
from app.adapters.okx_demo.writer_repository import SqlAlchemyOrderWriterStore
from app.adapters.okx_demo.writer_state import WriteEvent
from app.adapters.okx_demo.reconciliation_runtime import (
    OkxDemoRuntimeReconciliationAdapter,
)
from app.db.migrations import (
    CANARY_LINEAGE_WRITE_BASE_VERSION,
    CANARY_FINAL_EXPIRY_BASE_VERSION,
    CANARY_LIFECYCLE_BASE_VERSION,
    CANARY_CONSENT_HANDOFF_BASE_VERSION,
    CANARY_CONSENT_FAILURE_AUDIT_BASE_VERSION,
    BOUNDED_SECOND_ACCEPTANCE_BASE_VERSION,
    FULL_CHAIN_BASE_VERSION,
    ORDER_WRITER_BASE_VERSION,
    RECONCILIATION_BASE_VERSION,
    RECONCILIATION_INDEX_BASE_VERSION,
    RUNTIME_APP_ACL_BASE_VERSION,
    RUNTIME_APPLICATION_TABLES,
    SCHEMA_VERSION,
    OPERATOR_TOKEN_ENV,
    SOAK_BASE_VERSION,
    SchemaMigrationBlocked,
    VERSION_TABLE,
    _add_bounded_second_accepted_not_found_boundary,
    _add_final_accepted_not_found_boundary,
    _add_order_writer,
    _add_canary_consent_handoff_boundary,
    schema_problems,
    harden_operator_consent_access_boundary,
    harden_attestation_access_boundary,
    revoke_operator_consents_for_key_hardening,
    revoke_attested_sessions_for_key_hardening,
    upgrade_database,
    verify_connection_schema,
    verify_schema,
)
from app.models.execution_lineage import (
    ApprovedExecution,
    ExchangeFill,
    ExchangeOrder,
    OkxDemoTrustedSnapshot,
    ReconciliationRun,
    ResearchJobAttempt,
    RiskBudget,
    RiskDecision,
    TradeIntent,
)
from app.models.full_chain import FullChainRun, FullChainStageRun
from app.models.order_writer import (
    OkxDemoCanaryConsentHandoff,
    OkxDemoCanaryLifecycle,
    OkxDemoSubmissionGrant,
    OkxOrderWriteAttempt,
)
from app.models.okx_demo_reconciliation import (
    OkxDemoExchangeEvent,
    OkxDemoFillSnapshot,
    OkxDemoOrderSnapshot,
    OkxDemoPositionSnapshot,
    OkxDemoReconciliationState,
    OkxDemoRecoveryBatch,
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
    OkxDemoCanaryConsentCaptureFailed,
    OkxDemoCanaryPreparationBlocked,
    OkxDemoCanaryPreparationService,
    process_pending_canary_consent_handoff,
)
from app.services.okx_demo_submission_grant import (
    acquire_one_shot_runtime_lock,
    arm_finalized_canary_consent,
    CANARY_PROVENANCE,
    fail_canary_grant_before_prepare,
    OkxDemoSubmissionGrantService,
    OkxDemoSubmissionGrantBlocked,
    release_one_shot_runtime_lock,
    revoke_restarted_canary_grant,
    require_canary_reconciliation,
    submission_grant_request_digest,
    try_one_shot_transaction_lock,
)


# PostgreSQL attestation functions validate against statement_timestamp();
# keep integration evidence fresh instead of coupling the suite to one date.
NOW = datetime.now(timezone.utc).replace(microsecond=0)

BACKUP_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "postgres_backup.py"
BACKUP_SPEC = importlib.util.spec_from_file_location(
    "postgres_backup_pg_integration", BACKUP_SCRIPT_PATH
)
assert BACKUP_SPEC is not None and BACKUP_SPEC.loader is not None
postgres_backup = importlib.util.module_from_spec(BACKUP_SPEC)
BACKUP_SPEC.loader.exec_module(postgres_backup)


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
    snapshot_ttl_seconds: int = 30,
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
            "bbo": {"bid_price": "57000", "ask_price": "57000"},
            "mark": {"price": "57000"},
            "as_of": now.isoformat(),
        },
        "account": {
            "execution_target": "OKX_DEMO",
            "source": "okx_demo_rest",
            "resource": "account",
            "stale": False,
            "authenticated": True,
            "leverage_by_position_side": {"long": "1", "short": "1"},
        },
    }
    for content in contents.values():
        content["expires_at"] = (
            now + timedelta(seconds=snapshot_ttl_seconds)
        ).isoformat()
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
            expires_at=now + timedelta(seconds=snapshot_ttl_seconds),
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


def _capture_runtime_attested_bundle(
    session: Session,
    *,
    capability,
    observed_at: datetime,
    ttl_seconds: int = 30,
):
    contents = {
        "instrument": {
            "execution_target": "OKX_DEMO", "source": "okx_demo_rest",
            "resource": "instrument", "stale": False,
            "instId": "BTC-USDT-SWAP", "minSz": "1", "lotSz": "1",
            "ctVal": "0.0001", "ctValCcy": "BTC", "tickSz": "0.1",
            "state": "live", "contract_shape": "linear",
        },
        "market": {
            "execution_target": "OKX_DEMO", "source": "okx_demo_rest",
            "resource": "market", "stale": False,
            "instrument_id": "BTC-USDT-SWAP", "reference_price": "57000",
            "bbo": {"bid_price": "57000", "ask_price": "57000"},
            "mark": {"price": "57000"}, "as_of": observed_at.isoformat(),
        },
        "account": {
            "execution_target": "OKX_DEMO", "source": "okx_demo_rest",
            "resource": "account", "stale": False, "authenticated": True,
            "leverage_by_position_side": {"long": "1", "short": "1"},
        },
    }
    expires_at = observed_at + timedelta(seconds=ttl_seconds)
    for content in contents.values():
        content["expires_at"] = expires_at.isoformat()
    rows = {}
    for kind, content in contents.items():
        normalized = _normalize_attested_snapshot(
            capability,
            kind=kind,
            content=content,
            observed_at=observed_at,
            expires_at=expires_at,
        )
        rows[kind] = _write_attested_snapshot(
            session, capability, normalized, now=observed_at
        )
    session.flush()
    return SimpleNamespace(**rows)


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


def _rehash_canary_payload(payload: dict) -> dict:
    altered = json.loads(json.dumps(payload))
    canonical_input = altered["request_snapshot"]["canonical_input"]
    altered["canonical_input_serialized"] = _canonical_json(canonical_input)
    altered["canonical_hash"] = hashlib.sha256(
        altered["canonical_input_serialized"].encode()
    ).hexdigest()
    policy = json.loads(altered["policy_serialized"])
    policy["max_leverage"] = canonical_input["leverage"]
    altered["policy_serialized"] = _canonical_json(policy)
    altered["policy_digest"] = hashlib.sha256(
        altered["policy_serialized"].encode()
    ).hexdigest()
    approved_payload = {
        "canonical_input": canonical_input,
        "notional": altered["notional"],
        "provenance": "CONTROLLED_CANARY_NON_PRODUCTION",
    }
    altered["approved_payload_serialized"] = _canonical_json(approved_payload)
    altered["approved_payload_hash"] = hashlib.sha256(
        altered["approved_payload_serialized"].encode()
    ).hexdigest()
    identity = {
        "provenance": "CONTROLLED_CANARY_NON_PRODUCTION",
        "idempotency_key_digest": altered["idempotency_key_digest"],
        "canonical_hash": altered["canonical_hash"],
    }
    altered["intent_identity_serialized"] = _canonical_json(identity)
    altered["intent_id"] = hashlib.sha256(
        altered["intent_identity_serialized"].encode()
    ).hexdigest()
    altered["client_order_id"] = "FAICANARY" + altered["intent_id"][:23]
    return altered


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
    seed_now: datetime = NOW,
) -> tuple[int, int]:
    ensure_execution_scope_catalog(session)
    capability = _issue_attested_session_capability(
        attestation_hmac_key=b"t" * 32,
        pinned_fingerprint_sha256="f" * 64,
        created_at=seed_now - timedelta(seconds=1),
        expires_at=seed_now + timedelta(minutes=10),
    )
    snapshots = {}
    for kind in ("instrument", "market", "account"):
        content = {
            "execution_target": "OKX_DEMO",
            "source": "okx_demo_rest",
            "resource": kind,
            "stale": False,
            "authenticated": kind == "account",
            "expires_at": (seed_now + timedelta(minutes=5)).isoformat(),
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
                as_of=seed_now.isoformat(),
            )
        normalized = _normalize_attested_snapshot(
            capability,
            kind=kind,
            content=content,
            observed_at=seed_now,
            expires_at=seed_now + timedelta(minutes=5),
        )
        snapshots[kind] = _write_attested_snapshot(
            session,
            capability,
            normalized,
            now=seed_now,
        )
    snapshot_evidence = {
        kind: {
            "snapshot_id": row.snapshot_id,
            "database_id": row.database_id,
            "digest": row.digest,
            "expires_at": (seed_now + timedelta(minutes=5)).isoformat(),
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
        expires_at=seed_now + timedelta(minutes=5),
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
        expires_at=seed_now + timedelta(minutes=5),
        evidence_snapshot={},
        created_at=seed_now,
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


def _bind_legal_consent_handoff_fixture(
    session: Session,
    grant: OkxDemoSubmissionGrant,
    *,
    now: datetime,
) -> None:
    """Give legacy lifecycle fixtures the same mandatory v28 grant lineage."""

    source = session.get(ResearchJob, 22)
    if source is None:
        source = ResearchJob(
            id=22,
            execution_scope_id="LOCAL_DRY_RUN",
            job_type="controlled_canary_source",
            operation=CANARY_OPERATION,
            idempotency_key_digest=canonical_digest({"source": grant.grant_id}),
            request_hash=canonical_digest({"source-request": grant.grant_id}),
            request_payload={"entry_kind": "FRESH_EXECUTION_ONLY_FINAL_EXPIRY_RECOVERY"},
            status="SUCCESS",
            stage="CANARY_SNAPSHOTS_READY",
            attempt_count=1,
            max_attempts=1,
            evidence_snapshot={"provenance": CANARY_PROVENANCE},
            started_at=now - timedelta(seconds=2),
            completed_at=now - timedelta(seconds=1),
            created_at=now - timedelta(seconds=2),
        )
        session.add(source)
        session.flush()
    audit = ResearchJob(
        execution_scope_id="LOCAL_DRY_RUN",
        job_type="controlled_canary_consent_audit",
        operation="okx_demo_canary_consent_execution_audit",
        idempotency_key_digest=canonical_digest({"audit": grant.grant_id}),
        request_hash=canonical_digest({"audit-request": grant.grant_id}),
        request_payload={},
        status="SUCCESS",
        stage="CONSENT_FINALIZED",
        attempt_count=1,
        max_attempts=1,
        evidence_snapshot={"provenance": CANARY_PROVENANCE},
        started_at=now - timedelta(seconds=1),
        completed_at=now,
        created_at=now - timedelta(seconds=1),
    )
    session.add(audit)
    session.flush()
    handoff_status = {
        "ACTIVE": "GRANT_ISSUED",
        "CONSUMED": "CONSUMED",
        "FAILED": "FAILED",
        "EXPIRED": "EXPIRED",
    }[grant.status]
    handoff_id = uuid4().hex
    grant.handoff_id = handoff_id
    session.add(OkxDemoCanaryConsentHandoff(
        handoff_id=handoff_id,
        execution_target_id="OKX_DEMO",
        source_job_id=22,
        source_ancestry=list(range(15, 23)),
        source_fingerprint=canonical_digest({"fingerprint": grant.grant_id}),
        idempotency_key_digest=canonical_digest({"handoff": grant.grant_id}),
        consent_nonce=canonical_digest({"nonce": grant.grant_id}),
        consent_payload_digest=canonical_digest({"payload": grant.grant_id}),
        consent_digest=canonical_digest({"consent": grant.grant_id}),
        provenance=CANARY_PROVENANCE,
        instrument_id=grant.instrument_id,
        max_notional=Decimal("20"),
        status=handoff_status,
        runtime_instance_id="FixtureRuntime627",
        reconciliation_run_id=grant.reconciliation_run_id,
        snapshot_binding={},
        audit_job_id=audit.id,
        approval_id=grant.approval_id,
        grant_id=grant.grant_id,
        consented_at=now - timedelta(seconds=2),
        consent_deadline_at=now + timedelta(minutes=1),
        finalized_at=now - timedelta(seconds=1),
        created_at=now - timedelta(seconds=2),
        updated_at=now,
    ))


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


def test_postgresql_v27_to_v28_preserves_exact_jobs_15_through_22(
    postgres_writer_engine,
) -> None:
    upgrade_database(postgres_writer_engine)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with Session(postgres_writer_engine) as session:
        _seed_final_consent_source(session, now=now)
        legacy_approval_id, _ = _seed_approved_order(
            session,
            create_order=False,
            controlled_canary=True,
            seed_now=now,
        )
        _snapshots, legacy_run_id, _order = _seed_canary_lineage_boundary(
            session, now=now
        )
        legacy_approval = session.get(ApprovedExecution, legacy_approval_id)
        legacy_identity = {
            "approval_id": legacy_approval_id,
            "run_id": legacy_run_id,
            "canonical_hash": legacy_approval.canonical_hash,
            "policy_digest": legacy_approval.policy_digest,
            "payload_hash": legacy_approval.approved_payload_hash,
            "client_order_id": legacy_approval.client_order_id,
        }
        before = session.execute(text(
            "SELECT id,request_hash,request_payload::jsonb,status,stage,"
            "evidence_snapshot::jsonb,completed_at FROM research_jobs "
            "WHERE id BETWEEN 15 AND 22 ORDER BY id"
        )).all()
    with postgres_writer_engine.begin() as connection:
        connection.exec_driver_sql(
            "DROP TRIGGER IF EXISTS freeze_okx_demo_canary_source ON research_jobs;"
            "DROP TRIGGER IF EXISTS require_okx_demo_grant_handoff "
            "ON okx_demo_submission_grants;"
            "DROP FUNCTION IF EXISTS request_okx_demo_canary_consent(text,text,text,text) CASCADE;"
            "DROP FUNCTION IF EXISTS pending_okx_demo_canary_consent() CASCADE;"
            "DROP FUNCTION IF EXISTS claim_okx_demo_canary_consent(text,text,bigint,jsonb) CASCADE;"
            "DROP FUNCTION IF EXISTS finalize_okx_demo_canary_consent(text,text,bigint,bigint,bigint,bigint,jsonb) CASCADE;"
            "DROP FUNCTION IF EXISTS finalized_okx_demo_canary_consent(text) CASCADE;"
            "DROP FUNCTION IF EXISTS issue_okx_demo_submission_grant(jsonb) CASCADE;"
            "DROP FUNCTION IF EXISTS revoke_restarted_okx_demo_canary_grant(text,text) CASCADE;"
            "DROP FUNCTION IF EXISTS fail_okx_demo_canary_grant_before_prepare(text) CASCADE;"
            "DROP FUNCTION IF EXISTS settle_okx_demo_canary_handoff(text) CASCADE;"
            "DROP FUNCTION IF EXISTS expire_okx_demo_canary_approval(bigint,text) CASCADE;"
            "DROP FUNCTION IF EXISTS revoke_all_okx_demo_canary_consents_for_hardening() CASCADE;"
            "DROP FUNCTION IF EXISTS canonical_jsonb_text(jsonb) CASCADE;"
            "DROP FUNCTION IF EXISTS canonical_decimal_text(numeric) CASCADE;"
            "DROP FUNCTION IF EXISTS freeze_okx_demo_canary_source() CASCADE;"
            "DROP FUNCTION IF EXISTS require_okx_demo_grant_handoff() CASCADE;"
            "ALTER TABLE okx_demo_submission_grants DROP COLUMN IF EXISTS handoff_id;"
            "DROP TABLE IF EXISTS okx_demo_canary_consent_handoffs CASCADE;"
            "DROP TABLE IF EXISTS okx_demo_operator_consent_secrets CASCADE"
        )
        connection.execute(text("DELETE FROM freqtrade_ai_schema_migrations"))
        connection.execute(text(
            "INSERT INTO freqtrade_ai_schema_migrations(version) VALUES(:version)"
        ), {"version": CANARY_LIFECYCLE_BASE_VERSION})
        connection.execute(text(
            "INSERT INTO okx_demo_submission_grants("
            "grant_id,execution_target_id,approval_id,reconciliation_run_id,"
            "canonical_hash,policy_digest,approved_payload_hash,client_order_id,"
            "instrument_id,canary_quantity,canary_notional,request_digest,"
            "provenance,status,issued_at,expires_at) VALUES("
            "'62700000000000000000000000000000','OKX_DEMO',:approval_id,:run_id,"
            ":canonical_hash,:policy_digest,:payload_hash,:client_order_id,"
            "'BTC-USDT-SWAP',1,5.7,:request_digest,:provenance,'ACTIVE',:issued,:expires)"
        ), {
            **legacy_identity,
            "request_digest": "6" * 64,
            "provenance": CANARY_PROVENANCE,
            "issued": now,
            "expires": now + timedelta(minutes=1),
        })
        connection.execute(text(
            "INSERT INTO research_jobs(id,execution_scope_id,job_type,operation,"
            "idempotency_key_digest,request_hash,request_payload,status,stage,"
            "attempt_count,max_attempts,cancel_requested,evidence_snapshot,created_at) "
            "SELECT 23,execution_scope_id,job_type,operation,:digest,request_hash,"
            "'{}'::jsonb,'PENDING',stage,attempt_count,max_attempts,FALSE,"
            "evidence_snapshot,created_at FROM research_jobs WHERE id=22"
        ), {"digest": "5" * 64})
    with pytest.raises(
        SchemaMigrationBlocked,
        match="refuses existing successor canary source job 23",
    ):
        upgrade_database(postgres_writer_engine)
    with postgres_writer_engine.begin() as connection:
        assert connection.execute(text(
            "SELECT status FROM okx_demo_submission_grants "
            "WHERE grant_id='62700000000000000000000000000000'"
        )).scalar_one() == "ACTIVE"
        connection.execute(text("DELETE FROM research_jobs WHERE id=23"))
    assert upgrade_database(postgres_writer_engine) == SCHEMA_VERSION
    with Session(postgres_writer_engine) as session:
        after = session.execute(text(
            "SELECT id,request_hash,request_payload::jsonb,status,stage,"
            "evidence_snapshot::jsonb,completed_at FROM research_jobs "
            "WHERE id BETWEEN 15 AND 22 ORDER BY id"
        )).all()
        assert after == before
        assert len(after) == 8
        assert session.execute(text(
            "SELECT status FROM okx_demo_submission_grants "
            "WHERE grant_id='62700000000000000000000000000000'"
        )).scalar_one() == "FAILED"
        assert session.get(ApprovedExecution, legacy_approval_id).status == "EXPIRED"
    assert verify_schema(postgres_writer_engine).ready is True


def test_postgresql_upgrade_26_to_27_installs_canary_lifecycle_boundary(
    postgres_writer_engine,
) -> None:
    upgrade_database(postgres_writer_engine)
    with postgres_writer_engine.begin() as connection:
        connection.execute(text(
            "DROP TABLE okx_demo_accepted_not_found_terminalizations CASCADE"
        ))
        connection.execute(text("DROP TRIGGER IF EXISTS okx_demo_recovery_lifecycle_identity_guard ON okx_demo_recovery_grants"))
        connection.execute(text("ALTER TABLE okx_demo_recovery_grants DROP COLUMN lifecycle_id"))
        connection.execute(text("DROP TABLE okx_demo_canary_lifecycles"))
        connection.execute(text("DELETE FROM {}".format(VERSION_TABLE)))
        connection.execute(
            text("INSERT INTO {} (version) VALUES (:version)".format(VERSION_TABLE)),
            {"version": CANARY_FINAL_EXPIRY_BASE_VERSION},
        )
    assert upgrade_database(postgres_writer_engine) == SCHEMA_VERSION
    with postgres_writer_engine.connect() as connection:
        assert connection.execute(text("SELECT to_regclass('okx_demo_canary_lifecycles') IS NOT NULL")).scalar_one()
        assert "lifecycle_id" in {
            row["name"] for row in inspect(connection).get_columns("okx_demo_recovery_grants")
        }
        indexes = {
            row["name"]: row
            for row in inspect(connection).get_indexes("okx_demo_recovery_grants")
        }
        assert indexes[
            "okx_demo_recovery_grants_one_active_lifecycle_action_idx"
        ]["unique"] is True


def test_postgresql_canary_lifecycle_is_function_only_for_runtime(
    postgres_writer_engine,
) -> None:
    upgrade_database(postgres_writer_engine)
    with postgres_writer_engine.connect() as connection:
        for signature in (
            "lock_okx_demo_reconciliation_state()",
            "create_okx_demo_canary_lifecycle(character varying)",
            "create_okx_demo_canary_cleanup_intent(character varying,bigint,bigint)",
            "can_resume_okx_demo_canary_recovery(bigint)",
            "transition_okx_demo_canary_lifecycle(character varying,text,bigint,bigint,character varying,bigint)",
            "issue_okx_demo_canary_recovery_grant(character varying,bigint,text,bigint)",
        ):
            row = connection.execute(text(
                "SELECT owner.rolname,p.prosecdef,p.proconfig,has_function_privilege('freqtrade',p.oid,'EXECUTE') "
                "FROM pg_proc p JOIN pg_roles owner ON owner.oid=p.proowner WHERE p.oid=CAST(:signature AS regprocedure)"
            ), {"signature": signature}).one()
            assert tuple(row) == ("freqtrade_ai_attestor", True, ["search_path=pg_catalog"], True)
        assert connection.execute(text(
            "SELECT has_table_privilege('freqtrade','okx_demo_canary_lifecycles','INSERT,UPDATE,DELETE'), "
            "has_table_privilege('freqtrade','okx_demo_recovery_grants','INSERT')"
        )).one() == (False, True)
    for statement in (
        "INSERT INTO okx_demo_canary_lifecycles DEFAULT VALUES",
        "UPDATE okx_demo_canary_lifecycles SET fencing_version=fencing_version+1",
    ):
        with pytest.raises(SQLAlchemyError):
            with postgres_writer_engine.begin() as connection:
                connection.execute(text("SET LOCAL ROLE freqtrade"))
                connection.execute(text(statement))


def test_postgresql_canary_lifecycle_create_reject_and_unopened_revoke(
    postgres_writer_engine,
) -> None:
    """Exercise the real v27 function path without contacting OKX."""

    upgrade_database(postgres_writer_engine)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with Session(postgres_writer_engine) as session:
        approval_id, _ = _seed_approved_order(
            session,
            create_order=False,
            controlled_canary=True,
        )
        approval = session.get(ApprovedExecution, approval_id)
        batch = OkxDemoRecoveryBatch(
            execution_target_id="OKX_DEMO",
            recovery_batch_id=uuid4().hex,
            authenticated=True,
            pagination_complete=True,
            complete_streams=["ACCOUNT", "FILL", "ORDER", "POSITION"],
            high_watermarks={kind: now.isoformat() for kind in ("ACCOUNT", "FILL", "ORDER", "POSITION")},
            overlap_started_at=now - timedelta(seconds=1),
            observed_at=now,
            completed_at=now,
            event_count=0,
            evidence_digest="8" * 64,
        )
        session.add(batch)
        session.flush()
        run = ReconciliationRun(
            execution_target_id="OKX_DEMO",
            status="RECONCILED",
            summary_snapshot={},
            database_ids={},
            artifact_sha256="9" * 64,
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
        run.database_ids = {
            "reconciliation_run": [run.id],
            "recovery_batches": [batch.database_id],
            "order_snapshots": [],
            "position_snapshots": [],
        }
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
        grant_id = uuid4().hex
        grant = OkxDemoSubmissionGrant(
                grant_id=grant_id,
                execution_target_id="OKX_DEMO",
                approval_id=approval.id,
                reconciliation_run_id=run.id,
                canonical_hash=approval.canonical_hash,
                policy_digest=approval.policy_digest,
                approved_payload_hash=approval.approved_payload_hash,
                client_order_id=approval.client_order_id,
                instrument_id="BTC-USDT-SWAP",
                canary_quantity=Decimal("1"),
                canary_notional=Decimal("5.7"),
                request_digest="a" * 64,
                provenance=CANARY_PROVENANCE,
                status="ACTIVE",
                issued_at=now - timedelta(seconds=1),
                expires_at=now + timedelta(minutes=1),
            )
        _bind_legal_consent_handoff_fixture(session, grant, now=now)
        session.add(grant)
        session.commit()
        run_id = run.id

    with postgres_writer_engine.connect() as connection:
        baseline_digest = connection.execute(
            text(
                "SELECT encode(public.digest(convert_to(concat_ws('|',id::text,"
                "artifact_sha256,authoritative_observed_at::text,completed_at::text),"
                "'UTF8'),'sha256'),'hex') FROM reconciliation_runs WHERE id=:run_id"
            ),
            {"run_id": run_id},
        ).scalar_one()

    future = now + timedelta(seconds=10)
    with postgres_writer_engine.begin() as connection:
        connection.execute(text(
            "UPDATE reconciliation_runs SET authoritative_observed_at=:future,completed_at=:future WHERE id=:run_id"
        ), {"future": future, "run_id": run_id})
    with pytest.raises(SQLAlchemyError, match="unsafe controlled canary lifecycle baseline"):
        with postgres_writer_engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE freqtrade"))
            connection.execute(text(
                "SELECT create_okx_demo_canary_lifecycle(:grant_id)"
            ), {"grant_id": grant_id})
    with postgres_writer_engine.begin() as connection:
        connection.execute(text(
            "UPDATE reconciliation_runs SET authoritative_observed_at=:now,completed_at=:now WHERE id=:run_id"
        ), {"now": now, "run_id": run_id})

    with pytest.raises(SQLAlchemyError, match="controlled canary grant missing"):
        with postgres_writer_engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE freqtrade"))
            connection.execute(
                text("SELECT create_okx_demo_canary_lifecycle(:grant_id)"),
                {"grant_id": "f" * 32},
            )

    with postgres_writer_engine.begin() as connection:
        connection.execute(text("SET LOCAL ROLE freqtrade"))
        assert connection.execute(
            text("SELECT create_okx_demo_canary_lifecycle(:grant_id)"),
            {"grant_id": grant_id},
        ).scalar_one() == grant_id
        assert connection.execute(
            text(
                "SELECT baseline_evidence_digest FROM okx_demo_canary_lifecycles "
                "WHERE lifecycle_id=:lifecycle"
            ),
            {"lifecycle": grant_id},
        ).scalar_one() == baseline_digest

    with pytest.raises(SQLAlchemyError, match="controlled issuer"):
        with postgres_writer_engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE freqtrade"))
            connection.execute(
                text(
                    "INSERT INTO okx_demo_recovery_grants("
                    "execution_target_id,reconciliation_run_id,lifecycle_id,grant_digest,"
                    "action,instrument_id,position_side,max_quantity,status,expires_at) "
                    "VALUES('OKX_DEMO',:run_id,:lifecycle,'b'||repeat('0',63),'CANCEL',"
                    "'BTC-USDT-SWAP','long',0,'ACTIVE',statement_timestamp()+interval '5 seconds')"
                ),
                {"run_id": run_id, "lifecycle": grant_id},
            )

    with postgres_writer_engine.connect() as connection:
        terminal_digest = connection.execute(
            text(
                "SELECT encode(public.digest(convert_to(concat_ws('|',l.lifecycle_id,"
                "r.id::text,r.artifact_sha256,l.outcome,l.attributed_fill_quantity::text),"
                "'UTF8'),'sha256'),'hex') FROM okx_demo_canary_lifecycles l "
                "JOIN reconciliation_runs r ON r.id=:run_id WHERE l.lifecycle_id=:lifecycle"
            ),
            {"run_id": run_id, "lifecycle": grant_id},
        ).scalar_one()
    with postgres_writer_engine.begin() as connection:
        connection.execute(text("SET LOCAL ROLE freqtrade"))
        version = connection.execute(
            text(
                "SELECT transition_okx_demo_canary_lifecycle("
                ":lifecycle,'REVOKE_UNOPENED',NULL,:run_id,:digest,1)"
            ),
            {"lifecycle": grant_id, "run_id": run_id, "digest": terminal_digest},
        ).scalar_one()
        assert version == 2
    with postgres_writer_engine.connect() as connection:
        assert tuple(connection.execute(
            text(
                "SELECT l.outcome,l.cleanup_phase,g.status FROM okx_demo_canary_lifecycles l "
                "JOIN okx_demo_submission_grants g ON g.grant_id=l.submission_grant_id "
                "WHERE l.lifecycle_id=:lifecycle"
            ),
            {"lifecycle": grant_id},
        ).one()) == ("FAILED", "REVOKED", "FAILED")


@pytest.mark.parametrize(
    "trigger_name",
    (
        "okx_demo_canary_recovery_insert_guard",
        "okx_demo_recovery_lifecycle_identity_guard",
    ),
)
def test_postgresql_canary_lifecycle_trigger_tamper_fails_readiness(
    postgres_writer_engine,
    trigger_name,
) -> None:
    upgrade_database(postgres_writer_engine)
    with postgres_writer_engine.begin() as connection:
        connection.execute(text(
            "DROP TRIGGER {} ON okx_demo_recovery_grants".format(trigger_name)
        ))
    assert "controlled canary lifecycle trigger boundary mismatch: {}".format(
        trigger_name
    ) in schema_problems(postgres_writer_engine)


@pytest.mark.parametrize(
    "tamper",
    (
        "REVOKE SELECT (opening_trade_intent_id) ON "
        "okx_demo_canary_lifecycles FROM freqtrade",
        "GRANT SELECT (opening_approval_id) ON "
        "okx_demo_canary_lifecycles TO freqtrade",
    ),
)
def test_postgresql_canary_lifecycle_column_acl_tamper_fails_readiness(
    postgres_writer_engine,
    tamper,
) -> None:
    upgrade_database(postgres_writer_engine)
    with postgres_writer_engine.begin() as connection:
        connection.execute(text(tamper))
    assert any(
        "controlled canary lifecycle column SELECT ACL mismatch" in item
        for item in schema_problems(postgres_writer_engine)
    )


def test_postgresql_canary_recovery_issuer_fences_repeat_and_unresolved_attempt(
    postgres_writer_engine,
    tmp_path,
) -> None:
    upgrade_database(postgres_writer_engine)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    lifecycle_id = uuid4().hex
    with Session(postgres_writer_engine) as session:
        approval_id, seeded_order_id = _seed_approved_order(
            session,
            create_order=True,
            controlled_canary=True,
        )
        approval = session.get(ApprovedExecution, approval_id)
        intent = session.get(TradeIntent, approval.trade_intent_id)
        order = session.get(ExchangeOrder, seeded_order_id)
        store = SqlAlchemyOrderWriterStore(session, now_provider=lambda: now)
        store.acquire_lease(
            writer_instance_id="CanaryIssuerFixture01",
            approval_id=approval.id,
            canonical_hash=approval.canonical_hash,
            policy_digest=approval.policy_digest,
            approved_payload_hash=approval.approved_payload_hash,
            now=now,
            expires_at=now + timedelta(minutes=1),
        )
        session.add(
            OkxOrderWriteAttempt(
                execution_target_id="OKX_DEMO",
                exchange_order_row_id=order.id,
                approval_id=approval.id,
                recovery_grant_database_id=None,
                operation="PLACE",
                operation_id=order.client_order_id,
                client_order_id=order.client_order_id,
                instrument_id=intent.instrument_id,
                state="PREPARED",
                request_digest="4" * 64,
                safe_request_snapshot={},
                safe_response_snapshot={},
                attempt_count=1,
                lease_generation=1,
                close_sequence=0,
                last_attempt_at=now,
                created_at=now - timedelta(seconds=3),
            )
        )
        session.flush()
        prepared = store.unresolved()
        assert prepared is not None
        acknowledged = store.transition(
            prepared,
            event=WriteEvent.ACKNOWLEDGE,
            exchange_order_id="okx-canary-opening-1",
        )
        store.transition(
            acknowledged,
            event=WriteEvent.RECONCILE,
            order_state="live",
            safe_response_snapshot={"order_id": "okx-canary-opening-1", "state": "live"},
        )
        session.flush()
        session.refresh(order)
        recovery_batch_id = uuid4().hex
        reconciliation = OkxDemoReconciliationService(
            session,
            evidence_root=tmp_path / "managed" / "reconciliation",
            allowed_evidence_root=tmp_path / "managed",
        )
        reconciliation.ingest_recovery_batch(
            [{
                "schema_version": RECONCILIATION_EVENT_SCHEMA_VERSION,
                "execution_target": "OKX_DEMO",
                "source": "REST",
                "entity_kind": "ORDER",
                "entity_key": order.exchange_order_id,
                "source_sequence": 1,
                "stream_generation": 1,
                "observed_at": now.isoformat(),
                "received_at": now.isoformat(),
                "payload": {
                    "ordId": order.exchange_order_id,
                    "clOrdId": order.client_order_id,
                    "instId": intent.instrument_id,
                    "state": "live",
                    "sz": "1",
                    "accFillSz": "0",
                    "avgPx": "",
                    "reduceOnly": False,
                },
            }],
            recovery_batch_id=recovery_batch_id,
            high_watermarks={kind: now.isoformat() for kind in ("ACCOUNT", "FILL", "ORDER", "POSITION")},
            overlap_started_at=now - timedelta(seconds=1),
            observed_at=now,
            completed_at=now,
        )
        batch = session.scalars(
            select(OkxDemoRecoveryBatch).where(
                OkxDemoRecoveryBatch.recovery_batch_id == recovery_batch_id
            )
        ).one()
        snapshot = session.scalars(
            select(OkxDemoOrderSnapshot).where(
                OkxDemoOrderSnapshot.exchange_order_id == order.exchange_order_id
            )
        ).one()
        reconciliation.ingest_event({
            "schema_version": RECONCILIATION_EVENT_SCHEMA_VERSION,
            "execution_target": "OKX_DEMO",
            "source": "REST",
            "entity_kind": "ORDER",
            "entity_key": order.exchange_order_id,
            "source_sequence": 2,
            "stream_generation": 1,
            "observed_at": (now + timedelta(milliseconds=100)).isoformat(),
            "received_at": (now + timedelta(milliseconds=100)).isoformat(),
            "payload": {
                "ordId": order.exchange_order_id,
                "clOrdId": order.client_order_id,
                "instId": intent.instrument_id,
                "state": "partially_filled",
                "sz": "1",
                "accFillSz": "1",
                "avgPx": "57000",
                "reduceOnly": False,
            },
        })
        mismatch_snapshot = session.scalars(
            select(OkxDemoOrderSnapshot).where(
                OkxDemoOrderSnapshot.exchange_order_id == order.exchange_order_id
            ).order_by(OkxDemoOrderSnapshot.database_id.desc())
        ).first()
        run = ReconciliationRun(
            execution_target_id="OKX_DEMO",
            status="DRIFTED",
            summary_snapshot={},
            database_ids={},
            artifact_sha256="e" * 64,
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
        run.database_ids = {
            "reconciliation_run": [run.id],
            "recovery_batches": [batch.database_id],
            "order_snapshots": [snapshot.database_id],
            "fill_snapshots": [],
            "position_snapshots": [],
        }
        canary_identity = "canary:" + hashlib.sha256(
            lifecycle_id.encode()
        ).hexdigest()[:16]
        run.summary_snapshot = {
            "execution_target": "OKX_DEMO",
            "source_type": "api_aggregate",
            "status": "DRIFTED",
            "core_data": True,
            "opening_frozen": True,
            "database_ids": run.database_ids,
            "findings": [],
        }
        state = session.scalars(
            select(OkxDemoReconciliationState).where(
                OkxDemoReconciliationState.execution_target_id == "OKX_DEMO"
            )
        ).one()
        state.status = "DRIFTED"
        state.opening_frozen = True
        state.block_reason = "CONTROLLED_CANARY_DEADLINE_CANCEL_REQUIRED"
        state.last_event_observed_at = now
        state.last_reconciliation_run_id = run.id
        grant = OkxDemoSubmissionGrant(
            grant_id=lifecycle_id,
            execution_target_id="OKX_DEMO",
            approval_id=approval.id,
            reconciliation_run_id=run.id,
            canonical_hash=approval.canonical_hash,
            policy_digest=approval.policy_digest,
            approved_payload_hash=approval.approved_payload_hash,
            client_order_id=approval.client_order_id,
            instrument_id=intent.instrument_id,
            canary_quantity=Decimal("1"),
            canary_notional=Decimal("5.7"),
            request_digest="f" * 64,
            provenance=CANARY_PROVENANCE,
            status="CONSUMED",
            issued_at=now - timedelta(seconds=2),
            expires_at=now + timedelta(minutes=1),
            consumed_at=now - timedelta(seconds=1),
        )
        _bind_legal_consent_handoff_fixture(session, grant, now=now)
        session.add(grant)
        session.flush()
        session.add(
            OkxDemoCanaryLifecycle(
                lifecycle_id=lifecycle_id,
                execution_target_id="OKX_DEMO",
                submission_grant_id=lifecycle_id,
                opening_approval_id=approval.id,
                opening_trade_intent_id=intent.id,
                opening_exchange_order_row_id=None,
                baseline_reconciliation_run_id=run.id,
                baseline_position_quantity=Decimal("0"),
                baseline_evidence_digest="1" * 64,
                opening_order_identity_digest=None,
                attributed_fill_quantity=Decimal("0"),
                max_quantity=Decimal("1"),
                outcome="PENDING",
                cleanup_phase="ARMED",
                deadline_at=now + timedelta(seconds=10),
                fencing_version=1,
                created_at=now - timedelta(seconds=4),
                updated_at=now,
            )
        )
        session.commit()
        run_id = run.id
        order_id = order.id
        snapshot_database_id = snapshot.database_id
        original_database_ids = dict(run.database_ids)
        mismatch_database_ids = dict(
            original_database_ids,
            order_snapshots=[mismatch_snapshot.database_id],
        )

    with postgres_writer_engine.connect() as connection:
        opening_digest = connection.execute(
            text(
                "SELECT encode(public.digest(convert_to(concat_ws('|',o.id::text,"
                "o.client_order_id,i.instrument_id,a.request_digest),'UTF8'),'sha256'),'hex') "
                "FROM exchange_orders o JOIN trade_intents i ON i.id=o.trade_intent_id "
                "JOIN okx_order_write_attempts a ON a.exchange_order_row_id=o.id "
                "WHERE o.id=:order_id AND a.operation='PLACE'"
            ),
            {"order_id": order_id},
        ).scalar_one()
    with postgres_writer_engine.begin() as connection:
        connection.execute(text("SET LOCAL ROLE freqtrade"))
        assert connection.execute(
            text(
                "SELECT transition_okx_demo_canary_lifecycle("
                ":lifecycle,'BIND_OPENING',:order_id,NULL,:digest,1)"
            ),
            {"lifecycle": lifecycle_id, "order_id": order_id, "digest": opening_digest},
        ).scalar_one() == 2

    def insert_conflicting_generic_grant() -> str:
        try:
            with postgres_writer_engine.begin() as connection:
                connection.execute(text("SET LOCAL ROLE freqtrade"))
                connection.execute(text(
                    "INSERT INTO okx_demo_recovery_grants("
                    "execution_target_id,reconciliation_run_id,lifecycle_id,"
                    "exchange_order_row_id,grant_digest,action,instrument_id,"
                    "position_side,max_quantity,status,expires_at) VALUES("
                    "'OKX_DEMO',:run_id,NULL,:order_id,:digest,'CANCEL',"
                    "'BTC-USDT-SWAP','long',0,'ACTIVE',:expires_at)"
                ), {
                    "run_id": run_id,
                    "order_id": order_id,
                    "digest": "2" * 64,
                    "expires_at": now + timedelta(minutes=1),
                })
            return "INSERTED"
        except SQLAlchemyError as exc:
            return "BLOCKED:{}".format(exc)

    with postgres_writer_engine.connect() as locker:
        transaction = locker.begin()
        locker.execute(text("SELECT lock_okx_demo_reconciliation_state()"))
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(insert_conflicting_generic_grant)
            time.sleep(0.1)
            assert future.done() is False
            transaction.commit()
            assert future.result().startswith("BLOCKED:")

    with Session(postgres_writer_engine) as session:
        normal_run = ReconciliationRun(
            execution_target_id="OKX_DEMO",
            status="RECONCILED",
            summary_snapshot={},
            database_ids={},
            artifact_sha256="6" * 64,
            artifact_status="READY",
            authoritative_observed_at=now,
            source_type="api_aggregate",
            core_data=True,
            started_at=now,
            completed_at=now,
            created_at=now,
        )
        session.add(normal_run)
        session.flush()
        normal_run.database_ids = {
            **original_database_ids,
            "reconciliation_run": [normal_run.id],
        }
        state = session.scalars(select(OkxDemoReconciliationState).where(
            OkxDemoReconciliationState.execution_target_id == "OKX_DEMO"
        )).one()
        state.status = "RECONCILED"
        state.opening_frozen = False
        state.last_reconciliation_run_id = normal_run.id
        session.commit()
        normal_run_id = normal_run.id
    adapter = object.__new__(OkxDemoRuntimeReconciliationAdapter)
    adapter._now_provider = lambda: now
    with Session(postgres_writer_engine) as session:
        session.execute(text("SET LOCAL ROLE freqtrade"))
        assert adapter._advance_controlled_canary(session) is True
        assert session.execute(text(
            "SELECT fencing_version FROM okx_demo_canary_lifecycles "
            "WHERE lifecycle_id=:lifecycle"
        ), {"lifecycle": lifecycle_id}).scalar_one() == 2
        session.rollback()
    with postgres_writer_engine.begin() as connection:
        connection.execute(text(
            "ALTER TABLE okx_demo_recovery_grants DISABLE TRIGGER "
            "okx_demo_canary_recovery_insert_guard"
        ))
        connection.execute(text(
            "INSERT INTO okx_demo_recovery_grants("
            "execution_target_id,reconciliation_run_id,lifecycle_id,"
            "exchange_order_row_id,grant_digest,action,instrument_id,"
            "position_side,max_quantity,status,expires_at) VALUES("
            "'OKX_DEMO',:run_id,NULL,:order_id,:digest,'CANCEL',"
            "'BTC-USDT-SWAP','long',0,'ACTIVE',:expires_at)"
        ), {
            "run_id": normal_run_id,
            "order_id": order_id,
            "digest": "3" * 64,
            "expires_at": now + timedelta(minutes=1),
        })
        connection.execute(text(
            "ALTER TABLE okx_demo_recovery_grants ENABLE TRIGGER "
            "okx_demo_canary_recovery_insert_guard"
        ))
    with Session(postgres_writer_engine) as session:
        session.execute(text("SET LOCAL ROLE freqtrade"))
        with pytest.raises(
            OkxDemoReconciliationBlocked,
            match="generic recovery authority conflicts",
        ):
            adapter._advance_controlled_canary(session)
        session.rollback()
    with postgres_writer_engine.begin() as connection:
        connection.execute(text(
            "DELETE FROM okx_demo_recovery_grants WHERE grant_digest=:digest"
        ), {"digest": "3" * 64})
    with postgres_writer_engine.begin() as connection:
        connection.execute(text(
            "UPDATE okx_demo_canary_lifecycles SET deadline_at=:deadline "
            "WHERE lifecycle_id=:lifecycle"
        ), {"deadline": now - timedelta(seconds=2), "lifecycle": lifecycle_id})
        connection.execute(text(
            "UPDATE okx_demo_reconciliation_states SET status='DRIFTED',"
            "opening_frozen=true,last_reconciliation_run_id=:run_id "
            "WHERE execution_target_id='OKX_DEMO'"
        ), {"run_id": run_id})

    # Exercise the real least-privilege reconciliation service after the
    # lifecycle exists.  Its controlled finding must be in the finalized
    # artifact, while the generic issuer remains completely suppressed.
    with Session(postgres_writer_engine) as session:
        session.execute(text("SET LOCAL ROLE freqtrade"))
        service_now = datetime.now(timezone.utc).replace(microsecond=0)
        service = OkxDemoReconciliationService(
            session,
            evidence_root=tmp_path / "runtime-role" / "reconciliation",
            allowed_evidence_root=tmp_path / "runtime-role",
        )
        service.ingest_recovery_batch(
            [
                {
                    "schema_version": RECONCILIATION_EVENT_SCHEMA_VERSION,
                    "execution_target": "OKX_DEMO",
                    "source": "REST",
                    "entity_kind": "ORDER",
                    "entity_key": "okx-canary-opening-1",
                    "source_sequence": 10,
                    "stream_generation": 2,
                    "observed_at": service_now.isoformat(),
                    "received_at": service_now.isoformat(),
                    "payload": {
                        "ordId": "okx-canary-opening-1",
                        "clOrdId": "PgWriterOrder001",
                        "instId": "BTC-USDT-SWAP",
                        "state": "live",
                        "sz": "1",
                        "accFillSz": "0",
                        "avgPx": "",
                        "reduceOnly": False,
                    },
                },
                {
                    **_postgres_position_event("0", 10),
                    "source": "REST",
                    "stream_generation": 2,
                    "observed_at": service_now.isoformat(),
                    "received_at": service_now.isoformat(),
                },
                {
                    "schema_version": RECONCILIATION_EVENT_SCHEMA_VERSION,
                    "execution_target": "OKX_DEMO",
                    "source": "REST",
                    "entity_kind": "ACCOUNT",
                    "entity_key": "account",
                    "source_sequence": 10,
                    "stream_generation": 2,
                    "observed_at": service_now.isoformat(),
                    "received_at": service_now.isoformat(),
                    "payload": {
                        "accountFingerprint": "a" * 64,
                        "equity": "10000",
                        "availableBalance": "9000",
                        "marginBalance": "1000",
                    },
                },
            ],
            recovery_batch_id=uuid4().hex,
            high_watermarks={
                kind: service_now.isoformat()
                for kind in ("ACCOUNT", "FILL", "ORDER", "POSITION")
            },
            overlap_started_at=service_now - timedelta(seconds=1),
            observed_at=service_now,
            completed_at=service_now,
        )
        service_result = service.reconcile(now=service_now)
        session.commit()
        assert service_result.status == "DRIFTED"
        assert service_result.database_ids["recovery_grants"] == []
        assert [item["code"] for item in service_result.findings] == [
            "CONTROLLED_CANARY_DEADLINE_CANCEL_REQUIRED"
        ]
        artifact = json.loads(
            open(service_result.artifact_path, encoding="utf-8").read()
        )
        assert artifact["findings"] == list(service_result.findings)
        assert lifecycle_id not in json.dumps(artifact)
        assert adapter.can_resume_controlled_canary(
            session,
            reconciliation_run_id=service_result.reconciliation_run_database_id,
        ) is True
    with postgres_writer_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE okx_demo_reconciliation_states SET status='DRIFTED',"
                "opening_frozen=true,last_reconciliation_run_id=:run_id "
                "WHERE execution_target_id='OKX_DEMO'"
            ),
            {"run_id": run_id},
        )

    with postgres_writer_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE reconciliation_runs SET summary_snapshot=jsonb_set("
                "summary_snapshot::jsonb,'{findings}',CAST(:findings AS jsonb))::json "
                "WHERE id=:run_id"
            ),
            {
                "run_id": run_id,
                "findings": json.dumps([{
                    "code": "CONTROLLED_CANARY_DEADLINE_CANCEL_REQUIRED",
                    "identity": canary_identity,
                }]),
            },
        )

    with postgres_writer_engine.begin() as connection:
        connection.execute(text(
            "UPDATE reconciliation_runs SET database_ids=CAST(:ids AS json),"
            "summary_snapshot=jsonb_set(summary_snapshot::jsonb,'{database_ids}',CAST(:ids AS jsonb))::json "
            "WHERE id=:run_id"
        ), {"run_id": run_id, "ids": json.dumps(mismatch_database_ids)})
    with postgres_writer_engine.connect() as connection:
        mismatch_digest = connection.execute(text(
            "SELECT encode(public.digest(convert_to(concat_ws('|',CAST(:lifecycle AS text),"
            "r.id::text,r.artifact_sha256,'0',os.status,''),'UTF8'),'sha256'),'hex') "
            "FROM reconciliation_runs r JOIN okx_demo_order_snapshots os "
            "ON os.database_id=CAST(r.database_ids::jsonb->'order_snapshots'->>0 AS bigint) "
            "WHERE r.id=:run_id"
        ), {"lifecycle": lifecycle_id, "run_id": run_id}).scalar_one()
    with pytest.raises(SQLAlchemyError, match="fill attribution transition rejected"):
        with postgres_writer_engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE freqtrade"))
            connection.execute(text(
                "SELECT transition_okx_demo_canary_lifecycle("
                ":lifecycle,'RECORD_FILLS',NULL,:run_id,:digest,2)"
            ), {"lifecycle": lifecycle_id, "run_id": run_id, "digest": mismatch_digest})
    with postgres_writer_engine.begin() as connection:
        connection.execute(text(
            "UPDATE reconciliation_runs SET database_ids=CAST(:ids AS json),"
            "summary_snapshot=jsonb_set(summary_snapshot::jsonb,'{database_ids}',CAST(:ids AS jsonb))::json "
            "WHERE id=:run_id"
        ), {"run_id": run_id, "ids": json.dumps(original_database_ids)})

    adapter = object.__new__(OkxDemoRuntimeReconciliationAdapter)
    adapter._now_provider = lambda: now
    with Session(postgres_writer_engine) as session:
        session.execute(text("SET LOCAL ROLE freqtrade"))
        runtime_snapshot = session.scalars(
            select(OkxDemoOrderSnapshot).where(
                OkxDemoOrderSnapshot.database_id == snapshot_database_id
            )
        ).one()
        derived_findings = []
        OkxDemoReconciliationService(session)._controlled_canary_findings(
            {runtime_snapshot.exchange_order_id: runtime_snapshot},
            {},
            {},
            derived_findings,
            now=now,
        )
        assert [item["code"] for item in derived_findings] == [
            "CONTROLLED_CANARY_DEADLINE_CANCEL_REQUIRED"
        ]
        assert adapter._advance_controlled_canary(session) is True
        session.commit()
        lifecycle = session.execute(
            text(
                "SELECT cleanup_phase,fencing_version "
                "FROM okx_demo_canary_lifecycles "
                "WHERE lifecycle_id=:lifecycle_id"
            ),
            {"lifecycle_id": lifecycle_id},
        ).one()
        assert lifecycle.cleanup_phase == "CANCEL_PENDING"
        assert lifecycle.fencing_version == 3
    with pytest.raises(SQLAlchemyError, match="stale or invalid"):
        with postgres_writer_engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE freqtrade"))
            connection.execute(
                text(
                    "SELECT transition_okx_demo_canary_lifecycle("
                    ":lifecycle,'RECORD_FILLS',NULL,:run_id,:digest,2)"
                ),
                {
                    "lifecycle": lifecycle_id,
                    "run_id": run_id,
                    "digest": "0" * 64,
                },
            )

    with postgres_writer_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE reconciliation_runs SET summary_snapshot=jsonb_set("
                "summary_snapshot::jsonb,'{findings}','[]'::jsonb)::json WHERE id=:run_id"
            ),
            {"run_id": run_id},
        )

    with pytest.raises(SQLAlchemyError, match="canary recovery grant context rejected"):
        with postgres_writer_engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE freqtrade"))
            connection.execute(
                text(
                    "SELECT issue_okx_demo_canary_recovery_grant("
                    ":lifecycle,:run_id,'CANCEL',3)"
                ),
                {"lifecycle": lifecycle_id, "run_id": run_id},
            )

    with postgres_writer_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE reconciliation_runs SET summary_snapshot=jsonb_set("
                "summary_snapshot::jsonb,'{findings}',CAST(:findings AS jsonb))::json "
                "WHERE id=:run_id"
            ),
            {
                "run_id": run_id,
                "findings": json.dumps([{"code": "FOREIGN_DRIFT", "identity": "foreign"}]),
            },
        )
    with pytest.raises(SQLAlchemyError, match="canary recovery grant context rejected"):
        with postgres_writer_engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE freqtrade"))
            connection.execute(
                text(
                    "SELECT issue_okx_demo_canary_recovery_grant("
                    ":lifecycle,:run_id,'CANCEL',3)"
                ),
                {"lifecycle": lifecycle_id, "run_id": run_id},
            )

    with postgres_writer_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE reconciliation_runs SET summary_snapshot=jsonb_set("
                "summary_snapshot::jsonb,'{findings}',CAST(:findings AS jsonb))::json "
                "WHERE id=:run_id"
            ),
            {
                "run_id": run_id,
                "findings": json.dumps([{
                    "code": "CONTROLLED_CANARY_DEADLINE_CANCEL_REQUIRED",
                    "identity": canary_identity,
                }]),
            },
        )
    issuer_barrier = Barrier(2)

    def issue_cancel_concurrently() -> tuple[str, object]:
        issuer_barrier.wait()
        try:
            with postgres_writer_engine.begin() as connection:
                connection.execute(text("SET LOCAL ROLE freqtrade"))
                return ("ISSUED", connection.execute(
                    text(
                        "SELECT issue_okx_demo_canary_recovery_grant("
                        ":lifecycle,:run_id,'CANCEL',3)"
                    ),
                    {"lifecycle": lifecycle_id, "run_id": run_id},
                ).scalar_one())
        except SQLAlchemyError as exc:
            return ("BLOCKED", str(exc))

    with ThreadPoolExecutor(max_workers=2) as executor:
        issuer_results = list(executor.map(lambda _: issue_cancel_concurrently(), range(2)))
    assert [item[0] for item in issuer_results].count("ISSUED") == 1
    assert [item[0] for item in issuer_results].count("BLOCKED") == 1
    recovery_id = next(item[1] for item in issuer_results if item[0] == "ISSUED")
    with postgres_writer_engine.connect() as connection:
        status, version, expires_at = connection.execute(
            text(
                "SELECT g.status,l.fencing_version,g.expires_at "
                "FROM okx_demo_recovery_grants g JOIN okx_demo_canary_lifecycles l "
                "ON l.lifecycle_id=g.lifecycle_id WHERE g.database_id=:grant_id"
            ),
            {"grant_id": recovery_id},
        ).one()
        assert (status, version) == ("ACTIVE", 4)
        assert expires_at <= datetime.now(timezone.utc) + timedelta(seconds=10)

    for expected_version in (3, 4):
        with pytest.raises(SQLAlchemyError, match="canary recovery grant context rejected"):
            with postgres_writer_engine.begin() as connection:
                connection.execute(text("SET LOCAL ROLE freqtrade"))
                connection.execute(
                    text(
                        "SELECT issue_okx_demo_canary_recovery_grant("
                        ":lifecycle,:run_id,'CANCEL',:version)"
                    ),
                    {"lifecycle": lifecycle_id, "run_id": run_id, "version": expected_version},
                )

    with postgres_writer_engine.begin() as connection:
        connection.execute(text("DELETE FROM okx_order_writer_leases"))
    with Session(postgres_writer_engine) as session:
        session.execute(text("SET LOCAL ROLE freqtrade"))
        store = SqlAlchemyOrderWriterStore(session, now_provider=lambda: now)
        store.acquire_recovery_lease(recovery_id, now=now)
        managed = store.load_recovery_order(recovery_id)
        prepared_cancel = store.prepare_existing(
            managed,
            operation="CANCEL",
            operation_id=managed.client_order_id,
            request_digest="3" * 64,
            safe_request_snapshot={
                "instId": managed.instrument_id,
                "clOrdId": managed.client_order_id,
            },
            recovery_grant_database_id=recovery_id,
        )
    with pytest.raises(SQLAlchemyError, match="canary recovery grant context rejected"):
        with postgres_writer_engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE freqtrade"))
            connection.execute(
                text(
                    "SELECT issue_okx_demo_canary_recovery_grant("
                    ":lifecycle,:run_id,'CANCEL',4)"
                ),
                {"lifecycle": lifecycle_id, "run_id": run_id},
            )

    with postgres_writer_engine.begin() as connection:
        connection.execute(text("DELETE FROM okx_order_writer_leases"))
    with Session(postgres_writer_engine) as session:
        session.execute(text("SET LOCAL ROLE freqtrade"))
        store = SqlAlchemyOrderWriterStore(session, now_provider=lambda: now)
        store.acquire_recovery_lease(recovery_id, now=now)
        resumed = store.unresolved()
        assert resumed is not None
        acknowledged = store.transition(
            resumed,
            event=WriteEvent.ACKNOWLEDGE,
            exchange_order_id="okx-canary-opening-1",
        )
        store.transition(
            acknowledged,
            event=WriteEvent.RECONCILE,
            order_state="canceled",
            safe_response_snapshot={"order_id": "okx-canary-opening-1", "state": "canceled"},
        )

    final_now = service_now + timedelta(seconds=1)
    with Session(postgres_writer_engine) as session:
        final_batch_id = uuid4().hex
        reconciliation = OkxDemoReconciliationService(
            session,
            evidence_root=tmp_path / "managed-final" / "reconciliation",
            allowed_evidence_root=tmp_path / "managed-final",
        )
        reconciliation.ingest_recovery_batch(
            [{
                "schema_version": RECONCILIATION_EVENT_SCHEMA_VERSION,
                "execution_target": "OKX_DEMO",
                "source": "REST",
                "entity_kind": "ORDER",
                "entity_key": "okx-canary-opening-1",
                "source_sequence": 11,
                "stream_generation": 3,
                "observed_at": final_now.isoformat(),
                "received_at": final_now.isoformat(),
                "payload": {
                    "ordId": "okx-canary-opening-1",
                    "clOrdId": "PgWriterOrder001",
                    "instId": "BTC-USDT-SWAP",
                    "state": "canceled",
                    "sz": "1",
                    "accFillSz": "0",
                    "avgPx": "",
                    "reduceOnly": False,
                },
            }],
            recovery_batch_id=final_batch_id,
            high_watermarks={kind: final_now.isoformat() for kind in ("ACCOUNT", "FILL", "ORDER", "POSITION")},
            overlap_started_at=final_now - timedelta(seconds=1),
            observed_at=final_now,
            completed_at=final_now,
        )
        final_batch = session.scalars(select(OkxDemoRecoveryBatch).where(
            OkxDemoRecoveryBatch.recovery_batch_id == final_batch_id
        )).one()
        final_snapshot = session.scalars(select(OkxDemoOrderSnapshot).where(
            OkxDemoOrderSnapshot.exchange_order_id == "okx-canary-opening-1"
        ).order_by(OkxDemoOrderSnapshot.database_id.desc())).first()
        final_run = ReconciliationRun(
            execution_target_id="OKX_DEMO",
            status="RECONCILED",
            summary_snapshot={},
            database_ids={},
            artifact_sha256="6" * 64,
            artifact_status="READY",
            authoritative_observed_at=final_now,
            source_type="api_aggregate",
            core_data=True,
            started_at=final_now,
            completed_at=final_now,
            created_at=final_now,
        )
        session.add(final_run)
        session.flush()
        final_run.database_ids = {
            "reconciliation_run": [final_run.id],
            "recovery_batches": [final_batch.database_id],
            "order_snapshots": [final_snapshot.database_id],
            "fill_snapshots": [],
            "position_snapshots": [],
        }
        state = session.scalars(select(OkxDemoReconciliationState).where(
            OkxDemoReconciliationState.execution_target_id == "OKX_DEMO"
        )).one()
        state.status = "RECONCILED"
        state.opening_frozen = False
        state.block_reason = None
        state.last_reconciliation_run_id = final_run.id
        state.last_event_observed_at = final_now
        session.commit()
        final_run_id = final_run.id

    with postgres_writer_engine.connect() as connection:
        terminal_digest = connection.execute(text(
            "SELECT encode(public.digest(convert_to(concat_ws('|',l.lifecycle_id,"
            "r.id::text,r.artifact_sha256,l.outcome,l.attributed_fill_quantity::text),"
            "'UTF8'),'sha256'),'hex') FROM okx_demo_canary_lifecycles l "
            "JOIN reconciliation_runs r ON r.id=:run_id WHERE l.lifecycle_id=:lifecycle"
        ), {"run_id": final_run_id, "lifecycle": lifecycle_id}).scalar_one()
    with postgres_writer_engine.begin() as connection:
        connection.execute(text("SET LOCAL ROLE freqtrade"))
        assert connection.execute(text(
            "SELECT transition_okx_demo_canary_lifecycle("
            ":lifecycle,'TERMINALIZE',NULL,:run_id,:digest,4)"
        ), {"lifecycle": lifecycle_id, "run_id": final_run_id, "digest": terminal_digest}).scalar_one() == 5
    with postgres_writer_engine.connect() as connection:
        assert tuple(connection.execute(text(
            "SELECT outcome,cleanup_phase FROM okx_demo_canary_lifecycles WHERE lifecycle_id=:lifecycle"
        ), {"lifecycle": lifecycle_id}).one()) == ("PASSED", "TERMINAL")


@pytest.mark.parametrize(
    "residual_scenario",
    ("terminal_after_child", "stale_child_atomic_rollback", "exhaustion_rejections"),
)
def test_postgresql_canary_residual_cleanup_paths(
    postgres_writer_engine,
    tmp_path,
    residual_scenario,
) -> None:
    """Prove exact partial-fill cleanup lineage reaches FAILED terminal."""

    upgrade_database(postgres_writer_engine)
    now = datetime.now(timezone.utc)
    lifecycle_id = uuid4().hex
    with Session(postgres_writer_engine) as session:
        approval_id, order_id = _seed_approved_order(
            session, create_order=True, controlled_canary=True
        )
        approval = session.get(ApprovedExecution, approval_id)
        opening_intent = session.get(TradeIntent, approval.trade_intent_id)
        opening_order = session.get(ExchangeOrder, order_id)
        opening_order.exchange_order_id = "okx-canary-partial-opening"
        opening_order.status = "canceled"
        approval_hashes = {
            "canonical_hash": approval.canonical_hash,
            "policy_digest": approval.policy_digest,
            "approved_payload_hash": approval.approved_payload_hash,
        }
        opening_intent_id = opening_intent.id
        session.commit()

    opening_client = "PgWriterOrder001"
    opening_event = {
        "schema_version": RECONCILIATION_EVENT_SCHEMA_VERSION,
        "execution_target": "OKX_DEMO",
        "source": "REST",
        "entity_kind": "ORDER",
        "entity_key": "okx-canary-partial-opening",
        "source_sequence": 1,
        "stream_generation": 1,
        "observed_at": now.isoformat(),
        "received_at": now.isoformat(),
        "payload": {
            "ordId": "okx-canary-partial-opening",
            "clOrdId": opening_client,
            "instId": "BTC-USDT-SWAP",
            "state": "canceled",
            "sz": "1",
            "accFillSz": "1",
            "avgPx": "57000",
            "reduceOnly": False,
        },
    }
    fill_event = {
        "schema_version": RECONCILIATION_EVENT_SCHEMA_VERSION,
        "execution_target": "OKX_DEMO",
        "source": "REST",
        "entity_kind": "FILL",
        "entity_key": "partial-fill-1",
        "source_sequence": 1,
        "stream_generation": 1,
        "observed_at": now.isoformat(),
        "received_at": now.isoformat(),
        "payload": {
            "fillId": "partial-fill-1",
            "ordId": "okx-canary-partial-opening",
            "instId": "BTC-USDT-SWAP",
            "fillPx": "57000",
            "fillSz": "1",
            "fee": "0",
        },
    }
    position_event = {
        **_postgres_position_event("1", 1),
        "source": "REST",
        "observed_at": now.isoformat(),
        "received_at": now.isoformat(),
    }
    with Session(postgres_writer_engine) as session:
        batch_id = uuid4().hex
        service = OkxDemoReconciliationService(
            session,
            evidence_root=tmp_path / "partial" / "reconciliation",
            allowed_evidence_root=tmp_path / "partial",
        )
        service.ingest_recovery_batch(
            [opening_event, fill_event, position_event],
            recovery_batch_id=batch_id,
            high_watermarks={kind: now.isoformat() for kind in ("ACCOUNT", "FILL", "ORDER", "POSITION")},
            overlap_started_at=now - timedelta(seconds=1),
            observed_at=now,
            completed_at=now,
        )
        batch = session.scalars(select(OkxDemoRecoveryBatch).where(
            OkxDemoRecoveryBatch.recovery_batch_id == batch_id
        )).one()
        order_snapshot = session.scalars(select(OkxDemoOrderSnapshot).where(
            OkxDemoOrderSnapshot.exchange_order_id == "okx-canary-partial-opening"
        )).one()
        fill_snapshot = session.scalars(select(OkxDemoFillSnapshot).where(
            OkxDemoFillSnapshot.exchange_order_id == "okx-canary-partial-opening"
        )).one()
        position_snapshot = session.scalars(select(OkxDemoPositionSnapshot).where(
            OkxDemoPositionSnapshot.instrument_id == "BTC-USDT-SWAP"
        )).one()
        run = ReconciliationRun(
            execution_target_id="OKX_DEMO", status="DRIFTED", summary_snapshot={},
            database_ids={}, artifact_sha256="7" * 64, artifact_status="READY",
            authoritative_observed_at=now, source_type="api_aggregate", core_data=True,
            started_at=now, completed_at=now, created_at=now,
        )
        session.add(run)
        session.flush()
        database_ids = {
            "reconciliation_run": [run.id],
            "recovery_batches": [batch.database_id],
            "order_snapshots": [order_snapshot.database_id],
            "fill_snapshots": [fill_snapshot.database_id],
            "position_snapshots": [position_snapshot.database_id],
        }
        canary_identity = "canary:" + hashlib.sha256(lifecycle_id.encode()).hexdigest()[:16]
        run.database_ids = database_ids
        run.summary_snapshot = {
            "execution_target": "OKX_DEMO", "source_type": "api_aggregate",
            "status": "DRIFTED", "core_data": True, "opening_frozen": True,
            "database_ids": database_ids,
            "findings": [{"code": "CONTROLLED_CANARY_CLEANUP_REQUIRED", "identity": canary_identity}],
        }
        state = session.scalars(select(OkxDemoReconciliationState).where(
            OkxDemoReconciliationState.execution_target_id == "OKX_DEMO"
        )).one()
        state.status = "DRIFTED"
        state.opening_frozen = True
        state.block_reason = "CONTROLLED_CANARY_CLEANUP_REQUIRED"
        state.last_reconciliation_run_id = run.id
        state.last_event_observed_at = now
        grant = OkxDemoSubmissionGrant(
            grant_id=lifecycle_id, execution_target_id="OKX_DEMO",
            approval_id=approval_id, reconciliation_run_id=run.id,
                canonical_hash=approval_hashes["canonical_hash"],
                policy_digest=approval_hashes["policy_digest"],
                approved_payload_hash=approval_hashes["approved_payload_hash"],
            client_order_id=opening_client, instrument_id="BTC-USDT-SWAP",
            canary_quantity=Decimal("1"), canary_notional=Decimal("5.7"),
            request_digest="8" * 64, provenance=CANARY_PROVENANCE,
            status="CONSUMED", issued_at=now - timedelta(seconds=2),
            expires_at=now + timedelta(minutes=1), consumed_at=now - timedelta(seconds=1),
        )
        _bind_legal_consent_handoff_fixture(session, grant, now=now)
        session.add(grant)
        session.flush()
        session.add(OkxDemoCanaryLifecycle(
            lifecycle_id=lifecycle_id, execution_target_id="OKX_DEMO",
            submission_grant_id=lifecycle_id, opening_approval_id=approval_id,
            opening_trade_intent_id=opening_intent_id,
            opening_exchange_order_row_id=order_id,
            baseline_reconciliation_run_id=run.id,
            baseline_position_quantity=Decimal("0"), baseline_evidence_digest="9" * 64,
            opening_order_identity_digest="a" * 64,
            fill_attribution_digest="b" * 64, attributed_fill_quantity=Decimal("1"),
            max_quantity=Decimal("1"), outcome="FAILED", failure_code="CANARY_FILLED",
            cleanup_phase="CLEANUP_PENDING", deadline_at=now - timedelta(seconds=1),
            fencing_version=1, created_at=now - timedelta(seconds=2), updated_at=now,
        ))
        session.commit()
        run_id = run.id

    with postgres_writer_engine.begin() as connection:
        connection.execute(text("SET LOCAL ROLE freqtrade"))
        reduce_grant_id = connection.execute(text(
            "SELECT issue_okx_demo_canary_recovery_grant("
            ":lifecycle,:run_id,'REDUCE_ONLY',1)"
        ), {"lifecycle": lifecycle_id, "run_id": run_id}).scalar_one()
    with postgres_writer_engine.begin() as connection:
        connection.execute(text("DELETE FROM okx_order_writer_leases"))
    with Session(postgres_writer_engine) as session:
        session.execute(text("SET LOCAL ROLE freqtrade"))
        store = SqlAlchemyOrderWriterStore(session, now_provider=lambda: now)
        store.acquire_recovery_lease(reduce_grant_id, now=now)
        cleanup_order, cleanup_attempt, cleanup_body = store.prepare_recovery_close(
            reduce_grant_id
        )
        cleanup_order_id = cleanup_order.exchange_order_row_id
        assert cleanup_body["reduceOnly"] is True
    with postgres_writer_engine.connect() as connection:
        lifecycle = connection.execute(
            text(
                "SELECT cleanup_trade_intent_id,cleanup_approval_id,"
                "cleanup_exchange_order_row_id,fencing_version "
                "FROM okx_demo_canary_lifecycles "
                "WHERE lifecycle_id=:lifecycle"
            ),
            {"lifecycle": lifecycle_id},
        ).one()
        assert lifecycle.cleanup_trade_intent_id is not None
        assert lifecycle.cleanup_approval_id is not None
        assert lifecycle.cleanup_exchange_order_row_id == cleanup_order_id
        assert lifecycle.fencing_version == 4
        assert connection.execute(
            text(
                "SELECT count(*) FROM okx_demo_recovery_grants "
                "WHERE lifecycle_id=:lifecycle AND action='REDUCE_ONLY'"
            ),
            {"lifecycle": lifecycle_id},
        ).scalar_one() == 1
    with pytest.raises(SQLAlchemyError, match="canary recovery grant context rejected"):
        with postgres_writer_engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE freqtrade"))
            connection.execute(
                text(
                    "SELECT issue_okx_demo_canary_recovery_grant("
                    ":lifecycle,:run_id,'REDUCE_ONLY',4)"
                ),
                {"lifecycle": lifecycle_id, "run_id": run_id},
            )

    with postgres_writer_engine.begin() as connection:
        connection.execute(text("DELETE FROM okx_order_writer_leases"))
    with Session(postgres_writer_engine) as session:
        session.execute(text("SET LOCAL ROLE freqtrade"))
        store = SqlAlchemyOrderWriterStore(session, now_provider=lambda: now)
        store.acquire_recovery_lease(reduce_grant_id, now=now)
        resumed = store.unresolved()
        acknowledged = store.transition(resumed, event=WriteEvent.ACKNOWLEDGE,
                                        exchange_order_id="okx-canary-cleanup-1")
        residual = store.transition(
            acknowledged,
            event=WriteEvent.RESIDUAL_DETECTED,
            order_state="partially_filled",
            safe_response_snapshot={
                "order_id": "okx-canary-cleanup-1",
                "state": "partially_filled",
                "accumulated_fill_size": "0.4",
            },
        )

    # A fresh authenticated batch is the only authority for the residual 0.6.
    residual_now = now + timedelta(seconds=1)
    cleanup_client = "rcv{:020d}".format(reduce_grant_id)
    with Session(postgres_writer_engine) as session:
        session.execute(text("SET LOCAL ROLE freqtrade"))
        service = OkxDemoReconciliationService(
            session,
            evidence_root=tmp_path / "residual" / "reconciliation",
            allowed_evidence_root=tmp_path / "residual",
        )
        service.ingest_recovery_batch(
            [
                {
                    **opening_event,
                    "source_sequence": 10,
                    "stream_generation": 2,
                    "observed_at": residual_now.isoformat(),
                    "received_at": residual_now.isoformat(),
                },
                {
                    **opening_event,
                    "entity_key": "okx-canary-cleanup-1",
                    "source_sequence": 11,
                    "stream_generation": 2,
                    "observed_at": residual_now.isoformat(),
                    "received_at": residual_now.isoformat(),
                    "payload": {
                        "ordId": "okx-canary-cleanup-1",
                        "clOrdId": cleanup_client,
                        "instId": "BTC-USDT-SWAP",
                        "state": "partially_filled",
                        "sz": "1",
                        "accFillSz": "0.4",
                        "avgPx": "57000",
                        "reduceOnly": True,
                    },
                },
                {
                    **fill_event,
                    "source_sequence": 10,
                    "stream_generation": 2,
                    "observed_at": residual_now.isoformat(),
                    "received_at": residual_now.isoformat(),
                },
                {
                    **fill_event,
                    "entity_key": "cleanup-fill-residual",
                    "source_sequence": 11,
                    "stream_generation": 2,
                    "observed_at": residual_now.isoformat(),
                    "received_at": residual_now.isoformat(),
                    "payload": {
                        **fill_event["payload"],
                        "fillId": "cleanup-fill-residual",
                        "ordId": "okx-canary-cleanup-1",
                        "fillSz": "0.4",
                    },
                },
                {
                    **_postgres_position_event("0.6", 10),
                    "source": "REST",
                    "stream_generation": 2,
                    "observed_at": residual_now.isoformat(),
                    "received_at": residual_now.isoformat(),
                },
                {
                    "schema_version": RECONCILIATION_EVENT_SCHEMA_VERSION,
                    "execution_target": "OKX_DEMO",
                    "source": "REST",
                    "entity_kind": "ACCOUNT",
                    "entity_key": "account",
                    "source_sequence": 10,
                    "stream_generation": 2,
                    "observed_at": residual_now.isoformat(),
                    "received_at": residual_now.isoformat(),
                    "payload": {
                        "accountFingerprint": "a" * 64,
                        "equity": "10000",
                        "availableBalance": "9000",
                        "marginBalance": "1000",
                    },
                },
            ],
            recovery_batch_id=uuid4().hex,
            high_watermarks={
                kind: residual_now.isoformat()
                for kind in ("ACCOUNT", "FILL", "ORDER", "POSITION")
            },
            overlap_started_at=residual_now - timedelta(seconds=1),
            observed_at=residual_now,
            completed_at=residual_now,
        )
        residual_result = service.reconcile(now=residual_now)
        assert residual_result.status == "DRIFTED"
        assert {
            item["code"] for item in residual_result.findings
        } <= {
            "CONTROLLED_CANARY_FILL_ATTRIBUTED",
            "CONTROLLED_CANARY_CLEANUP_REQUIRED",
            "POSITION_DRIFT",
        }
        residual_run_id = residual_result.reconciliation_run_database_id
        residual_position_id = residual_result.database_ids["position_snapshots"][0]
        if residual_scenario != "exhaustion_rejections":
            adapter = object.__new__(OkxDemoRuntimeReconciliationAdapter)
            adapter._now_provider = lambda: residual_now
            assert adapter._advance_controlled_canary(session) is True
        session.commit()
        if residual_scenario != "exhaustion_rejections":
            fresh_reduce_grant_id = session.scalars(
                select(OkxDemoRecoveryGrant)
                .where(
                    OkxDemoRecoveryGrant.lifecycle_id == lifecycle_id,
                    OkxDemoRecoveryGrant.action == "REDUCE_ONLY",
                    OkxDemoRecoveryGrant.status == "ACTIVE",
                )
                .order_by(OkxDemoRecoveryGrant.database_id.desc())
            ).one().database_id
    if residual_scenario == "stale_child_atomic_rollback":
        residual_attempt_id = residual.attempt_id
        with postgres_writer_engine.connect() as connection:
            before = tuple(connection.execute(text(
                "SELECT (SELECT count(*) FROM trade_intents),"
                "(SELECT count(*) FROM approved_executions),"
                "(SELECT count(*) FROM exchange_orders),"
                "(SELECT count(*) FROM okx_order_write_attempts)"
            )).one())
        with postgres_writer_engine.begin() as connection:
            connection.execute(text("DELETE FROM okx_order_writer_leases"))

        def prepare_after_state_switch() -> str:
            try:
                with Session(postgres_writer_engine) as session:
                    session.execute(text("SET LOCAL ROLE freqtrade"))
                    store = SqlAlchemyOrderWriterStore(
                        session, now_provider=lambda: residual_now
                    )
                    store.acquire_recovery_lease(
                        fresh_reduce_grant_id, now=residual_now
                    )
                    parent = store.unresolved()
                    assert parent is not None
                    assert parent.attempt_id == residual_attempt_id
                    store.prepare_recovery_close_cleanup(
                        parent, fresh_reduce_grant_id
                    )
                    session.commit()
                return "PREPARED"
            except OkxDemoWriteBlocked as exc:
                return "BLOCKED:{}".format(exc)

        with postgres_writer_engine.connect() as state_writer:
            state_transaction = state_writer.begin()
            state_writer.execute(
                text("SELECT lock_okx_demo_reconciliation_state()")
            )
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(prepare_after_state_switch)
                time.sleep(0.1)
                if future.done():
                    future.result()
                assert future.done() is False
                state_writer.execute(text(
                    "UPDATE okx_demo_reconciliation_states "
                    "SET last_reconciliation_run_id=:stale_run "
                    "WHERE execution_target_id='OKX_DEMO'"
                ), {"stale_run": run_id})
                state_transaction.commit()
                assert future.result().startswith("BLOCKED:")
        with postgres_writer_engine.connect() as connection:
            after = tuple(connection.execute(text(
                "SELECT (SELECT count(*) FROM trade_intents),"
                "(SELECT count(*) FROM approved_executions),"
                "(SELECT count(*) FROM exchange_orders),"
                "(SELECT count(*) FROM okx_order_write_attempts)"
            )).one())
            assert after == before
            assert connection.execute(text(
                "SELECT status FROM okx_demo_recovery_grants WHERE database_id=:grant"
            ), {"grant": fresh_reduce_grant_id}).scalar_one() == "ACTIVE"
            assert connection.execute(text(
                "SELECT state FROM okx_order_write_attempts WHERE id=:attempt"
            ), {"attempt": residual_attempt_id}).scalar_one() == "RESIDUAL_CLOSE_REQUIRED"
        return

    if residual_scenario == "exhaustion_rejections":
        with postgres_writer_engine.begin() as connection:
            connection.execute(text(
                "UPDATE okx_order_write_attempts SET close_sequence=3 WHERE id=:attempt"
            ), {"attempt": residual.attempt_id})

        def exhaustion_digest(connection):
            return connection.execute(text(
                "SELECT encode(public.digest(convert_to(concat_ws('|',l.lifecycle_id,"
                "CAST(:attempt AS text),r.id::text,r.artifact_sha256,(ti.quantity-(SELECT "
                "COALESCE(sum(COALESCE(NULLIF(a.safe_response_snapshot::jsonb->>"
                "'accumulated_fill_size','')::numeric,0)),0) FROM okx_order_write_attempts a "
                "JOIN exchange_orders eo ON eo.id=a.exchange_order_row_id WHERE "
                "eo.trade_intent_id=l.cleanup_trade_intent_id AND a.operation='CLOSE'))::text,"
                "'CLEANUP_LIMIT_REACHED'),'UTF8'),'sha256'),'hex') "
                "FROM okx_demo_canary_lifecycles l JOIN trade_intents ti "
                "ON ti.id=l.cleanup_trade_intent_id JOIN reconciliation_runs r ON r.id=:run "
                "WHERE l.lifecycle_id=:lifecycle"
            ), {"attempt": residual.attempt_id, "run": residual_run_id,
                "lifecycle": lifecycle_id}).scalar_one()

        with postgres_writer_engine.connect() as connection:
            digest = exhaustion_digest(connection)
        with postgres_writer_engine.begin() as connection:
            connection.execute(text(
                "UPDATE okx_demo_reconciliation_states SET last_reconciliation_run_id=:stale "
                "WHERE execution_target_id='OKX_DEMO'"
            ), {"stale": run_id})
        with pytest.raises(SQLAlchemyError, match="canary cleanup exhaustion rejected"):
            with postgres_writer_engine.begin() as connection:
                connection.execute(text("SET LOCAL ROLE freqtrade"))
                connection.execute(text(
                    "SELECT transition_okx_demo_canary_lifecycle(:lifecycle,"
                    "'EXHAUST_RECOVERY',:attempt,:run,:digest,4)"
                ), {"lifecycle": lifecycle_id, "attempt": residual.attempt_id,
                    "run": residual_run_id, "digest": digest})
        with postgres_writer_engine.begin() as connection:
            connection.execute(text(
                "UPDATE okx_demo_reconciliation_states SET last_reconciliation_run_id=:run "
                "WHERE execution_target_id='OKX_DEMO'"
            ), {"run": residual_run_id})
        for unsafe_quantity in (Decimal("0"), Decimal("0.5")):
            with postgres_writer_engine.begin() as connection:
                connection.execute(text(
                    "ALTER TABLE okx_demo_position_snapshots DISABLE TRIGGER "
                    "okx_demo_position_snapshots_immutable"
                ))
                connection.execute(text(
                    "UPDATE okx_demo_position_snapshots SET quantity=:quantity "
                    "WHERE database_id=:snapshot"
                ), {"quantity": unsafe_quantity, "snapshot": residual_position_id})
                connection.execute(text(
                    "ALTER TABLE okx_demo_position_snapshots ENABLE TRIGGER "
                    "okx_demo_position_snapshots_immutable"
                ))
            with pytest.raises(SQLAlchemyError, match="canary cleanup exhaustion rejected"):
                with postgres_writer_engine.begin() as connection:
                    connection.execute(text("SET LOCAL ROLE freqtrade"))
                    connection.execute(text(
                        "SELECT transition_okx_demo_canary_lifecycle(:lifecycle,"
                        "'EXHAUST_RECOVERY',:attempt,:run,:digest,4)"
                    ), {"lifecycle": lifecycle_id, "attempt": residual.attempt_id,
                        "run": residual_run_id, "digest": digest})
            with postgres_writer_engine.connect() as connection:
                assert connection.execute(text(
                    "SELECT cleanup_phase FROM okx_demo_canary_lifecycles "
                    "WHERE lifecycle_id=:lifecycle"
                ), {"lifecycle": lifecycle_id}).scalar_one() == "CLEANUP_PENDING"
        with postgres_writer_engine.begin() as connection:
            connection.execute(text(
                "ALTER TABLE okx_demo_position_snapshots DISABLE TRIGGER "
                "okx_demo_position_snapshots_immutable"
            ))
            connection.execute(text(
                "UPDATE okx_demo_position_snapshots SET quantity=0.6 "
                "WHERE database_id=:snapshot"
            ), {"snapshot": residual_position_id})
            connection.execute(text(
                "ALTER TABLE okx_demo_position_snapshots ENABLE TRIGGER "
                "okx_demo_position_snapshots_immutable"
            ))
        with Session(postgres_writer_engine) as session:
            session.execute(text("SET LOCAL ROLE freqtrade"))
            adapter = object.__new__(OkxDemoRuntimeReconciliationAdapter)
            adapter._now_provider = lambda: residual_now
            assert adapter._advance_controlled_canary(session) is True
            session.commit()
        with postgres_writer_engine.connect() as connection:
            assert tuple(connection.execute(text(
                "SELECT cleanup_phase,failure_code FROM okx_demo_canary_lifecycles "
                "WHERE lifecycle_id=:lifecycle"
            ), {"lifecycle": lifecycle_id}).one()) == (
                "RECOVERY_EXHAUSTED", "CLEANUP_LIMIT_REACHED"
            )
            assert connection.execute(text(
                "SELECT count(*) FROM okx_demo_recovery_grants "
                "WHERE lifecycle_id=:lifecycle AND status='ACTIVE'"
            ), {"lifecycle": lifecycle_id}).scalar_one() == 0
        return
    with postgres_writer_engine.begin() as connection:
        connection.execute(text("DELETE FROM okx_order_writer_leases"))
    with Session(postgres_writer_engine) as session:
        session.execute(text("SET LOCAL ROLE freqtrade"))
        store = SqlAlchemyOrderWriterStore(session, now_provider=lambda: now)
        store.acquire_recovery_lease(fresh_reduce_grant_id, now=now)
        child_order, child_attempt, child_body = (
            store.prepare_recovery_close_cleanup(
                residual,
                fresh_reduce_grant_id,
            )
        )
        assert Decimal(child_body["sz"]) == Decimal("0.6")
        assert child_attempt.parent_attempt_id == residual.attempt_id
        assert child_attempt.close_sequence == 1
        assert (
            child_attempt.recovery_grant_database_id
            == fresh_reduce_grant_id
        )
        child_order_id = child_order.exchange_order_row_id
        acknowledged_child = store.transition(
            child_attempt,
            event=WriteEvent.ACKNOWLEDGE,
            exchange_order_id="okx-canary-cleanup-2",
        )
        store.transition(
            acknowledged_child,
            event=WriteEvent.RECONCILE,
            order_state="filled",
            safe_response_snapshot={
                "order_id": "okx-canary-cleanup-2",
                "state": "filled",
                "accumulated_fill_size": "0.6",
            },
        )
    with postgres_writer_engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT count(*) FROM okx_order_write_attempts "
                "WHERE recovery_grant_database_id IN (:old_grant,:new_grant)"
            ),
            {"old_grant": reduce_grant_id, "new_grant": fresh_reduce_grant_id},
        ).scalar_one() == 2

    final_now = now + timedelta(seconds=2)
    final_opening = {**opening_event, "source_sequence": 2,
                     "stream_generation": 3,
                     "observed_at": final_now.isoformat(), "received_at": final_now.isoformat()}
    cleanup_event = {
        **opening_event,
        "entity_key": "okx-canary-cleanup-1",
        "source_sequence": 3,
        "stream_generation": 3,
        "observed_at": final_now.isoformat(), "received_at": final_now.isoformat(),
        "payload": {
            "ordId": "okx-canary-cleanup-1", "clOrdId": cleanup_client,
            "instId": "BTC-USDT-SWAP", "state": "canceled", "sz": "1",
            "accFillSz": "0.4", "avgPx": "57000", "reduceOnly": True,
        },
    }
    child_cleanup_client = "rcv{:020d}C1".format(fresh_reduce_grant_id)
    child_cleanup_event = {
        **cleanup_event,
        "entity_key": "okx-canary-cleanup-2",
        "source_sequence": 4,
        "stream_generation": 3,
        "payload": {
            **cleanup_event["payload"],
            "ordId": "okx-canary-cleanup-2",
            "clOrdId": child_cleanup_client,
            "state": "filled",
            "sz": "0.6",
            "accFillSz": "0.6",
        },
    }
    final_opening_fill = {
        **fill_event,
        "source_sequence": 2,
        "stream_generation": 3,
        "observed_at": final_now.isoformat(),
        "received_at": final_now.isoformat(),
    }
    cleanup_fill = {
        **fill_event,
        "entity_key": "cleanup-fill-1",
        "source_sequence": 3,
        "stream_generation": 3,
        "observed_at": final_now.isoformat(),
        "received_at": final_now.isoformat(),
        "payload": {
            **fill_event["payload"],
            "fillId": "cleanup-fill-1",
            "ordId": "okx-canary-cleanup-1",
            "fillSz": "0.4",
        },
    }
    child_cleanup_fill = {
        **cleanup_fill,
        "entity_key": "cleanup-fill-2",
        "source_sequence": 4,
        "payload": {
            **cleanup_fill["payload"],
            "fillId": "cleanup-fill-2",
            "ordId": "okx-canary-cleanup-2",
            "fillSz": "0.6",
        },
    }
    zero_position = {**_postgres_position_event("0", 2), "source": "REST",
                     "stream_generation": 3,
                     "observed_at": final_now.isoformat(), "received_at": final_now.isoformat()}
    with Session(postgres_writer_engine) as session:
        final_batch_id = uuid4().hex
        service = OkxDemoReconciliationService(
            session, evidence_root=tmp_path / "partial-final" / "reconciliation",
            allowed_evidence_root=tmp_path / "partial-final",
        )
        service.ingest_recovery_batch(
            [
                final_opening,
                cleanup_event,
                child_cleanup_event,
                final_opening_fill,
                cleanup_fill,
                child_cleanup_fill,
                zero_position,
            ], recovery_batch_id=final_batch_id,
            high_watermarks={kind: final_now.isoformat() for kind in ("ACCOUNT", "FILL", "ORDER", "POSITION")},
            overlap_started_at=final_now - timedelta(seconds=1), observed_at=final_now,
            completed_at=final_now,
        )
        final_batch = session.scalars(select(OkxDemoRecoveryBatch).where(
            OkxDemoRecoveryBatch.recovery_batch_id == final_batch_id
        )).one()
        final_orders = session.scalars(select(OkxDemoOrderSnapshot).where(
            OkxDemoOrderSnapshot.observed_at == final_now
        )).all()
        final_position = session.scalars(select(OkxDemoPositionSnapshot).where(
            OkxDemoPositionSnapshot.observed_at == final_now
        )).one()
        final_fills = session.scalars(select(OkxDemoFillSnapshot).where(
            OkxDemoFillSnapshot.observed_at == final_now
        )).all()
        final_fill_ids = [row.database_id for row in final_fills]
        final_run = ReconciliationRun(
            execution_target_id="OKX_DEMO", status="RECONCILED", summary_snapshot={},
            database_ids={}, artifact_sha256="c" * 64, artifact_status="READY",
            authoritative_observed_at=final_now, source_type="api_aggregate", core_data=True,
            started_at=final_now, completed_at=final_now, created_at=final_now,
        )
        session.add(final_run)
        session.flush()
        final_run.database_ids = {
            "reconciliation_run": [final_run.id], "recovery_batches": [final_batch.database_id],
            "order_snapshots": [row.database_id for row in final_orders],
            "fill_snapshots": [],
            "position_snapshots": [final_position.database_id],
        }
        state = session.scalars(select(OkxDemoReconciliationState).where(
            OkxDemoReconciliationState.execution_target_id == "OKX_DEMO"
        )).one()
        state.status = "RECONCILED"
        state.opening_frozen = False
        state.block_reason = None
        state.last_reconciliation_run_id = final_run.id
        state.last_event_observed_at = final_now
        session.commit()
        final_run_id = final_run.id
    with postgres_writer_engine.connect() as connection:
        terminal_digest = connection.execute(text(
            "SELECT encode(public.digest(convert_to(concat_ws('|',l.lifecycle_id,"
            "r.id::text,r.artifact_sha256,l.outcome,l.attributed_fill_quantity::text),"
            "'UTF8'),'sha256'),'hex') FROM okx_demo_canary_lifecycles l "
            "JOIN reconciliation_runs r ON r.id=:run_id WHERE l.lifecycle_id=:lifecycle"
        ), {"run_id": final_run_id, "lifecycle": lifecycle_id}).scalar_one()
        terminal_version = connection.execute(text(
            "SELECT fencing_version FROM okx_demo_canary_lifecycles WHERE lifecycle_id=:lifecycle"
        ), {"lifecycle": lifecycle_id}).scalar_one()
    with pytest.raises(SQLAlchemyError, match="terminal lifecycle evidence rejected"):
        with postgres_writer_engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE freqtrade"))
            connection.execute(text(
                "SELECT transition_okx_demo_canary_lifecycle("
                ":lifecycle,'TERMINALIZE',NULL,:run_id,:digest,:version)"
            ), {"lifecycle": lifecycle_id, "run_id": final_run_id,
                "digest": terminal_digest, "version": terminal_version})
    with postgres_writer_engine.begin() as connection:
        database_ids = connection.execute(
            text("SELECT database_ids FROM reconciliation_runs WHERE id=:run_id"),
            {"run_id": final_run_id},
        ).scalar_one()
        database_ids["fill_snapshots"] = final_fill_ids
        connection.execute(
            text(
                "UPDATE reconciliation_runs SET database_ids=CAST(:ids AS json) "
                "WHERE id=:run_id"
            ),
            {"run_id": final_run_id, "ids": json.dumps(database_ids)},
        )
    with postgres_writer_engine.begin() as connection:
        connection.execute(text("SET LOCAL ROLE freqtrade"))
        connection.execute(text(
            "SELECT transition_okx_demo_canary_lifecycle("
            ":lifecycle,'TERMINALIZE',NULL,:run_id,:digest,:version)"
        ), {"lifecycle": lifecycle_id, "run_id": final_run_id,
            "digest": terminal_digest, "version": terminal_version})
    with postgres_writer_engine.connect() as connection:
        assert tuple(connection.execute(text(
            "SELECT outcome,cleanup_phase FROM okx_demo_canary_lifecycles WHERE lifecycle_id=:lifecycle"
        ), {"lifecycle": lifecycle_id}).one()) == ("FAILED", "TERMINAL")


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
        ("market", "reference_price", "56000"),
        ("instrument", "state", "suspend"),
        ("instrument", "source", "tampered"),
        ("market", "stale", True),
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
                if content_mutation
                in {
                    ("account", "authenticated", None),
                    ("instrument", "source", "tampered"),
                    ("market", "stale", True),
                }
                else content_mutation
            ),
        )
        snapshot_ids = {kind: row.database_id for kind, row in snapshots.items()}
    if content_mutation in {
        ("account", "authenticated", None),
        ("instrument", "source", "tampered"),
        ("market", "stale", True),
    }:
        kind, field, value = content_mutation
        with postgres_writer_engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE okx_demo_trusted_snapshots DISABLE TRIGGER USER")
            )
            connection.execute(
                text(
                    "UPDATE okx_demo_trusted_snapshots "
                    "SET content_json = CASE "
                    "WHEN :remove THEN (content_json::jsonb - :field)::json "
                    "ELSE jsonb_set(content_json::jsonb, ARRAY[:field], "
                    "CAST(:value AS jsonb))::json END WHERE kind = :kind"
                ),
                {
                    "remove": value is None,
                    "field": field,
                    "value": json.dumps(value),
                    "kind": kind,
                },
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
    ("field", "value"),
    (
        ("quantity", Decimal("2")),
        ("leverage", Decimal("2")),
        ("limit_price", Decimal("56999.9")),
        ("stop_loss", Decimal("54000")),
        ("take_profit", Decimal("60000")),
    ),
)
def test_postgresql_canary_lineage_function_rederives_order_from_snapshots(
    postgres_writer_engine,
    field,
    value,
) -> None:
    """A fully re-hashed caller-selected order must still fail in PostgreSQL."""

    upgrade_database(postgres_writer_engine)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with Session(postgres_writer_engine) as admin_session:
        snapshots, reconciliation_run_id, order = _seed_canary_lineage_boundary(
            admin_session,
            now=now,
        )
        snapshot_ids = {kind: row.database_id for kind, row in snapshots.items()}
    crafted_order = dict(order)
    crafted_order[field] = value
    if field == "quantity":
        crafted_order["notional"] = value * Decimal("0.0001") * Decimal("57000")

    with pytest.raises(SQLAlchemyError, match="order derivation mismatch"):
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
                key_digest=hashlib.sha256(
                    "crafted-order:{}:{}".format(field, value).encode()
                ).hexdigest(),
                now=now,
                reconciliation_run_id=reconciliation_run_id,
                snapshots=runtime_snapshots,
                order=crafted_order,
            )

    with postgres_writer_engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM trade_intents")).scalar_one() == 0
        assert connection.execute(text("SELECT count(*) FROM risk_decisions")).scalar_one() == 0
        assert connection.execute(text("SELECT count(*) FROM approved_executions")).scalar_one() == 0


def test_postgresql_canary_lineage_rejects_fabricated_provenance_and_evidence(
    postgres_writer_engine,
) -> None:
    upgrade_database(postgres_writer_engine)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with Session(postgres_writer_engine) as admin_session:
        snapshots, reconciliation_run_id, order = _seed_canary_lineage_boundary(
            admin_session,
            now=now,
        )
        snapshot_ids = {kind: row.database_id for kind, row in snapshots.items()}
    key_digest = hashlib.sha256(b"crafted-canary-evidence").hexdigest()
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
            key_digest=key_digest,
            now=now,
            reconciliation_run_id=reconciliation_run_id,
            snapshots=runtime_snapshots,
            order=order,
        )
        runtime_session.commit()
    with Session(postgres_writer_engine) as admin_session:
        payload = _canary_function_payload(
            admin_session,
            key_digest=key_digest,
            reconciliation_run_id=reconciliation_run_id,
        )
    # Return only this isolated temporary database to the pre-write boundary so
    # direct function calls exercise the attested binding rather than replay.
    with postgres_writer_engine.begin() as connection:
        for table_name in (
            "full_chain_runs",
            "approved_executions",
            "risk_decisions",
            "trade_intents",
        ):
            connection.execute(
                text("ALTER TABLE {} DISABLE TRIGGER USER".format(table_name))
            )
        connection.execute(
            text(
                "UPDATE full_chain_runs SET trade_intent_id = NULL, "
                "risk_decision_id = NULL, approved_execution_id = NULL"
            )
        )
        connection.execute(text("DELETE FROM approved_executions"))
        connection.execute(text("DELETE FROM risk_decisions"))
        connection.execute(text("DELETE FROM trade_intents"))
        for table_name in (
            "trade_intents",
            "risk_decisions",
            "approved_executions",
            "full_chain_runs",
        ):
            connection.execute(
                text("ALTER TABLE {} ENABLE TRIGGER USER".format(table_name))
            )

    canonical_mutations = []
    extra_lineage = json.loads(json.dumps(payload))
    extra_lineage["request_snapshot"]["canonical_input"]["lineage"][
        "strategy_id"
    ] = 1
    canonical_mutations.append(extra_lineage)
    missing_lineage = json.loads(json.dumps(payload))
    missing_lineage["request_snapshot"]["canonical_input"]["lineage"].pop(
        "backtest_task_id"
    )
    canonical_mutations.append(missing_lineage)
    candidate_approval = json.loads(json.dumps(payload))
    candidate_approval["request_snapshot"]["canonical_input"][
        "candidate_approval_id"
    ] = 1
    canonical_mutations.append(candidate_approval)
    signal_snapshot = json.loads(json.dumps(payload))
    signal_snapshot["request_snapshot"]["canonical_input"][
        "signal_snapshot_id"
    ] = 1
    canonical_mutations.append(signal_snapshot)
    bad_signal_digest = json.loads(json.dumps(payload))
    bad_signal_digest["request_snapshot"]["canonical_input"][
        "signal_digest"
    ] = None
    canonical_mutations.append(bad_signal_digest)
    wrong_signal_digest = json.loads(json.dumps(payload))
    wrong_signal_digest["request_snapshot"]["canonical_input"][
        "signal_digest"
    ] = "f" * 64
    canonical_mutations.append(wrong_signal_digest)
    extra_canonical_key = json.loads(json.dumps(payload))
    extra_canonical_key["request_snapshot"]["canonical_input"][
        "fabricated"
    ] = True
    canonical_mutations.append(extra_canonical_key)
    missing_canonical_key = json.loads(json.dumps(payload))
    missing_canonical_key["request_snapshot"]["canonical_input"].pop(
        "take_profit"
    )
    canonical_mutations.append(missing_canonical_key)
    extra_request_key = json.loads(json.dumps(payload))
    extra_request_key["request_snapshot"]["fabricated"] = True
    canonical_mutations.append(extra_request_key)
    missing_request_key = json.loads(json.dumps(payload))
    missing_request_key["request_snapshot"].pop("non_production")
    canonical_mutations.append(missing_request_key)
    extra_snapshot_id = json.loads(json.dumps(payload))
    extra_snapshot_id["request_snapshot"]["canonical_input"]["snapshot_ids"][
        "fabricated"
    ] = "snapshot:fake"
    canonical_mutations.append(extra_snapshot_id)
    for mutation in canonical_mutations:
        altered = _rehash_canary_payload(mutation)
        with pytest.raises(SQLAlchemyError, match="lineage safety contract"):
            with postgres_writer_engine.begin() as connection:
                connection.execute(text("SET LOCAL ROLE freqtrade"))
                connection.execute(
                    text(
                        "SELECT create_okx_demo_canary_lineage("
                        "CAST(:payload AS jsonb))"
                    ),
                    {"payload": json.dumps(altered, sort_keys=True)},
                )

    evidence_mutations = []
    wrong_database_id = json.loads(json.dumps(payload))
    wrong_database_id["request_snapshot"]["snapshot_evidence"]["market"][
        "database_id"
    ] += 1
    evidence_mutations.append(wrong_database_id)
    wrong_expiry = json.loads(json.dumps(payload))
    wrong_expiry["request_snapshot"]["snapshot_evidence"]["account"][
        "expires_at"
    ] = (now + timedelta(seconds=29)).isoformat()
    evidence_mutations.append(wrong_expiry)
    extra_evidence = json.loads(json.dumps(payload))
    extra_evidence["request_snapshot"]["snapshot_evidence"]["instrument"][
        "fabricated"
    ] = True
    evidence_mutations.append(extra_evidence)
    for altered in evidence_mutations:
        with pytest.raises(SQLAlchemyError, match="attested snapshot binding"):
            with postgres_writer_engine.begin() as connection:
                connection.execute(text("SET LOCAL ROLE freqtrade"))
                connection.execute(
                    text(
                        "SELECT create_okx_demo_canary_lineage("
                        "CAST(:payload AS jsonb))"
                    ),
                    {"payload": json.dumps(altered, sort_keys=True)},
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
        ("authoritative_observed_at", NOW + timedelta(seconds=30)),
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

    exact_evidence_tampers = (
        (
            "risk_decisions",
            "evidence_snapshot::jsonb || jsonb_build_object('fabricated', TRUE)",
            "evidence_snapshot::jsonb - 'fabricated'",
        ),
        (
            "risk_decisions",
            "jsonb_set(evidence_snapshot::jsonb, '{reasons}', "
            "'[\"fabricated\"]'::jsonb)",
            "jsonb_set(evidence_snapshot::jsonb, '{reasons}', '[]'::jsonb)",
        ),
        (
            "risk_decisions",
            "jsonb_set(evidence_snapshot::jsonb, '{llm_authority}', 'true'::jsonb)",
            "jsonb_set(evidence_snapshot::jsonb, '{llm_authority}', 'false'::jsonb)",
        ),
        (
            "approved_executions",
            "evidence_snapshot::jsonb || jsonb_build_object('fabricated', TRUE)",
            "evidence_snapshot::jsonb - 'fabricated'",
        ),
    )
    for table_name, mutation, restoration in exact_evidence_tampers:
        with postgres_writer_engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE {} DISABLE TRIGGER USER".format(table_name)
                )
            )
            connection.execute(
                text(
                    "UPDATE {} SET evidence_snapshot = ({})::json".format(
                        table_name, mutation
                    )
                )
            )
            connection.execute(
                text("ALTER TABLE {} ENABLE TRIGGER USER".format(table_name))
            )
        with pytest.raises(SQLAlchemyError, match="idempotency conflict"):
            with postgres_writer_engine.begin() as connection:
                connection.execute(text("SET LOCAL ROLE freqtrade"))
                connection.execute(
                    text(
                        "SELECT create_okx_demo_canary_lineage("
                        "CAST(:payload AS jsonb))"
                    ),
                    {"payload": json.dumps(payload, sort_keys=True)},
                )
        with postgres_writer_engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE {} DISABLE TRIGGER USER".format(table_name)
                )
            )
            connection.execute(
                text(
                    "UPDATE {} SET evidence_snapshot = ({})::json".format(
                        table_name, restoration
                    )
                )
            )
            connection.execute(
                text("ALTER TABLE {} ENABLE TRIGGER USER".format(table_name))
            )

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
        "null_target": ("execution_target", None),
        "provenance": ("provenance", "DEEPSEEK"),
        "null_provenance": ("provenance", None),
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

    nested_null_paths = (
        ("canonical_input", "execution_target"),
        ("canonical_input", "quantity"),
        ("snapshot_evidence", "instrument", "snapshot_id"),
        ("snapshot_evidence", "market", "digest"),
    )
    for path in nested_null_paths:
        altered_null = json.loads(json.dumps(payload))
        target = altered_null["request_snapshot"]
        for component in path[:-1]:
            target = target[component]
        target[path[-1]] = None
        with pytest.raises(SQLAlchemyError):
            with postgres_writer_engine.begin() as connection:
                connection.execute(text("SET LOCAL ROLE freqtrade"))
                connection.execute(
                    text(
                        "SELECT create_okx_demo_canary_lineage("
                        "CAST(:payload AS jsonb))"
                    ),
                    {"payload": json.dumps(altered_null, sort_keys=True)},
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
    race_now = datetime.now(timezone.utc)
    with Session(postgres_writer_engine) as session:
        approval_id, _order_id = _seed_approved_order(
            session,
            create_order=False,
            controlled_canary=True,
            seed_now=race_now,
        )
        approval = session.get(ApprovedExecution, approval_id)
        batch = OkxDemoRecoveryBatch(
            execution_target_id="OKX_DEMO",
            recovery_batch_id="one-shot-race-baseline",
            authenticated=True,
            pagination_complete=True,
            complete_streams=["ACCOUNT", "FILL", "ORDER", "POSITION"],
            high_watermarks={
                "ACCOUNT": "a" * 64,
                "FILL": "b" * 64,
                "ORDER": "c" * 64,
                "POSITION": "d" * 64,
            },
            overlap_started_at=race_now - timedelta(seconds=1),
            observed_at=race_now,
            completed_at=race_now,
            event_count=0,
            evidence_digest="e" * 64,
        )
        session.add(batch)
        session.flush()
        run = ReconciliationRun(
            execution_target_id="OKX_DEMO",
            status="RECONCILED",
            summary_snapshot={},
            database_ids={
                "order_snapshots": [],
                "position_snapshots": [],
                "recovery_batches": [batch.database_id],
            },
            artifact_sha256="f" * 64,
            artifact_status="READY",
            authoritative_observed_at=race_now,
            source_type="api_aggregate",
            core_data=True,
            started_at=race_now,
            completed_at=race_now,
            created_at=race_now,
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
        state.last_event_observed_at = race_now
        state.last_reconciliation_run_id = run.id
        grant_id = uuid4().hex
        grant = OkxDemoSubmissionGrant(
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
                issued_at=race_now - timedelta(seconds=1),
                expires_at=race_now + timedelta(seconds=10),
            )
        _bind_legal_consent_handoff_fixture(session, grant, now=race_now)
        session.add(grant)
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
            store = SqlAlchemyOrderWriterStore(
                session, now_provider=lambda: race_now
            )
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
                issued_at=race_now - timedelta(seconds=1),
                expires_at=race_now + timedelta(seconds=10),
            )
            command = normalize_order_command(
                claimed,
                submission_grant=authorization,
                instrument=instrument,
                now=race_now,
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
                    now=race_now,
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
        lifecycle = connection.execute(
            text(
                "SELECT submission_grant_id,cleanup_phase,fencing_version,"
                "opening_exchange_order_row_id FROM okx_demo_canary_lifecycles"
            )
        ).one()
        assert lifecycle.submission_grant_id == grant_id
        assert lifecycle.cleanup_phase == "OPENING_SUBMITTED"
        assert lifecycle.fencing_version == 2
        assert lifecycle.opening_exchange_order_row_id is not None
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

        # A direct legacy arm is now fail-closed before any grant-table DML;
        # the finalized handoff path exercises the same SELECT-only lineage
        # traversal in the v28 integration matrix below.
        with pytest.raises(
            OkxDemoSubmissionGrantBlocked,
            match="require finalized operator consent",
        ):
            OkxDemoSubmissionGrantService(
                runtime_session,
                now_provider=lambda: NOW,
            ).arm(
                approval_id=approval_id,
                canonical_hash=approval.canonical_hash,
                policy_digest=approval.policy_digest,
                approved_payload_hash=approval.approved_payload_hash,
                client_order_id=approval.client_order_id,
            )


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


def _seed_final_consent_source(session: Session, *, now: datetime) -> None:
    ensure_execution_scope_catalog(session)
    expired = (now - timedelta(seconds=1)).isoformat()
    for job_id in range(15, 23):
        is_source = job_id == 22
        payload = {
            "provenance": CANARY_PROVENANCE,
            "execution_target": "OKX_DEMO",
            "instrument_id": "BTC-USDT-SWAP",
            "bundle_kind": "EXECUTION_ONLY",
            "non_production": True,
        }
        if is_source:
            payload.update(
                {
                    "entry_kind": "FRESH_EXECUTION_ONLY_FINAL_EXPIRY_RECOVERY",
                    "recovery_of_job_id": 21,
                    "supersedes_job_ids": list(range(15, 22)),
                    "recovery_boundary": "POST_PERSISTENCE_RECOVERY_SNAPSHOT_EXPIRY",
                }
            )
        session.add(
            ResearchJob(
                id=job_id,
                execution_scope_id="LOCAL_DRY_RUN",
                job_type="okx_demo_controlled_canary",
                operation=CANARY_OPERATION,
                idempotency_key_digest=hashlib.sha256(
                    "source-{}".format(job_id).encode()
                ).hexdigest(),
                request_hash=canonical_digest(payload),
                request_payload=payload,
                status="SUCCESS" if is_source else "BLOCKED",
                stage="CANARY_SNAPSHOTS_READY" if is_source else "CANARY_SNAPSHOT_BLOCKED",
                attempt_count=1,
                max_attempts=1,
                evidence_snapshot=(
                    {
                        "provenance": CANARY_PROVENANCE,
                        "snapshot_evidence": {
                            kind: {
                                "snapshot_id": "expired-{}".format(kind),
                                "digest": hashlib.sha256(kind.encode()).hexdigest(),
                                "expires_at": expired,
                            }
                            for kind in ("instrument", "market", "account")
                        },
                    }
                    if is_source
                    else {"provenance": CANARY_PROVENANCE}
                ),
                started_at=now - timedelta(minutes=1),
                completed_at=now - timedelta(seconds=2),
            )
        )
    session.commit()


def _finalize_and_arm_v28_handoff(engine, monkeypatch, *, key: str):
    upgrade_database(engine)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with Session(engine) as admin:
        _seed_final_consent_source(admin, now=now)
        _snapshots, run_id, _order = _seed_canary_lineage_boundary(admin, now=now)
        batch = OkxDemoRecoveryBatch(
            execution_target_id="OKX_DEMO",
            recovery_batch_id=uuid4().hex,
            authenticated=True,
            pagination_complete=True,
            complete_streams=["ACCOUNT", "FILL", "ORDER", "POSITION"],
            high_watermarks={
                kind: now.isoformat()
                for kind in ("ACCOUNT", "FILL", "ORDER", "POSITION")
            },
            overlap_started_at=now - timedelta(seconds=1),
            observed_at=now,
            completed_at=now,
            event_count=0,
            evidence_digest="8" * 64,
        )
        admin.add(batch)
        admin.flush()
        run = admin.get(ReconciliationRun, run_id)
        run.artifact_sha256 = "9" * 64
        run.database_ids = {
            "reconciliation_run": [run_id],
            "recovery_batches": [batch.database_id],
            "order_snapshots": [],
            "position_snapshots": [],
        }
        admin.commit()
    with Session(engine) as runtime:
        runtime.execute(text("SET LOCAL ROLE freqtrade"))
        request = OkxDemoCanaryPreparationService(
            runtime
        ).request_final_attestation_consent(
            idempotency_key=key,
            operator_token="synthetic-test-operator-token",
        )
    capability = _issue_attested_session_capability(
        attestation_hmac_key=b"t" * 32,
        pinned_fingerprint_sha256="8" * 64,
        created_at=now - timedelta(seconds=1),
        expires_at=now + timedelta(minutes=2),
    )

    class Read:
        def capture_execution_attestation(self, db, *, inst_id):
            assert inst_id == "BTC-USDT-SWAP"
            return _capture_runtime_attested_bundle(
                db,
                capability=capability,
                observed_at=datetime.now(timezone.utc),
            )

    runtime_id = "RuntimePrepared627"
    with engine.connect() as connection, Session(bind=connection) as runtime:
        runtime.execute(text("SET LOCAL ROLE freqtrade"))
        assert acquire_one_shot_runtime_lock(runtime) is True
        result = process_pending_canary_consent_handoff(
            read_client=Read(), db=runtime, runtime_instance_id=runtime_id,
            fresh_reconciliation=lambda: SimpleNamespace(reconciliation_run_id=run_id),
            safety_check=lambda: True, now=now,
        )
        assert result is not None
        runtime.commit()
        assert release_one_shot_runtime_lock(runtime) is True
        runtime.commit()
    monkeypatch.setattr(
        "app.services.okx_demo_submission_grant.get_settings",
        lambda: SimpleNamespace(
            demo_automation_policy=SimpleNamespace(
                demo_risk_policy=SimpleNamespace(allowed_instruments=("BTC-USDT-SWAP",))
            ),
            execution_target_manifest=SimpleNamespace(
                active_target_id="OKX_DEMO",
                active_target=SimpleNamespace(
                    simulated_trading=True, allow_real_funds=False,
                    order_submission_enabled=False,
                ),
            ),
        ),
    )
    with engine.connect() as connection, Session(bind=connection) as runtime:
        runtime.execute(text("SET LOCAL ROLE freqtrade"))
        assert acquire_one_shot_runtime_lock(runtime) is True
        grant = arm_finalized_canary_consent(runtime, runtime_instance_id=runtime_id)
        assert grant is not None
        grant_id, approval_id = grant.grant_id, grant.approval_id
        assert release_one_shot_runtime_lock(runtime) is True
        runtime.commit()
    return now, request.handoff_id, grant_id, approval_id


def test_postgresql_v28_consent_exact_finalize_and_restart_revoke(
    postgres_writer_engine, monkeypatch
) -> None:
    upgrade_database(postgres_writer_engine)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with Session(postgres_writer_engine) as admin:
        _seed_final_consent_source(admin, now=now)
        _snapshots, reconciliation_run_id, _order = _seed_canary_lineage_boundary(
            admin, now=now
        )
        baseline_snapshot_count = admin.query(OkxDemoTrustedSnapshot).count()
        source_before = admin.execute(
            text(
                "SELECT request_hash,request_payload::jsonb,status,stage,"
                "evidence_snapshot::jsonb,completed_at FROM research_jobs WHERE id=22"
            )
        ).one()

    with Session(postgres_writer_engine) as runtime:
        runtime.execute(text("SET LOCAL ROLE freqtrade"))
        with pytest.raises(SQLAlchemyError, match="immutable canary source job 22"):
            runtime.execute(text(
                "UPDATE research_jobs SET stage=stage WHERE id=22"
            ))
        runtime.rollback()
        runtime.execute(text("SET LOCAL ROLE freqtrade"))
        with pytest.raises(SQLAlchemyError, match="successor canary source is forbidden"):
            runtime.execute(text(
                "INSERT INTO research_jobs(id,execution_scope_id,job_type,operation,"
                "idempotency_key_digest,request_hash,request_payload,status,stage,"
                "attempt_count,max_attempts,evidence_snapshot,started_at,completed_at,created_at) "
                "SELECT 23,execution_scope_id,job_type,operation,"
                ":digest,request_hash,request_payload,status,stage,attempt_count,max_attempts,"
                "evidence_snapshot,started_at,completed_at,created_at FROM research_jobs WHERE id=22"
            ), {"digest": "9" * 64})
        runtime.rollback()
        for successor_status, entry_kind in (
            ("PENDING", "UNRELATED"),
            ("RUNNING", "FRESH_EXECUTION_ONLY_FINAL_EXPIRY_RECOVERY"),
            ("FAILED", None),
        ):
            runtime.execute(text("SET LOCAL ROLE freqtrade"))
            with pytest.raises(
                SQLAlchemyError, match="successor canary source is forbidden"
            ):
                runtime.execute(text(
                    "INSERT INTO research_jobs(id,execution_scope_id,job_type,operation,"
                    "idempotency_key_digest,request_hash,request_payload,status,stage,"
                    "attempt_count,max_attempts,evidence_snapshot,created_at) "
                    "SELECT 23,execution_scope_id,job_type,operation,:digest,request_hash,"
                    "CAST(:payload AS jsonb),:status,stage,attempt_count,max_attempts,"
                    "evidence_snapshot,created_at FROM research_jobs WHERE id=22"
                ), {
                    "digest": hashlib.sha256(
                        "{}:{}".format(successor_status, entry_kind).encode()
                    ).hexdigest(),
                    "payload": json.dumps({"entry_kind": entry_kind}),
                    "status": successor_status,
                })
            runtime.rollback()
        runtime.execute(text("SET LOCAL ROLE freqtrade"))
        runtime.execute(text(
            "INSERT INTO research_jobs(id,execution_scope_id,job_type,operation,"
            "idempotency_key_digest,request_hash,request_payload,status,stage,"
            "attempt_count,max_attempts,cancel_requested,evidence_snapshot,created_at) "
            "SELECT 23,execution_scope_id,job_type,'unrelated.operation',:digest,"
            "request_hash,'{}'::jsonb,status,stage,attempt_count,max_attempts,FALSE,"
            "evidence_snapshot,created_at FROM research_jobs WHERE id=22"
        ), {"digest": "7" * 64})
        runtime.commit()
        runtime.execute(text("SET LOCAL ROLE freqtrade"))
        with pytest.raises(
            SQLAlchemyError, match="successor canary source is forbidden"
        ):
            runtime.execute(text(
                "UPDATE research_jobs SET operation=:operation WHERE id=23"
            ), {"operation": CANARY_OPERATION})
        runtime.rollback()

    def insert_successor(job_id: int) -> str:
        with Session(postgres_writer_engine) as contender:
            contender.execute(text("SET LOCAL ROLE freqtrade"))
            try:
                contender.execute(text(
                    "INSERT INTO research_jobs(id,execution_scope_id,job_type,operation,"
                    "idempotency_key_digest,request_hash,request_payload,status,stage,"
                    "attempt_count,max_attempts,evidence_snapshot,created_at) "
                    "SELECT :job_id,execution_scope_id,job_type,operation,:digest,"
                    "request_hash,'{}'::jsonb,'PENDING',stage,attempt_count,max_attempts,"
                    "evidence_snapshot,created_at FROM research_jobs WHERE id=22"
                ), {
                    "job_id": job_id,
                    "digest": hashlib.sha256(str(job_id).encode()).hexdigest(),
                })
                contender.commit()
                return "INSERTED"
            except SQLAlchemyError:
                contender.rollback()
                return "BLOCKED"

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(insert_successor, (24, 25))) == [
            "BLOCKED", "BLOCKED"
        ]

    forged_key = "f" * 64
    forged_payload = _canonical_json({
        "authorization": "once",
        "consent_policy": "immutable-job-22-final-attestation-v1",
        "execution_target": "OKX_DEMO",
        "idempotency_key_digest": forged_key,
        "instrument_id": "BTC-USDT-SWAP",
        "max_notional": "20",
        "operation": "okx-demo-canary-consent-finalize",
        "source_ancestry": [15, 16, 17, 18, 19, 20, 21, 22],
        "source_job_id": 22,
    })
    with Session(postgres_writer_engine) as runtime:
        runtime.execute(text("SET LOCAL ROLE freqtrade"))
        with pytest.raises(SQLAlchemyError, match="invalid controlled canary operator proof"):
            runtime.execute(text(
                "SELECT request_okx_demo_canary_consent("
                ":key,:nonce,:payload,:proof)"
            ), {
                "key": forged_key,
                "nonce": "e" * 64,
                "payload": forged_payload,
                "proof": "0" * 64,
            })
        runtime.rollback()
        runtime.execute(text("SET LOCAL ROLE freqtrade"))
        with pytest.raises(SQLAlchemyError):
            runtime.execute(text("SELECT * FROM okx_demo_operator_consent_secrets"))
        runtime.rollback()

    with Session(postgres_writer_engine) as runtime:
        runtime.execute(text("SET LOCAL ROLE freqtrade"))
        requested = OkxDemoCanaryPreparationService(
            runtime
        ).request_final_attestation_consent(
            idempotency_key="issue-627-consent",
            operator_token="synthetic-test-operator-token",
        )
        assert requested.operation_status == "REQUESTED"
        runtime.execute(text("SET LOCAL ROLE freqtrade"))
        with pytest.raises(SQLAlchemyError):
            runtime.execute(text("SELECT * FROM okx_demo_canary_consent_handoffs"))
        runtime.rollback()

    monkeypatch.setenv(OPERATOR_TOKEN_ENV, "synthetic-rotated-operator-token")
    with pytest.raises(SchemaMigrationBlocked, match="active handoff"):
        harden_operator_consent_access_boundary(postgres_writer_engine)
    monkeypatch.setenv(OPERATOR_TOKEN_ENV, "synthetic-test-operator-token")

    with postgres_writer_engine.connect() as locker_connection, \
         postgres_writer_engine.connect() as contender_connection, \
         Session(bind=locker_connection) as locker, \
         Session(bind=contender_connection) as contender:
        locker.execute(text("SET LOCAL ROLE freqtrade"))
        assert acquire_one_shot_runtime_lock(locker) is True
        contender.execute(text("SET LOCAL ROLE freqtrade"))
        with pytest.raises(SQLAlchemyError, match="coordination lock is busy"):
            contender.execute(
                text(
                    "SELECT claim_okx_demo_canary_consent("
                    ":handoff,:runtime,:run,'{}'::jsonb)"
                ),
                {
                    "handoff": requested.handoff_id,
                    "runtime": "RuntimeContender1",
                    "run": reconciliation_run_id,
                },
            )
        contender.rollback()
        assert release_one_shot_runtime_lock(locker) is True
        locker.commit()

    capture_capability = _issue_attested_session_capability(
        attestation_hmac_key=b"t" * 32,
        pinned_fingerprint_sha256="b" * 64,
        created_at=now - timedelta(seconds=1),
        expires_at=now + timedelta(minutes=2),
    )
    distractor_capability = _issue_attested_session_capability(
        attestation_hmac_key=b"t" * 32,
        pinned_fingerprint_sha256="c" * 64,
        created_at=now - timedelta(seconds=1),
        expires_at=now + timedelta(minutes=2),
    )
    captured_database_ids = []
    class RuntimeRead:
        def capture_execution_attestation(self, db, *, inst_id):
            assert inst_id == "BTC-USDT-SWAP"
            observed_at = datetime.now(timezone.utc)
            exact = _capture_runtime_attested_bundle(
                db, capability=capture_capability, observed_at=observed_at
            )
            captured_database_ids.append(
                tuple(getattr(exact, kind).database_id for kind in ("instrument", "market", "account"))
            )
            # Same observed_at but later database ids: a global-latest lookup
            # would select this different attested session.
            _capture_runtime_attested_bundle(
                db, capability=distractor_capability, observed_at=observed_at
            )
            return exact

    runtime_id = "RuntimeIssue627A"
    persist_lineage = OkxDemoCanaryPreparationService._persist_lineage
    monkeypatch.setattr(
        OkxDemoCanaryPreparationService,
        "_persist_lineage",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("synthetic crash after exact claim")
        ),
    )
    with postgres_writer_engine.connect() as crash_connection, Session(
        bind=crash_connection
    ) as crashed_runtime:
        crashed_runtime.execute(text("SET LOCAL ROLE freqtrade"))
        assert acquire_one_shot_runtime_lock(crashed_runtime) is True
        with pytest.raises(OkxDemoCanaryConsentCaptureFailed) as failure:
            process_pending_canary_consent_handoff(
                read_client=RuntimeRead(),
                db=crashed_runtime,
                runtime_instance_id=runtime_id,
                fresh_reconciliation=lambda: {
                    "reconciliation_run_id": reconciliation_run_id
                },
                safety_check=lambda: True,
                now=now,
            )
        assert failure.value.stage == "LINEAGE_PERSIST"
        assert failure.value.category == "UNEXPECTED"
        assert "synthetic crash after exact claim" not in str(failure.value)
        crashed_runtime.rollback()
        assert release_one_shot_runtime_lock(crashed_runtime) is True
        crashed_runtime.commit()
    monkeypatch.setattr(
        OkxDemoCanaryPreparationService, "_persist_lineage", persist_lineage
    )
    with Session(postgres_writer_engine) as admin:
        assert admin.query(OkxDemoTrustedSnapshot).count() == baseline_snapshot_count
        assert admin.query(TradeIntent).count() == 0

    with postgres_writer_engine.connect() as runtime_connection, Session(
        bind=runtime_connection
    ) as runtime:
        runtime.execute(text("SET LOCAL ROLE freqtrade"))
        assert acquire_one_shot_runtime_lock(runtime) is True
        finalized = process_pending_canary_consent_handoff(
            read_client=RuntimeRead(),
            db=runtime,
            runtime_instance_id=runtime_id,
            # The production reconciliation adapter returns a Mapping.  Keep
            # this end-to-end PostgreSQL path on that exact contract.
            fresh_reconciliation=lambda: {
                "reconciliation_run_id": reconciliation_run_id,
                "status": "RECOVERED",
                "safe_to_open": True,
            },
            safety_check=lambda: True,
            now=now,
        )
        assert finalized is not None
        runtime.commit()  # commit A: lineage only
        assert release_one_shot_runtime_lock(runtime) is True
        runtime.commit()

    with Session(postgres_writer_engine) as runtime:
        runtime.execute(text("SET LOCAL ROLE freqtrade"))
        with pytest.raises(SQLAlchemyError):
            runtime.execute(text(
                "UPDATE okx_demo_trusted_snapshots SET digest=:digest "
                "WHERE database_id=:database_id"
            ), {
                "digest": "f" * 64,
                "database_id": captured_database_ids[-1][0],
            })
        runtime.rollback()
    with Session(postgres_writer_engine) as admin:
        handoff = admin.get(OkxDemoCanaryConsentHandoff, requested.handoff_id)
        bound_ids = tuple(
            int(handoff.snapshot_binding[kind]["database_id"])
            for kind in ("instrument", "market", "account")
        )
        assert bound_ids == captured_database_ids[-1]
        global_latest = tuple(admin.execute(text(
            "SELECT database_id FROM okx_demo_trusted_snapshots WHERE kind=:kind "
            "ORDER BY observed_at DESC,database_id DESC LIMIT 1"
        ), {"kind": kind}).scalar_one() for kind in ("instrument", "market", "account"))
        assert global_latest != bound_ids

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
    monkeypatch.setattr(
        "app.services.okx_demo_submission_grant.GRANT_TTL_SECONDS", 1
    )
    takeover_runtime_id = "RuntimeIssue627B"
    with postgres_writer_engine.connect() as arm_connection, Session(
        bind=arm_connection
    ) as runtime:
        runtime.execute(text("SET LOCAL ROLE freqtrade"))
        assert acquire_one_shot_runtime_lock(runtime) is True
        grant = arm_finalized_canary_consent(
            runtime, runtime_instance_id=takeover_runtime_id
        )
        assert grant is not None and grant.status == "ACTIVE"
        grant_id = grant.grant_id
        assert release_one_shot_runtime_lock(runtime) is True
        runtime.commit()

    time.sleep(1.1)
    with postgres_writer_engine.connect() as restart_connection, Session(
        bind=restart_connection
    ) as restarted:
        restarted.execute(text("SET LOCAL ROLE freqtrade"))
        assert acquire_one_shot_runtime_lock(restarted) is True
        assert arm_finalized_canary_consent(
            restarted, runtime_instance_id="RuntimeIssue627C"
        ) is None
        assert release_one_shot_runtime_lock(restarted) is True
        restarted.commit()

    with Session(postgres_writer_engine) as admin:
        source_after = admin.execute(
            text(
                "SELECT request_hash,request_payload::jsonb,status,stage,"
                "evidence_snapshot::jsonb,completed_at FROM research_jobs WHERE id=22"
            )
        ).one()
        handoff = admin.get(OkxDemoCanaryConsentHandoff, requested.handoff_id)
        assert source_after == source_before
        assert handoff.status == "EXPIRED"
        assert handoff.runtime_instance_id == takeover_runtime_id
        assert handoff.failure_code == "GRANT_RECONCILIATION_TERMINAL"
        assert admin.query(TradeIntent).count() == 1
        assert admin.query(ApprovedExecution).count() == 1
        assert admin.query(OkxOrderWriteAttempt).count() == 0
        assert admin.query(OkxDemoCanaryLifecycle).count() == 0
        assert admin.get(OkxDemoSubmissionGrant, grant_id).status == "EXPIRED"
        assert admin.get(ApprovedExecution, handoff.approval_id).status == "EXPIRED"
        reserved, positions = admin.execute(text(
            "SELECT COALESCE(sum(reserved_notional),0),"
            "COALESCE(sum(approved_positions),0) FROM risk_budgets "
            "WHERE execution_target_id='OKX_DEMO'"
        )).one()
        assert reserved == 0
        assert positions == 0
    monkeypatch.setenv(OPERATOR_TOKEN_ENV, "synthetic-rotated-operator-token")
    harden_operator_consent_access_boundary(postgres_writer_engine)


def test_postgresql_v29_terminal_consent_allows_one_fresh_request_only(
    postgres_writer_engine,
) -> None:
    upgrade_database(postgres_writer_engine)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with Session(postgres_writer_engine) as admin:
        _seed_final_consent_source(admin, now=now)

    with Session(postgres_writer_engine) as runtime:
        runtime.execute(text("SET LOCAL ROLE freqtrade"))
        old = OkxDemoCanaryPreparationService(
            runtime
        ).request_final_attestation_consent(
            idempotency_key="issue-627-terminal-old",
            operator_token="synthetic-test-operator-token",
        )
    with postgres_writer_engine.begin() as admin:
        admin.execute(
            text(
                "UPDATE okx_demo_canary_consent_handoffs "
                "SET status='EXPIRED',failure_code='TEST_TERMINAL',"
                "updated_at=statement_timestamp() WHERE handoff_id=:handoff_id"
            ),
            {"handoff_id": old.handoff_id},
        )
        terminal_before = admin.execute(
            text(
                "SELECT handoff_id,idempotency_key_digest,status,failure_code,created_at "
                "FROM okx_demo_canary_consent_handoffs WHERE handoff_id=:handoff_id"
            ),
            {"handoff_id": old.handoff_id},
        ).one()
        admin.execute(
            text("DROP INDEX okx_demo_canary_consent_active_source_unique")
        )
        admin.execute(
            text(
                "ALTER TABLE okx_demo_canary_consent_handoffs ADD CONSTRAINT "
                "okx_demo_canary_consent_source_unique UNIQUE(source_job_id)"
            )
        )
        admin.execute(text("DELETE FROM {}".format(VERSION_TABLE)))
        admin.execute(
            text("INSERT INTO {}(version) VALUES(:version)".format(VERSION_TABLE)),
            {"version": CANARY_CONSENT_HANDOFF_BASE_VERSION},
        )

    assert upgrade_database(postgres_writer_engine) == SCHEMA_VERSION
    assert schema_problems(postgres_writer_engine) == []
    with postgres_writer_engine.begin() as admin:
        assert admin.execute(
            text(
                "SELECT handoff_id,idempotency_key_digest,status,failure_code,created_at "
                "FROM okx_demo_canary_consent_handoffs WHERE handoff_id=:handoff_id"
            ),
            {"handoff_id": old.handoff_id},
        ).one() == terminal_before

    def request(key: str) -> tuple[str, str]:
        with Session(postgres_writer_engine) as runtime:
            runtime.execute(text("SET LOCAL ROLE freqtrade"))
            try:
                result = OkxDemoCanaryPreparationService(
                    runtime
                ).request_final_attestation_consent(
                    idempotency_key=key,
                    operator_token="synthetic-test-operator-token",
                )
                return result.operation_status, result.handoff_id
            except OkxDemoCanaryPreparationBlocked as exc:
                return "BLOCKED", str(exc)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(
                request,
                ("issue-627-terminal-fresh-a", "issue-627-terminal-fresh-b"),
            )
        )
    assert sorted(status for status, _detail in outcomes) == ["BLOCKED", "REQUESTED"]
    assert all(
        detail == "controlled canary consent request was rejected"
        for status, detail in outcomes
        if status == "BLOCKED"
    )

    old_retry_status, old_retry_handoff = request("issue-627-terminal-old")
    assert (old_retry_status, old_retry_handoff) == ("EXPIRED", old.handoff_id)
    with Session(postgres_writer_engine) as admin:
        assert admin.query(OkxDemoCanaryConsentHandoff).count() == 2
        assert admin.execute(
            text(
                "SELECT count(*) FROM okx_demo_canary_consent_handoffs "
                "WHERE source_job_id=22 "
                "AND status IN ('REQUESTED','FINALIZED','GRANT_ISSUED')"
            )
        ).scalar_one() == 1


def test_postgresql_v30_precommit_failure_is_exact_atomic_and_restart_safe(
    postgres_writer_engine,
) -> None:
    upgrade_database(postgres_writer_engine)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with Session(postgres_writer_engine) as admin:
        _seed_final_consent_source(admin, now=now)
    with Session(postgres_writer_engine) as runtime:
        runtime.execute(text("SET LOCAL ROLE freqtrade"))
        first = OkxDemoCanaryPreparationService(
            runtime
        ).request_final_attestation_consent(
            idempotency_key="issue-627-v30-concurrent-terminal",
            operator_token="synthetic-test-operator-token",
        )

    def terminalize(_index: int) -> bool:
        with Session(postgres_writer_engine) as runtime:
            runtime.execute(text("SET LOCAL ROLE freqtrade"))
            result = runtime.execute(
                text(
                    "SELECT fail_requested_okx_demo_canary_consent("
                    ":handoff,'FRESH_RECONCILIATION','SAFETY')"
                ),
                {"handoff": first.handoff_id},
            ).scalar_one()
            runtime.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(terminalize, (1, 2))) == [False, True]

    with Session(postgres_writer_engine) as runtime:
        runtime.execute(text("SET LOCAL ROLE freqtrade"))
        second = OkxDemoCanaryPreparationService(
            runtime
        ).request_final_attestation_consent(
            idempotency_key="issue-627-v30-capture-failure",
            operator_token="synthetic-test-operator-token",
        )
        runtime.execute(text("SET LOCAL ROLE freqtrade"))
        with pytest.raises(OkxDemoCanaryConsentCaptureFailed) as failure:
            process_pending_canary_consent_handoff(
                read_client=SimpleNamespace(),
                db=runtime,
                runtime_instance_id="RuntimeIssue627V30",
                fresh_reconciliation=lambda: (_ for _ in ()).throw(
                    RuntimeError("sensitive synthetic detail")
                ),
                safety_check=lambda: True,
                now=now,
            )
        assert failure.value.handoff_id == second.handoff_id
        assert failure.value.stage == "FRESH_RECONCILIATION"
        assert failure.value.category == "UNEXPECTED"
        assert "sensitive synthetic detail" not in str(failure.value)
        runtime.rollback()
        runtime.execute(text("SET LOCAL ROLE freqtrade"))
        assert runtime.execute(
            text(
                "SELECT fail_requested_okx_demo_canary_consent("
                ":handoff,:stage,:category)"
            ),
            {
                "handoff": failure.value.handoff_id,
                "stage": failure.value.stage,
                "category": failure.value.category,
            },
        ).scalar_one() is True
        runtime.commit()

    invalid_observations = (
        {},
        {"reconciliation_run_id": True},
        {"reconciliation_run_id": "123"},
        {"reconciliation_run_id": 0},
        {"reconciliation_run_id": -1},
        True,
        "123",
        None,
    )
    for index, observation in enumerate(invalid_observations):
        with Session(postgres_writer_engine) as runtime:
            runtime.execute(text("SET LOCAL ROLE freqtrade"))
            requested = OkxDemoCanaryPreparationService(
                runtime
            ).request_final_attestation_consent(
                idempotency_key=f"issue-627-v30-invalid-mapping-{index}",
                operator_token="synthetic-test-operator-token",
            )
            runtime.execute(text("SET LOCAL ROLE freqtrade"))
            with pytest.raises(OkxDemoCanaryConsentCaptureFailed) as failure:
                process_pending_canary_consent_handoff(
                    read_client=SimpleNamespace(),
                    db=runtime,
                    runtime_instance_id="RuntimeIssue627V30Mapping",
                    fresh_reconciliation=lambda value=observation: value,
                    safety_check=lambda: True,
                    now=now,
                )
            assert failure.value.handoff_id == requested.handoff_id
            assert failure.value.stage == "FRESH_RECONCILIATION"
            assert failure.value.category == "SAFETY"
            runtime.rollback()
            runtime.execute(text("SET LOCAL ROLE freqtrade"))
            assert runtime.execute(
                text(
                    "SELECT fail_requested_okx_demo_canary_consent("
                    ":handoff,:stage,:category)"
                ),
                {
                    "handoff": failure.value.handoff_id,
                    "stage": failure.value.stage,
                    "category": failure.value.category,
                },
            ).scalar_one() is True
            runtime.commit()

    with Session(postgres_writer_engine) as restarted:
        restarted.execute(text("SET LOCAL ROLE freqtrade"))
        assert restarted.execute(
            text("SELECT pending_okx_demo_canary_consent()")
        ).scalar_one() is None
        restarted.commit()
    with Session(postgres_writer_engine) as admin:
        statuses = dict(admin.execute(text(
            "SELECT failure_code,status FROM okx_demo_canary_consent_handoffs"
        )).all())
        assert statuses == {
            "CAPTURE_FRESH_RECONCILIATION_UNEXPECTED": "EXPIRED",
            "CAPTURE_FRESH_RECONCILIATION_SAFETY": "EXPIRED",
        }
        assert admin.query(OkxDemoCanaryConsentHandoff).count() == 10
        assert admin.query(TradeIntent).count() == 0
        assert admin.query(ApprovedExecution).count() == 0
        assert admin.query(OkxDemoSubmissionGrant).count() == 0
        assert admin.query(OkxOrderWriteAttempt).count() == 0
        assert admin.query(ExchangeOrder).count() == 0
        assert admin.query(ExchangeFill).count() == 0
        assert admin.query(OkxDemoCanaryLifecycle).count() == 0


def test_postgresql_v29_to_v30_preserves_terminal_history(
    postgres_writer_engine,
) -> None:
    upgrade_database(postgres_writer_engine)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with Session(postgres_writer_engine) as admin:
        _seed_final_consent_source(admin, now=now)
    with Session(postgres_writer_engine) as runtime:
        runtime.execute(text("SET LOCAL ROLE freqtrade"))
        requested = OkxDemoCanaryPreparationService(
            runtime
        ).request_final_attestation_consent(
            idempotency_key="issue-627-v30-migration-history",
            operator_token="synthetic-test-operator-token",
        )
    with postgres_writer_engine.begin() as admin:
        admin.execute(text(
            "UPDATE okx_demo_canary_consent_handoffs SET status='EXPIRED',"
            "failure_code='INSUFFICIENT_CAPTURE_BUDGET' "
            "WHERE handoff_id=:handoff"
        ), {"handoff": requested.handoff_id})
        before = admin.execute(text(
            "SELECT handoff_id,status,failure_code,created_at,updated_at "
            "FROM okx_demo_canary_consent_handoffs WHERE handoff_id=:handoff"
        ), {"handoff": requested.handoff_id}).one()
        admin.execute(text(
            "DROP FUNCTION fail_requested_okx_demo_canary_consent(text,text,text)"
        ))
        admin.execute(text("DELETE FROM {}".format(VERSION_TABLE)))
        admin.execute(text(
            "INSERT INTO {}(version) VALUES(:version)".format(VERSION_TABLE)
        ), {"version": CANARY_CONSENT_FAILURE_AUDIT_BASE_VERSION})
    assert upgrade_database(postgres_writer_engine) == SCHEMA_VERSION
    assert schema_problems(postgres_writer_engine) == []
    with postgres_writer_engine.connect() as admin:
        after = admin.execute(text(
            "SELECT handoff_id,status,failure_code,created_at,updated_at "
            "FROM okx_demo_canary_consent_handoffs WHERE handoff_id=:handoff"
        ), {"handoff": requested.handoff_id}).one()
        assert after == before


def test_postgresql_v28_migration_lock_closes_successor_trigger_window(
    postgres_writer_engine,
) -> None:
    upgrade_database(postgres_writer_engine)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with Session(postgres_writer_engine) as admin:
        _seed_final_consent_source(admin, now=now)
    with postgres_writer_engine.begin() as admin:
        admin.execute(text(
            "DROP TRIGGER freeze_okx_demo_canary_source ON research_jobs"
        ))

    barrier = Barrier(2)
    def race_successor_insert() -> str:
        with postgres_writer_engine.connect() as contender:
            transaction = contender.begin()
            contender.execute(text("SET LOCAL statement_timeout='5s'"))
            barrier.wait()
            try:
                contender.execute(text(
                    "INSERT INTO research_jobs(id,execution_scope_id,job_type,operation,"
                    "idempotency_key_digest,request_hash,request_payload,status,stage,"
                    "attempt_count,max_attempts,cancel_requested,evidence_snapshot,created_at) "
                    "SELECT 23,execution_scope_id,job_type,operation,:digest,request_hash,"
                    "'{}'::jsonb,'PENDING',stage,attempt_count,max_attempts,FALSE,"
                    "evidence_snapshot,created_at FROM research_jobs WHERE id=22"
                ), {"digest": "4" * 64})
                transaction.commit()
                return "INSERTED"
            except SQLAlchemyError:
                transaction.rollback()
                return "BLOCKED"

    with postgres_writer_engine.connect() as migrator:
        transaction = migrator.begin()
        migrator.execute(text(
            "LOCK TABLE research_jobs IN SHARE ROW EXCLUSIVE MODE"
        ))
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(race_successor_insert)
            barrier.wait()
            time.sleep(0.1)
            assert future.done() is False
            _add_canary_consent_handoff_boundary(migrator)
            transaction.commit()
            assert future.result(timeout=5) == "BLOCKED"
    with postgres_writer_engine.connect() as admin:
        assert admin.execute(text(
            "SELECT count(*) FROM research_jobs WHERE id=23"
        )).scalar_one() == 0


def test_postgresql_v28_five_second_expiry_rolls_back_without_lineage(
    postgres_writer_engine,
) -> None:
    upgrade_database(postgres_writer_engine)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with Session(postgres_writer_engine) as admin:
        _seed_final_consent_source(admin, now=now)
        _snapshots, run_id, _order = _seed_canary_lineage_boundary(
            admin, now=now, snapshot_ttl_seconds=5
        )
        baseline_snapshot_count = admin.query(OkxDemoTrustedSnapshot).count()
    with Session(postgres_writer_engine) as runtime:
        runtime.execute(text("SET LOCAL ROLE freqtrade"))
        requested = OkxDemoCanaryPreparationService(
            runtime
        ).request_final_attestation_consent(
            idempotency_key="issue-627-five-second",
            operator_token="synthetic-test-operator-token",
        )

    slow_capability = _issue_attested_session_capability(
        attestation_hmac_key=b"t" * 32,
        pinned_fingerprint_sha256="d" * 64,
        created_at=now - timedelta(seconds=1),
        expires_at=now + timedelta(minutes=2),
    )
    class SlowRead:
        def capture_execution_attestation(self, db, *, inst_id):
            assert inst_id == "BTC-USDT-SWAP"
            bundle = _capture_runtime_attested_bundle(
                db,
                capability=slow_capability,
                observed_at=datetime.now(timezone.utc),
                ttl_seconds=5,
            )
            time.sleep(5.2)
            return bundle

    with Session(postgres_writer_engine) as runtime:
        runtime.execute(text("SET LOCAL ROLE freqtrade"))
        assert acquire_one_shot_runtime_lock(runtime) is True
        with pytest.raises(OkxDemoCanaryConsentCaptureFailed) as captured:
            process_pending_canary_consent_handoff(
                read_client=SlowRead(),
                db=runtime,
                runtime_instance_id="RuntimeIssue627TTL",
                fresh_reconciliation=lambda: SimpleNamespace(
                    reconciliation_run_id=run_id
                ),
                safety_check=lambda: True,
                now=now,
            )
        assert captured.value.stage == "HANDOFF_CLAIM"
        assert captured.value.category == "DATABASE"
        runtime.rollback()
        assert release_one_shot_runtime_lock(runtime) is True
        runtime.commit()

    with Session(postgres_writer_engine) as admin:
        handoff = admin.get(OkxDemoCanaryConsentHandoff, requested.handoff_id)
        assert handoff.status == "REQUESTED"
        assert admin.query(TradeIntent).count() == 0
        assert admin.query(ApprovedExecution).count() == 0
        assert admin.query(OkxDemoSubmissionGrant).count() == 0
        assert admin.query(OkxOrderWriteAttempt).count() == 0
        assert admin.query(OkxDemoCanaryLifecycle).count() == 0
        assert admin.query(OkxDemoTrustedSnapshot).count() == baseline_snapshot_count


def _prepare_v31_atomic_successor(engine, monkeypatch, *, suffix: str):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with Session(engine) as admin:
        _seed_final_consent_source(admin, now=now)
        _snapshots, run_id, _order = _seed_canary_lineage_boundary(admin, now=now)
        batch = OkxDemoRecoveryBatch(
            execution_target_id="OKX_DEMO",
            recovery_batch_id=uuid4().hex,
            authenticated=True,
            pagination_complete=True,
            complete_streams=["ACCOUNT", "FILL", "ORDER", "POSITION"],
            high_watermarks={
                kind: now.isoformat()
                for kind in ("ACCOUNT", "FILL", "ORDER", "POSITION")
            },
            overlap_started_at=now - timedelta(seconds=1),
            observed_at=now,
            completed_at=now,
            event_count=0,
            evidence_digest="8" * 64,
        )
        admin.add(batch)
        admin.flush()
        run = admin.get(ReconciliationRun, run_id)
        run.artifact_sha256 = "9" * 64
        run.database_ids = {
            "reconciliation_run": [run_id],
            "recovery_batches": [batch.database_id],
            "order_snapshots": [],
            "position_snapshots": [],
        }
        admin.commit()
    with Session(engine) as runtime:
        runtime.execute(text("SET LOCAL ROLE freqtrade"))
        predecessor = OkxDemoCanaryPreparationService(
            runtime
        ).request_final_attestation_consent(
            idempotency_key="v31-predecessor-" + suffix,
            operator_token="synthetic-test-operator-token",
        )
    capability = _issue_attested_session_capability(
        attestation_hmac_key=b"t" * 32,
        pinned_fingerprint_sha256="e" * 64,
        created_at=now - timedelta(seconds=1),
        expires_at=now + timedelta(minutes=2),
    )

    class Read:
        def __init__(self, ttl_seconds=5):
            self.ttl_seconds = ttl_seconds

        def capture_execution_attestation(self, db, *, inst_id):
            assert inst_id == "BTC-USDT-SWAP"
            return _capture_runtime_attested_bundle(
                db,
                capability=capability,
                observed_at=datetime.now(timezone.utc),
                ttl_seconds=self.ttl_seconds,
            )

    with Session(engine) as runtime:
        runtime.execute(text("SET LOCAL ROLE freqtrade"))
        assert acquire_one_shot_runtime_lock(runtime) is True
        finalized = process_pending_canary_consent_handoff(
            read_client=Read(30),
            db=runtime,
            runtime_instance_id="RuntimeV31Predecessor",
            fresh_reconciliation=lambda: {"reconciliation_run_id": run_id},
            safety_check=lambda: True,
            now=now,
        )
        assert finalized is not None and finalized.atomic_receipt is None
        runtime.commit()
    with Session(engine) as admin:
        handoff = admin.get(OkxDemoCanaryConsentHandoff, predecessor.handoff_id)
        approval = admin.get(ApprovedExecution, handoff.approval_id)
        handoff.status = "EXPIRED"
        handoff.failure_code = "FINALIZED_EVIDENCE_EXPIRED"
        handoff.revoked_at = datetime.now(timezone.utc)
        approval.status = "EXPIRED"
        for chain in admin.scalars(
            select(FullChainRun).where(
                FullChainRun.approved_execution_id == approval.id
            )
        ):
            chain.status = "BLOCKED"
            chain.terminal_reason = "finalized consent expired before grant"
        budgets = admin.scalars(
            select(RiskBudget).where(RiskBudget.execution_target_id == "OKX_DEMO")
        ).all()
        for budget in budgets:
            budget.reserved_notional = Decimal("0")
            budget.approved_positions = 0
        admin.commit()
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
    with Session(engine) as runtime:
        runtime.execute(text("SET LOCAL ROLE freqtrade"))
        successor = OkxDemoCanaryPreparationService(
            runtime
        ).request_final_attestation_consent(
            idempotency_key="v31-successor-" + suffix,
            operator_token="synthetic-test-operator-token",
        )
    return now, run_id, predecessor, successor, Read


def test_postgresql_v31_atomic_prepare_commit_and_dispatch_cas(
    postgres_writer_engine, monkeypatch
) -> None:
    upgrade_database(postgres_writer_engine)
    now, run_id, predecessor, successor, read_type = _prepare_v31_atomic_successor(
        postgres_writer_engine, monkeypatch, suffix="commit"
    )
    with Session(postgres_writer_engine) as admin:
        successor_row = admin.get(OkxDemoCanaryConsentHandoff, successor.handoff_id)
        assert successor_row.supersedes_handoff_id == predecessor.handoff_id

    with postgres_writer_engine.connect() as connection, Session(bind=connection) as runtime:
        runtime.execute(text("SET LOCAL ROLE freqtrade"))
        store = SqlAlchemyOrderWriterStore(runtime)
        assert acquire_one_shot_runtime_lock(runtime) is True
        prepared = process_pending_canary_consent_handoff(
            read_client=read_type(),
            db=runtime,
            runtime_instance_id="RuntimeV31Atomic",
            fresh_reconciliation=lambda: {"reconciliation_run_id": run_id},
            safety_check=lambda: True,
            atomic_holder_token_digest=store.atomic_process_token_digest,
            now=now,
        )
        assert prepared is not None and prepared.atomic_receipt is not None
        receipt = dict(prepared.atomic_receipt)
        assert receipt["dispatch_guard_policy"] == "db-clock-monotonic-v2"
        assert receipt["dispatch_guard_ms"] == 1000
        assert receipt["dispatch_claim_min_remaining_ms"] == 500
        assert receipt["post_start_reserve_ms"] == 100
        runtime.rollback()

    with Session(postgres_writer_engine) as admin:
        assert admin.get(OkxDemoCanaryConsentHandoff, successor.handoff_id).status == "REQUESTED"
        assert admin.query(OkxDemoSubmissionGrant).count() == 0
        assert admin.query(OkxOrderWriteAttempt).count() == 0
        assert admin.query(OkxDemoCanaryLifecycle).count() == 0

    with postgres_writer_engine.connect() as connection, Session(bind=connection) as runtime:
        runtime.execute(text("SET LOCAL ROLE freqtrade"))
        store = SqlAlchemyOrderWriterStore(runtime)
        assert acquire_one_shot_runtime_lock(runtime) is True
        prepared = process_pending_canary_consent_handoff(
            read_client=read_type(),
            db=runtime,
            runtime_instance_id="RuntimeV31Atomic",
            fresh_reconciliation=lambda: {"reconciliation_run_id": run_id},
            safety_check=lambda: True,
            atomic_holder_token_digest=store.atomic_process_token_digest,
            now=now,
        )
        receipt = dict(prepared.atomic_receipt)
        runtime.commit()
        wrong = runtime.execute(text(
            "SELECT claim_atomic_okx_demo_canary_dispatch("
            ":attempt,:runtime,:token,:generation,:digest)"
        ), {
            "attempt": receipt["attempt_id"], "runtime": "RuntimeV31Atomic",
            "token": "0" * 64, "generation": receipt["lease_generation"],
            "digest": receipt["request_digest"],
        }).scalar_one()
        assert wrong is None
        runtime.rollback()
        runtime.execute(text("SET LOCAL ROLE freqtrade"))
        claimed = runtime.execute(text(
            "SELECT claim_atomic_okx_demo_canary_dispatch("
            ":attempt,:runtime,:token,:generation,:digest)"
        ), {
            "attempt": receipt["attempt_id"], "runtime": "RuntimeV31Atomic",
            "token": store.atomic_process_token,
            "generation": receipt["lease_generation"],
            "digest": receipt["request_digest"],
        }).scalar_one()
        assert claimed is not None
        runtime.commit()
        runtime.execute(text("SET LOCAL ROLE freqtrade"))
        assert store.validate_atomic_dispatch_authority(
            attempt_id=int(receipt["attempt_id"]),
            runtime_instance_id="RuntimeV31Atomic",
            lease_generation=int(receipt["lease_generation"]),
            request_digest=str(receipt["request_digest"]),
            bundle_digest=str(receipt["bundle_digest"]),
        ) >= 100
        runtime.execute(text("SET LOCAL ROLE freqtrade"))
        duplicate = runtime.execute(text(
            "SELECT claim_atomic_okx_demo_canary_dispatch("
            ":attempt,:runtime,:token,:generation,:digest)"
        ), {
            "attempt": receipt["attempt_id"], "runtime": "RuntimeV31Atomic",
            "token": store.atomic_process_token,
            "generation": receipt["lease_generation"],
            "digest": receipt["request_digest"],
        }).scalar_one()
        assert duplicate is None
        runtime.rollback()

    with Session(postgres_writer_engine) as admin:
        handoff = admin.get(OkxDemoCanaryConsentHandoff, successor.handoff_id)
        attempt = admin.get(OkxOrderWriteAttempt, receipt["attempt_id"])
        assert handoff.status == "CONSUMED"
        assert attempt.state == "DISPATCHED"
        assert admin.query(OkxDemoSubmissionGrant).one().status == "CONSUMED"
        assert admin.query(OkxDemoCanaryLifecycle).one().cleanup_phase == "OPENING_SUBMITTED"


def test_postgresql_v31_dispatch_guard_expiry_is_zero_claim(
    postgres_writer_engine, monkeypatch
) -> None:
    upgrade_database(postgres_writer_engine)
    now, run_id, _predecessor, _successor, read_type = _prepare_v31_atomic_successor(
        postgres_writer_engine, monkeypatch, suffix="guard"
    )
    with postgres_writer_engine.connect() as connection, Session(bind=connection) as runtime:
        runtime.execute(text("SET LOCAL ROLE freqtrade"))
        store = SqlAlchemyOrderWriterStore(runtime)
        assert acquire_one_shot_runtime_lock(runtime) is True
        prepared = process_pending_canary_consent_handoff(
            read_client=read_type(), db=runtime,
            runtime_instance_id="RuntimeV31Guard",
            fresh_reconciliation=lambda: {"reconciliation_run_id": run_id},
            safety_check=lambda: True,
            atomic_holder_token_digest=store.atomic_process_token_digest,
            now=now,
        )
        receipt = dict(prepared.atomic_receipt)
        runtime.commit()
        time.sleep(1.1)
        expired = runtime.execute(text(
            "SELECT claim_atomic_okx_demo_canary_dispatch("
            ":attempt,:runtime,:token,:generation,:digest)"
        ), {
            "attempt": receipt["attempt_id"], "runtime": "RuntimeV31Guard",
            "token": store.atomic_process_token,
            "generation": receipt["lease_generation"],
            "digest": receipt["request_digest"],
        }).scalar_one()
        assert expired is None
        runtime.rollback()
    with Session(postgres_writer_engine) as admin:
        assert admin.get(OkxOrderWriteAttempt, receipt["attempt_id"]).state == "PREPARED"


def test_postgresql_v28_missing_consent_key_requires_explicit_terminalization(
    postgres_writer_engine, monkeypatch
) -> None:
    upgrade_database(postgres_writer_engine)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with Session(postgres_writer_engine) as admin:
        _seed_final_consent_source(admin, now=now)
    with Session(postgres_writer_engine) as runtime:
        runtime.execute(text("SET LOCAL ROLE freqtrade"))
        requested = OkxDemoCanaryPreparationService(
            runtime
        ).request_final_attestation_consent(
            idempotency_key="issue-627-restore-missing-key",
            operator_token="synthetic-test-operator-token",
        )

    # Both secret tables are intentionally excluded from backup.  Model the
    # post-restore state before provisioning a new host-local verifier key.
    with postgres_writer_engine.begin() as admin:
        admin.execute(text("DELETE FROM okx_demo_operator_consent_secrets"))
    monkeypatch.setenv(OPERATOR_TOKEN_ENV, "synthetic-restored-operator-token")
    with pytest.raises(SchemaMigrationBlocked, match="active handoff"):
        harden_operator_consent_access_boundary(postgres_writer_engine)

    assert revoke_operator_consents_for_key_hardening(
        postgres_writer_engine
    ) == 1
    with Session(postgres_writer_engine) as admin:
        handoff = admin.get(OkxDemoCanaryConsentHandoff, requested.handoff_id)
        assert handoff.status == "EXPIRED"
        assert handoff.failure_code == "KEY_HARDENING_REAUTHORIZATION_REQUIRED"
        assert handoff.approval_id is None
        assert handoff.grant_id is None
        assert admin.query(OkxDemoSubmissionGrant).count() == 0

    harden_operator_consent_access_boundary(postgres_writer_engine)
    assert verify_schema(postgres_writer_engine).ready is True


def test_postgresql_v28_terminal_history_real_atomic_dump_restore_and_reharden(
    postgres_writer_engine, monkeypatch, tmp_path
) -> None:
    _now, handoff_id, grant_id, _approval_id = _finalize_and_arm_v28_handoff(
        postgres_writer_engine,
        monkeypatch,
        key="issue-627-real-restore-terminal-history",
    )
    with Session(postgres_writer_engine) as runtime:
        runtime.execute(text("SET LOCAL ROLE freqtrade"))
        assert fail_canary_grant_before_prepare(runtime, grant_id=grant_id) is True
        runtime.commit()
    requested_id = uuid4().hex
    requested_now = datetime.now(timezone.utc)
    with Session(postgres_writer_engine) as admin:
        admin.add(OkxDemoCanaryConsentHandoff(
            handoff_id=requested_id,
            execution_target_id="OKX_DEMO",
            source_job_id=21,
            source_ancestry=list(range(15, 22)),
            source_fingerprint=canonical_digest({"restore-source": requested_id}),
            idempotency_key_digest=canonical_digest({"restore-key": requested_id}),
            consent_nonce=canonical_digest({"restore-nonce": requested_id}),
            consent_payload_digest=canonical_digest({"restore-payload": requested_id}),
            consent_digest=canonical_digest({"restore-consent": requested_id}),
            provenance=CANARY_PROVENANCE,
            instrument_id="BTC-USDT-SWAP",
            max_notional=Decimal("20"),
            status="REQUESTED",
            snapshot_binding={},
            consented_at=requested_now,
            consent_deadline_at=requested_now + timedelta(minutes=1),
            created_at=requested_now,
            updated_at=requested_now,
        ))
        admin.commit()

    source_url = postgres_writer_engine.url.render_as_string(hide_password=False)
    monkeypatch.setattr(
        postgres_backup, "peer_admin_database_url", lambda database_url: database_url
    )
    backup_path, manifest_path = postgres_backup.create_backup(
        database_url=source_url,
        output_dir=tmp_path,
        pg_dump_binary="/opt/homebrew/bin/pg_dump",
    )

    destination_name = "test_okx_restore_{}".format(uuid4().hex)
    admin_url = make_url(source_url).set(database="postgres")
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    destination_engine = None
    try:
        with admin_engine.begin() as admin:
            admin.execute(text('CREATE DATABASE "{}" TEMPLATE template0'.format(
                destination_name
            )))
        destination_engine = create_engine(
            make_url(source_url).set(database=destination_name)
        )
        assert upgrade_database(destination_engine) == SCHEMA_VERSION
        destination_url = destination_engine.url.render_as_string(
            hide_password=False
        )
        with destination_engine.begin() as destination:
            table_names = [
                name for name in inspect(destination).get_table_names()
                if name != VERSION_TABLE
            ]
            quoted = ",".join(
                destination.dialect.identifier_preparer.quote(name)
                for name in table_names
            )
            destination.exec_driver_sql("TRUNCATE {} CASCADE".format(quoted))

        for secret_table in (
            "okx_demo_attestation_secrets",
            "okx_demo_operator_consent_secrets",
        ):
            with destination_engine.begin() as destination:
                destination.execute(text(
                    "INSERT INTO {}(secret_id,hmac_key) "
                    "VALUES('ACTIVE',decode(repeat('01',32),'hex'))".format(
                        secret_table
                    )
                ))
            with pytest.raises(
                postgres_backup.BackupBlocked,
                match="restore target contains managed data: {}".format(
                    secret_table
                ),
            ):
                postgres_backup.restore_backup(
                    database_url=destination_url,
                    backup_path=backup_path,
                    manifest_path=manifest_path,
                    psql_binary="/opt/homebrew/bin/psql",
                )
            with destination_engine.begin() as destination:
                destination.execute(text("DELETE FROM {}".format(secret_table)))

        with destination_engine.begin() as destination:
            destination.execute(text(
                "INSERT INTO research_worker_control(id,paused,updated_at) "
                "VALUES(1,FALSE,clock_timestamp())"
            ))
        with pytest.raises(
            postgres_backup.BackupBlocked,
            match="restore target contains managed data",
        ):
            postgres_backup.restore_backup(
                database_url=destination_url,
                backup_path=backup_path,
                manifest_path=manifest_path,
                psql_binary="/opt/homebrew/bin/psql",
            )
        with destination_engine.begin() as destination:
            assert destination.execute(text(
                "SELECT count(*) FROM research_worker_control"
            )).scalar_one() == 1
            destination.execute(text("DELETE FROM research_worker_control"))
            destination.execute(text(
                "UPDATE freqtrade_ai_schema_migrations SET version='wrong-version'"
            ))
        with pytest.raises(
            postgres_backup.BackupBlocked,
            match="restore target is not an exact v28 schema",
        ):
            postgres_backup.restore_backup(
                database_url=destination_url,
                backup_path=backup_path,
                manifest_path=manifest_path,
                psql_binary="/opt/homebrew/bin/psql",
            )
        with destination_engine.begin() as destination:
            assert destination.execute(text(
                "SELECT version FROM freqtrade_ai_schema_migrations"
            )).scalar_one() == "wrong-version"
            destination.execute(text(
                "UPDATE freqtrade_ai_schema_migrations SET version=:version"
            ), {"version": SCHEMA_VERSION})

        delayed_backup_path = tmp_path / "delayed-restore.sql"
        delayed_backup_path.write_bytes(
            b"SELECT pg_sleep(2);\n" + backup_path.read_bytes()
        )
        delayed_manifest_path = tmp_path / "delayed-restore.manifest.json"
        delayed_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        delayed_manifest["backup_file"] = delayed_backup_path.name
        delayed_manifest["sha256"] = hashlib.sha256(
            delayed_backup_path.read_bytes()
        ).hexdigest()
        delayed_manifest_path.write_text(
            json.dumps(delayed_manifest), encoding="utf-8"
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            restore_future = executor.submit(
                postgres_backup.restore_backup,
                database_url=destination_url,
                backup_path=delayed_backup_path,
                manifest_path=delayed_manifest_path,
                psql_binary="/opt/homebrew/bin/psql",
            )
            lock_observed = False
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with destination_engine.connect() as observer:
                    lock_observed = observer.execute(text(
                        "SELECT EXISTS ("
                        "SELECT 1 FROM pg_locks l "
                        "JOIN pg_class c ON c.oid=l.relation "
                        "WHERE l.database=(SELECT oid FROM pg_database "
                        "WHERE datname=current_database()) "
                        "AND c.relname='research_worker_control' "
                        "AND l.mode='AccessExclusiveLock' AND l.granted "
                        "AND l.pid<>pg_backend_pid())"
                    )).scalar_one()
                if lock_observed:
                    break
                time.sleep(0.02)
            assert lock_observed is True
            with pytest.raises(SQLAlchemyError, match="statement timeout"):
                with destination_engine.begin() as concurrent_writer:
                    concurrent_writer.execute(text(
                        "SET LOCAL statement_timeout='250ms'"
                    ))
                    concurrent_writer.execute(text(
                        "INSERT INTO research_worker_control"
                        "(id,paused,updated_at) "
                        "VALUES(1,FALSE,clock_timestamp())"
                    ))
            restore_future.result(timeout=10)
        with Session(destination_engine) as restored:
            assert restored.get(
                OkxDemoCanaryConsentHandoff, handoff_id
            ).status == "FAILED"
            assert restored.get(OkxDemoSubmissionGrant, grant_id).status == "FAILED"
        with destination_engine.connect() as restored:
            assert restored.execute(text(
                "SELECT count(*) FROM okx_demo_operator_consent_secrets"
            )).scalar_one() == 0
        with Session(destination_engine) as runtime:
            runtime.execute(text("SET LOCAL ROLE freqtrade"))
            with pytest.raises(
                SQLAlchemyError,
                match="active operator consent proof key unavailable",
            ):
                runtime.execute(text("SELECT pending_okx_demo_canary_consent()"))
            runtime.rollback()

        # Secret-excluding restore always requires an explicit owner audit,
        # and a nonterminal restored request cannot be advanced or replayed.
        monkeypatch.setenv(OPERATOR_TOKEN_ENV, "synthetic-restored-operator-token")
        with pytest.raises(SchemaMigrationBlocked, match="active handoff"):
            harden_operator_consent_access_boundary(destination_engine)
        assert revoke_operator_consents_for_key_hardening(destination_engine) == 1
        harden_operator_consent_access_boundary(destination_engine)
        assert revoke_attested_sessions_for_key_hardening(destination_engine) > 0
        harden_attestation_access_boundary(destination_engine)
        assert verify_schema(destination_engine).ready is True
        with Session(destination_engine) as restored:
            assert restored.get(
                OkxDemoCanaryConsentHandoff, requested_id
            ).status == "EXPIRED"
            restored.execute(text("SET LOCAL ROLE freqtrade"))
            assert restored.execute(text(
                "SELECT pending_okx_demo_canary_consent()"
            )).scalar_one() is None
    finally:
        if destination_engine is not None:
            destination_engine.dispose()
        with admin_engine.begin() as admin:
            admin.execute(text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname=:database AND pid<>pg_backend_pid()"
            ), {"database": destination_name})
            admin.execute(text('DROP DATABASE IF EXISTS "{}"'.format(
                destination_name
            )))
        admin_engine.dispose()


def test_postgresql_v28_writer_failure_before_prepare_terminalizes_atomically(
    postgres_writer_engine, monkeypatch
) -> None:
    _now, handoff_id, grant_id, approval_id = _finalize_and_arm_v28_handoff(
        postgres_writer_engine,
        monkeypatch,
        key="issue-627-before-prepared-failure",
    )
    with Session(postgres_writer_engine) as runtime:
        runtime.execute(text("SET LOCAL ROLE freqtrade"))
        assert fail_canary_grant_before_prepare(runtime, grant_id=grant_id) is True
        runtime.commit()
    with Session(postgres_writer_engine) as admin:
        assert admin.get(OkxDemoSubmissionGrant, grant_id).status == "FAILED"
        handoff = admin.get(OkxDemoCanaryConsentHandoff, handoff_id)
        assert handoff.status == "FAILED"
        assert handoff.failure_code == "WRITER_FAILED_BEFORE_PREPARED"
        assert admin.get(ApprovedExecution, approval_id).status == "EXPIRED"
        reserved, positions = admin.execute(text(
            "SELECT COALESCE(sum(reserved_notional),0),"
            "COALESCE(sum(approved_positions),0) FROM risk_budgets "
            "WHERE execution_target_id='OKX_DEMO'"
        )).one()
        assert reserved == 0
        assert positions == 0


def test_postgresql_v28_prepared_restart_is_get_only_and_post_at_most_once(
    postgres_writer_engine, monkeypatch
) -> None:
    now, handoff_id, grant_id, approval_id = _finalize_and_arm_v28_handoff(
        postgres_writer_engine, monkeypatch, key="issue-627-prepared-restart"
    )
    instrument = InstrumentSpec(
        inst_id="BTC-USDT-SWAP", inst_type="SWAP", base_ccy="BTC",
        quote_ccy="USDT", settle_ccy="USDT", contract_type="linear",
        contract_value="0.0001", contract_value_ccy="BTC",
        lot_size="1", min_size="1", tick_size="0.1", state="live",
    )
    with Session(postgres_writer_engine) as session:
        grant = session.get(OkxDemoSubmissionGrant, grant_id)
        submit_now = max(now, grant.issued_at + timedelta(milliseconds=1))
        assert submit_now < grant.expires_at
        store = SqlAlchemyOrderWriterStore(
            session, now_provider=lambda: submit_now
        )
        claimed = store.load_approved_execution(approval_id)
        authorization = OrderSubmissionAuthorization(
            grant_id=grant_id, authorization_mode="ONE_SHOT",
            execution_target_id="OKX_DEMO", authorization_schema_version="RISK_V1",
            canonical_hash=claimed.canonical_hash, policy_digest=claimed.policy_digest,
            approved_payload_hash=claimed.approved_payload_hash,
            allow_real_funds=False, simulated_trading=True,
            order_submission_enabled=True, writer_instance_id="PreparedWriter627A",
            approval_id=approval_id, client_order_id=claimed.client_order_id,
            issued_at=grant.issued_at, expires_at=grant.expires_at,
        )
        command = normalize_order_command(
            claimed,
            submission_grant=authorization,
            instrument=instrument,
            now=submit_now,
        )
        store.acquire_lease(
            grant_id=grant_id, authorization_mode="ONE_SHOT",
            writer_instance_id=authorization.writer_instance_id,
            approval_id=approval_id, canonical_hash=claimed.canonical_hash,
            policy_digest=claimed.policy_digest,
            approved_payload_hash=claimed.approved_payload_hash,
            now=submit_now, expires_at=submit_now + timedelta(seconds=1),
        )
        order, prepared = store.prepare_place(
            command, operation="PLACE", operation_id=claimed.client_order_id,
            request_digest=hashlib.sha256(
                _canonical_json(command.request_body).encode()
            ).hexdigest(),
            safe_request_snapshot=command.request_body,
        )

    with Session(postgres_writer_engine) as runtime:
        runtime.execute(text("SET LOCAL ROLE freqtrade"))
        assert fail_canary_grant_before_prepare(runtime, grant_id=grant_id) is False
        runtime.commit()

    restart_now = submit_now + timedelta(seconds=2)
    post_calls = []

    class NotFoundRecovery:
        def order(self, inst_id, *, order_id=None, client_order_id=None):
            raise OkxReadAdapterError(
                kind="BUSINESS_ERROR",
                status="FAILED",
                message="OKX returned a read business error",
                okx_code="51603",
            )

    second_restart_now = restart_now + timedelta(seconds=11)

    class ReadOnlyRecovery:
        def order(self, inst_id, *, order_id=None, client_order_id=None):
            return OkxReadSnapshot(
                metadata=SnapshotMetadata(
                    resource="order", fetched_at=second_restart_now,
                    expires_at=second_restart_now + timedelta(seconds=30),
                    stale=False, authenticated=True,
                ),
                items=[{
                    "inst_id": inst_id, "order_id": "demo-recovered-627",
                    "client_order_id": client_order_id, "state": "live",
                    "side": "buy", "position_side": "long",
                    "margin_mode": "isolated", "order_type": "limit",
                    "reduce_only": False, "price": Decimal("57000"),
                    "size": Decimal("1"),
                }],
            )

    class NoPostTransport:
        def post(self, **kwargs):
            post_calls.append(kwargs)
            raise AssertionError("restart recovery must never POST")

    with Session(postgres_writer_engine) as session:
        restarted_store = SqlAlchemyOrderWriterStore(
            session, now_provider=lambda: restart_now
        )
        result = OkxDemoOrderWriter(
            read_client=NotFoundRecovery(), write_transport=NoPostTransport(),
            store=restarted_store, now_provider=lambda: restart_now,
        ).reconcile_unresolved(prepared.attempt_id)
        assert result.status == "RECOVERY_REQUIRED"
    with Session(postgres_writer_engine) as admin:
        attempt = admin.get(OkxOrderWriteAttempt, prepared.attempt_id)
        assert attempt.state == "RECOVERY_REQUIRED"
        assert attempt.reason_code == "EXACT_ORDER_NOT_FOUND"
        assert attempt.safe_response_snapshot == {
            "exact_order_get": "NOT_FOUND",
            "okx_code": "51603",
        }

    with Session(postgres_writer_engine) as session:
        restarted_store = SqlAlchemyOrderWriterStore(
            session, now_provider=lambda: second_restart_now
        )
        result = OkxDemoOrderWriter(
            read_client=ReadOnlyRecovery(), write_transport=NoPostTransport(),
            store=restarted_store, now_provider=lambda: second_restart_now,
        ).reconcile_unresolved(prepared.attempt_id)
        assert result.status == "RECONCILED"
    with Session(postgres_writer_engine) as admin:
        assert post_calls == []
        assert admin.get(OkxDemoSubmissionGrant, grant_id).status == "CONSUMED"
        assert admin.get(OkxDemoCanaryConsentHandoff, handoff_id).status == "CONSUMED"
        attempt = admin.get(OkxOrderWriteAttempt, prepared.attempt_id)
        assert attempt.attempt_count == 1
        assert attempt.state == "RECONCILED"
        assert admin.query(OkxOrderWriteAttempt).filter_by(
            approval_id=approval_id, operation="PLACE"
        ).count() == 1


def test_postgresql_v32_owner_accepts_exact_not_found_once_and_allows_one_successor(
    postgres_writer_engine, monkeypatch
) -> None:
    upgrade_database(postgres_writer_engine)
    now, run_id, predecessor, current, read_type = _prepare_v31_atomic_successor(
        postgres_writer_engine, monkeypatch, suffix="accepted-not-found"
    )
    runtime_id = "RuntimeV32AcceptedNotFound"
    with postgres_writer_engine.connect() as connection, Session(bind=connection) as runtime:
        runtime.execute(text("SET LOCAL ROLE freqtrade"))
        store = SqlAlchemyOrderWriterStore(runtime)
        assert acquire_one_shot_runtime_lock(runtime) is True
        finalized = process_pending_canary_consent_handoff(
            read_client=read_type(30), db=runtime,
            runtime_instance_id=runtime_id,
            fresh_reconciliation=lambda: {"reconciliation_run_id": run_id},
            safety_check=lambda: True,
            atomic_holder_token_digest=store.atomic_process_token_digest,
            now=now,
        )
        assert finalized is not None and finalized.atomic_receipt is not None
        prepared = SimpleNamespace(**dict(finalized.atomic_receipt))
        claimed = runtime.execute(text(
            "SELECT claim_atomic_okx_demo_canary_dispatch("
            ":attempt,:runtime,:token,:generation,:digest)"
        ), {
            "attempt": prepared.attempt_id, "runtime": runtime_id,
            "token": store.atomic_process_token,
            "generation": prepared.lease_generation,
            "digest": prepared.request_digest,
        }).scalar_one()
        assert claimed is not None
        runtime.commit()
        assert release_one_shot_runtime_lock(runtime) is True
        runtime.commit()
    handoff_id = current.handoff_id
    submit_now = datetime.now(timezone.utc)
    with Session(postgres_writer_engine) as admin:
        handoff = admin.get(OkxDemoCanaryConsentHandoff, handoff_id)
        assert handoff.supersedes_handoff_id == predecessor.handoff_id
        grant_id = handoff.grant_id
        approval_id = handoff.approval_id

    class NotFoundRecovery:
        def order(self, inst_id, *, order_id=None, client_order_id=None):
            raise OkxReadAdapterError(
                kind="BUSINESS_ERROR", status="FAILED",
                message="OKX returned a read business error", okx_code="51603",
            )

    class NoPostTransport:
        def post(self, **_kwargs):
            raise AssertionError("accepted NOT_FOUND recovery must never POST")

    with postgres_writer_engine.begin() as admin:
        admin.execute(text(
            "UPDATE okx_order_writer_leases SET "
            "acquired_at=clock_timestamp()-interval '3 seconds',"
            "heartbeat_at=clock_timestamp()-interval '2 seconds',"
            "expires_at=clock_timestamp()-interval '1 second' "
            "WHERE execution_target_id='OKX_DEMO'"
        ))
    restart_now = submit_now + timedelta(seconds=2)
    with Session(postgres_writer_engine) as session:
        result = OkxDemoOrderWriter(
            read_client=NotFoundRecovery(), write_transport=NoPostTransport(),
            store=SqlAlchemyOrderWriterStore(
                session, now_provider=lambda: restart_now
            ), now_provider=lambda: restart_now,
        ).reconcile_unresolved(prepared.attempt_id)
        assert result.status == "RECOVERY_REQUIRED"

    with postgres_writer_engine.connect() as admin:
        lease_expires_at = admin.execute(text(
            "SELECT expires_at FROM okx_order_writer_leases "
            "WHERE execution_target_id='OKX_DEMO'"
        )).scalar_one()
        database_now = admin.execute(text("SELECT clock_timestamp()" )).scalar_one()
    time.sleep(max(0.0, (lease_expires_at - database_now).total_seconds()) + 0.1)
    with postgres_writer_engine.begin() as admin:
        lifecycle_id, fencing_version = admin.execute(text(
            "SELECT lifecycle_id,fencing_version FROM okx_demo_canary_lifecycles "
            "WHERE opening_exchange_order_row_id=:order_id"
        ), {"order_id": prepared.exchange_order_row_id}).one()
        for statement, parameters in (
            (
                "UPDATE okx_demo_canary_consent_handoffs SET consent_deadline_at="
                "clock_timestamp()-interval '1 second',consented_at="
                "clock_timestamp()-interval '2 seconds',bundle_expires_at="
                "clock_timestamp()-interval '1 second' WHERE handoff_id=:handoff",
                {"handoff": handoff_id},
            ),
            (
                "UPDATE okx_demo_canary_lifecycles SET deadline_at="
                "clock_timestamp()-interval '1 second' WHERE lifecycle_id=:lifecycle",
                {"lifecycle": lifecycle_id},
            ),
            (
                "UPDATE okx_order_write_attempts SET dispatch_not_after="
                "clock_timestamp()-interval '1 second' WHERE id=:attempt",
                {"attempt": prepared.attempt_id},
            ),
        ):
            admin.execute(text(statement), parameters)
        evidence = {
            "absolute_submission_claim": False,
            "attempt_count": 1,
            "client_order_id": prepared.client_order_id,
            "exchange_result_code": "51603",
            "exchange_result_state": "NOT_FOUND",
            "fill_count": 0,
            "instrument_id": "BTC-USDT-SWAP",
            "query_kind": "exact_get",
            "request_digest": prepared.request_digest,
            "restart_resubmission_count": 0,
        }
        observed_at = admin.execute(text(
            "SELECT last_attempt_at FROM okx_order_write_attempts WHERE id=:attempt"
        ), {"attempt": prepared.attempt_id}).scalar_one()
        evidence_digest = admin.execute(text(
            "SELECT encode(digest(convert_to(canonical_jsonb_text(CAST(:evidence AS jsonb)),"
            "'UTF8'),'sha256'),'hex')"
        ), {"evidence": json.dumps(evidence)}).scalar_one()
        acceptance_digest = admin.execute(text(
            "SELECT encode(digest(convert_to(canonical_jsonb_text(jsonb_build_object("
            "'acceptance_kind','USER_ACCEPTED_NOT_FOUND_NO_FILL_V1',"
            "'absolute_submission_claim',false,'attempt_id',CAST(:attempt AS bigint),"
            "'evidence_digest',CAST(:evidence_digest AS text),'evidence_observed_at',"
            "CAST(:observed_at AS timestamptz),'exchange_order_row_id',"
            "CAST(:order_id AS bigint),'lifecycle_id',CAST(:lifecycle AS text),"
            "'predecessor_grant_id',CAST(:grant AS text),'predecessor_handoff_id',"
            "CAST(:handoff AS text),"
            "'request_digest',CAST(:request_digest AS text),"
            "'source_job_id',22)),'UTF8'),'sha256'),'hex')"
        ), {
            "attempt": prepared.attempt_id,
            "evidence_digest": evidence_digest, "observed_at": observed_at,
            "order_id": prepared.exchange_order_row_id, "lifecycle": lifecycle_id,
            "grant": grant_id, "handoff": handoff_id,
            "request_digest": prepared.request_digest,
        }).scalar_one()
        payload = {
            "acceptance_digest": acceptance_digest,
            "attempt_id": prepared.attempt_id,
            "evidence_digest": evidence_digest,
            "evidence_observed_at": observed_at.isoformat(),
            "evidence_snapshot": evidence,
            "expected_fencing_version": fencing_version,
            "lifecycle_id": lifecycle_id,
        }
        preconditions = admin.execute(text(
            "SELECT jsonb_build_object("
            "'attempt',(a.operation='PLACE' AND a.state='RECOVERY_REQUIRED' AND "
            "a.reason_code='EXACT_ORDER_NOT_FOUND' AND a.attempt_count=1),"
            "'chain',(h.source_job_id=22 AND h.supersedes_handoff_id IS NOT NULL AND "
            "h.status='CONSUMED' AND g.status='CONSUMED' AND EXISTS(SELECT 1 FROM "
            "okx_demo_canary_consent_handoffs original WHERE "
            "original.handoff_id=h.supersedes_handoff_id AND "
            "original.supersedes_handoff_id IS NULL AND original.status='EXPIRED')),"
            "'lifecycle',(l.cleanup_phase='OPENING_SUBMITTED' AND l.outcome='PENDING' "
            "AND l.attributed_fill_quantity=0 AND l.fencing_version=:fencing),"
            "'handoff_expiry',(h.consent_deadline_at<clock_timestamp() AND "
            "h.bundle_expires_at<clock_timestamp()),"
            "'grant_expiry',g.expires_at<clock_timestamp(),"
            "'lifecycle_expiry',l.deadline_at<clock_timestamp(),"
            "'lease_expiry',lease.expires_at<clock_timestamp(),"
            "'counts',((SELECT count(*) FROM okx_order_write_attempts)=1 AND "
            "(SELECT count(*) FROM exchange_orders)=1 AND "
            "(SELECT count(*) FROM exchange_fills)=0)) "
            "FROM okx_order_write_attempts a JOIN exchange_orders o ON o.id=a.exchange_order_row_id "
            "JOIN okx_demo_canary_lifecycles l ON l.opening_exchange_order_row_id=o.id "
            "JOIN okx_demo_submission_grants g ON g.grant_id=l.submission_grant_id "
            "JOIN okx_demo_canary_consent_handoffs h ON h.grant_id=g.grant_id "
            "JOIN okx_order_writer_leases lease ON lease.execution_target_id='OKX_DEMO' "
            "WHERE a.id=:attempt"
        ), {"attempt": prepared.attempt_id, "fencing": fencing_version}).scalar_one()
        assert [key for key, value in preconditions.items() if not value] == []

    with pytest.raises(SQLAlchemyError, match="invalid accepted NOT_FOUND attempt transition"):
        with postgres_writer_engine.begin() as runtime:
            runtime.execute(text("SET LOCAL ROLE freqtrade"))
            runtime.execute(text(
                "UPDATE okx_order_write_attempts SET "
                "state='USER_ACCEPTED_NOT_FOUND_NO_FILL',"
                "order_state='USER_ACCEPTED_NOT_FOUND_NO_FILL' WHERE id=:attempt"
            ), {"attempt": prepared.attempt_id})

    with pytest.raises(SQLAlchemyError, match="invalid accepted NOT_FOUND exchange order transition"):
        with postgres_writer_engine.begin() as runtime:
            runtime.execute(text("SET LOCAL ROLE freqtrade"))
            runtime.execute(text(
                "UPDATE exchange_orders SET status='USER_ACCEPTED_NOT_FOUND_NO_FILL' "
                "WHERE id=:order_id"
            ), {"order_id": prepared.exchange_order_row_id})

    mismatched_time_payload = dict(payload)
    mismatched_time_payload["evidence_observed_at"] = (
        observed_at + timedelta(microseconds=1)
    ).isoformat()
    with pytest.raises(SQLAlchemyError, match="terminalization precondition mismatch"):
        with postgres_writer_engine.begin() as owner:
            owner.execute(text(
                "SELECT terminalize_accepted_not_found_no_fill(CAST(:payload AS jsonb))"
            ), {"payload": json.dumps(mismatched_time_payload)})

    with pytest.raises(SQLAlchemyError, match="terminalization precondition mismatch"):
        with postgres_writer_engine.begin() as owner:
            owner.execute(text(
                "UPDATE okx_order_write_attempts SET safe_response_snapshot='{}'::json "
                "WHERE id=:attempt"
            ), {"attempt": prepared.attempt_id})
            owner.execute(text(
                "SELECT terminalize_accepted_not_found_no_fill(CAST(:payload AS jsonb))"
            ), {"payload": json.dumps(payload)})

    future_observed_at = observed_at + timedelta(days=1)
    future_payload = dict(payload)
    future_payload["evidence_observed_at"] = future_observed_at.isoformat()
    with pytest.raises(SQLAlchemyError, match="terminalization precondition mismatch"):
        with postgres_writer_engine.begin() as owner:
            owner.execute(text(
                "UPDATE okx_order_write_attempts SET last_attempt_at=:future "
                "WHERE id=:attempt"
            ), {"future": future_observed_at, "attempt": prepared.attempt_id})
            owner.execute(text(
                "SELECT terminalize_accepted_not_found_no_fill(CAST(:payload AS jsonb))"
            ), {"payload": json.dumps(future_payload)})

    with postgres_writer_engine.connect() as admin:
        assert admin.execute(text(
            "SELECT count(*) FROM okx_demo_accepted_not_found_terminalizations"
        )).scalar_one() == 0

    # The production owner action runs only after the canonical runtime is
    # stopped.  Close pooled test connections too, releasing session locks.
    postgres_writer_engine.dispose()

    def terminalize(_index: int):
        with postgres_writer_engine.begin() as owner:
            return owner.execute(text(
                "SELECT terminalize_accepted_not_found_no_fill(CAST(:payload AS jsonb))"
            ), {"payload": json.dumps(payload)}).scalar_one()

    with ThreadPoolExecutor(max_workers=2) as executor:
        receipts = list(executor.map(terminalize, (1, 2)))
    assert {int(item["terminalization_id"]) for item in receipts} == {1}
    assert sorted(bool(item["idempotent"]) for item in receipts) == [False, True]

    with Session(postgres_writer_engine) as admin:
        attempt = admin.get(OkxOrderWriteAttempt, prepared.attempt_id)
        lifecycle = admin.get(OkxDemoCanaryLifecycle, lifecycle_id)
        assert attempt.state == "USER_ACCEPTED_NOT_FOUND_NO_FILL"
        assert attempt.attempt_count == 1
        assert attempt.request_digest == prepared.request_digest
        assert lifecycle.cleanup_phase == "TERMINAL"
        assert lifecycle.outcome == "FAILED"
        assert lifecycle.final_reconciliation_run_id is None
        assert lifecycle.final_evidence_digest == acceptance_digest
        assert admin.get(ApprovedExecution, approval_id).status == "EXPIRED"
        assert admin.execute(text(
            "SELECT status FROM full_chain_runs WHERE id=(SELECT full_chain_run_id "
            "FROM okx_demo_canary_consent_handoffs WHERE handoff_id=:handoff)"
        ), {"handoff": handoff_id}).scalar_one() == "BLOCKED"
        assert admin.query(ExchangeFill).count() == 0
        assert admin.execute(text(
            "SELECT request_digest FROM okx_demo_accepted_not_found_terminalizations"
        )).scalar_one() == prepared.request_digest
        assert admin.execute(text(
            "SELECT has_function_privilege('freqtrade',"
            "'terminalize_accepted_not_found_no_fill(jsonb)','EXECUTE')"
        )).scalar_one() is False
        assert admin.execute(text(
            "SELECT has_table_privilege('freqtrade',"
            "'okx_demo_accepted_not_found_terminalizations','INSERT')"
        )).scalar_one() is False

    with Session(postgres_writer_engine) as runtime:
        runtime.execute(text("SET LOCAL ROLE freqtrade"))
        successor = OkxDemoCanaryPreparationService(
            runtime
        ).request_final_attestation_consent(
            idempotency_key="issue-627-v32-only-successor",
            operator_token="synthetic-test-operator-token",
        )
        assert successor.operation_status == "REQUESTED"

    successor_runtime_id = "RuntimeV32AcceptedSuccessor"
    with postgres_writer_engine.connect() as connection, Session(bind=connection) as runtime:
        runtime.execute(text("SET LOCAL ROLE freqtrade"))
        store = SqlAlchemyOrderWriterStore(runtime)
        assert acquire_one_shot_runtime_lock(runtime) is True
        successor_prepared = process_pending_canary_consent_handoff(
            read_client=read_type(30), db=runtime,
            runtime_instance_id=successor_runtime_id,
            fresh_reconciliation=lambda: {"reconciliation_run_id": run_id},
            safety_check=lambda: True,
            atomic_holder_token_digest=store.atomic_process_token_digest,
            now=datetime.now(timezone.utc),
        )
        assert successor_prepared is not None
        assert successor_prepared.atomic_receipt is not None
        second_prepared = SimpleNamespace(**dict(successor_prepared.atomic_receipt))
        assert runtime.execute(text(
            "SELECT claim_atomic_okx_demo_canary_dispatch("
            ":attempt,:runtime,:token,:generation,:digest)"
        ), {
            "attempt": second_prepared.attempt_id,
            "runtime": successor_runtime_id,
            "token": store.atomic_process_token,
            "generation": second_prepared.lease_generation,
            "digest": second_prepared.request_digest,
        }).scalar_one() is not None
        runtime.commit()
        assert release_one_shot_runtime_lock(runtime) is True
        runtime.commit()

    with Session(postgres_writer_engine) as runtime:
        runtime.execute(text("SET LOCAL ROLE freqtrade"))
        with pytest.raises(
            OkxDemoCanaryPreparationBlocked,
            match="accepted terminalization successor is unavailable",
        ):
            OkxDemoCanaryPreparationService(runtime).request_final_attestation_consent(
                idempotency_key="issue-627-v32-forbidden-third",
                operator_token="synthetic-test-operator-token",
            )
    with Session(postgres_writer_engine) as admin:
        successor_row = admin.get(OkxDemoCanaryConsentHandoff, successor.handoff_id)
        assert successor_row.supersedes_handoff_id == handoff_id
        assert successor_row.terminal_receipt_id == 1
        assert successor_row.status == "CONSUMED"
        assert admin.query(OkxOrderWriteAttempt).count() == 2
        assert admin.query(OkxOrderWriteAttempt).filter_by(
            state="DISPATCHED"
        ).count() == 1
        assert admin.query(ExchangeFill).count() == 0
        assert admin.execute(text(
            "SELECT eligible_atomic_okx_demo_canary_predecessor()"
        )).scalar_one() is None

    # The already-created receipt-bound successor is reconciled by exact GET
    # only.  No POST is permitted while producing the fixed depth-2 receipt.
    with postgres_writer_engine.begin() as admin:
        admin.execute(text(
            "UPDATE okx_order_writer_leases SET acquired_at=clock_timestamp()-interval '3 seconds',"
            "heartbeat_at=clock_timestamp()-interval '2 seconds',"
            "expires_at=clock_timestamp()-interval '1 second'"
        ))

    class SecondNotFound:
        def order(self, inst_id, *, order_id=None, client_order_id=None):
            raise OkxReadAdapterError(
                kind="BUSINESS_ERROR", status="FAILED", message="not found",
                okx_code="51603",
            )

    with Session(postgres_writer_engine) as runtime:
        result = OkxDemoOrderWriter(
            read_client=SecondNotFound(),
            write_transport=SimpleNamespace(post=lambda **_: pytest.fail("POST forbidden")),
            store=SqlAlchemyOrderWriterStore(runtime),
        ).reconcile_unresolved(second_prepared.attempt_id)
        assert result.status == "RECOVERY_REQUIRED"

    # Recreate the exact deployed v32 shape: immutable R1 plus its consumed,
    # unresolved C attempt.  Upgrading it must not rewrite either fact.
    with postgres_writer_engine.connect() as admin:
        before_v33 = admin.execute(text(
            "SELECT jsonb_build_object("
            "'attempt_state',a.state,'reason_code',a.reason_code,"
            "'attempt_count',a.attempt_count,'safe_response',a.safe_response_snapshot::jsonb,"
            "'receipt_digest',r.acceptance_digest,'receipt_evidence',r.evidence_snapshot::jsonb) "
            "FROM okx_order_write_attempts a CROSS JOIN "
            "okx_demo_accepted_not_found_terminalizations r WHERE a.id=:attempt"
        ), {"attempt": second_prepared.attempt_id}).scalar_one()
    with postgres_writer_engine.begin() as admin:
        admin.execute(text(
            "DROP TRIGGER IF EXISTS okx_demo_canary_bounded_accepted_successor_guard "
            "ON okx_demo_canary_consent_handoffs"
        ))
        for signature in (
            "terminalize_second_accepted_not_found_no_fill(jsonb)",
            "guard_bounded_accepted_successor_handoff()",
            "exact_bounded_accepted_not_found_predecessor(text)",
        ):
            admin.execute(text("DROP FUNCTION IF EXISTS {}".format(signature)))
        admin.execute(text(
            "ALTER TABLE okx_demo_accepted_not_found_terminalizations "
            "DROP COLUMN IF EXISTS parent_terminal_receipt_id CASCADE,"
            "DROP COLUMN IF EXISTS receipt_depth CASCADE"
        ))
        admin.execute(text("DELETE FROM {}".format(VERSION_TABLE)))
        admin.execute(text(
            "INSERT INTO {}(version) VALUES(:version)".format(VERSION_TABLE)
        ), {"version": BOUNDED_SECOND_ACCEPTANCE_BASE_VERSION})
    assert upgrade_database(postgres_writer_engine) == SCHEMA_VERSION
    with postgres_writer_engine.connect() as admin:
        after_v33 = admin.execute(text(
            "SELECT jsonb_build_object("
            "'attempt_state',a.state,'reason_code',a.reason_code,"
            "'attempt_count',a.attempt_count,'safe_response',a.safe_response_snapshot::jsonb,"
            "'receipt_digest',r.acceptance_digest,'receipt_evidence',r.evidence_snapshot::jsonb) "
            "FROM okx_order_write_attempts a CROSS JOIN "
            "okx_demo_accepted_not_found_terminalizations r WHERE a.id=:attempt"
        ), {"attempt": second_prepared.attempt_id}).scalar_one()
        assert after_v33 == before_v33
        assert admin.execute(text(
            "SELECT receipt_depth=1 AND parent_terminal_receipt_id IS NULL "
            "FROM okx_demo_accepted_not_found_terminalizations"
        )).scalar_one() is True
        assert admin.execute(text(
            "SELECT okx_demo_canary_consent_eligibility()->>'eligibility_state'"
        )).scalar_one() == "BLOCKED"

    with postgres_writer_engine.connect() as admin:
        terminalization_not_before, database_now = admin.execute(text(
            "SELECT GREATEST(g.expires_at,lease.expires_at),clock_timestamp() "
            "FROM okx_demo_submission_grants g "
            "JOIN okx_order_write_attempts a ON a.approval_id=g.approval_id "
            "JOIN okx_order_writer_leases lease ON lease.execution_target_id='OKX_DEMO' "
            "WHERE a.id=:attempt"
        ), {"attempt": second_prepared.attempt_id}).one()
    time.sleep(
        max(0.0, (terminalization_not_before - database_now).total_seconds())
        + 0.1
    )

    with postgres_writer_engine.begin() as admin:
        second_lifecycle_id, second_fencing, second_grant_id = admin.execute(text(
            "SELECT l.lifecycle_id,l.fencing_version,l.submission_grant_id "
            "FROM okx_demo_canary_lifecycles l "
            "WHERE l.opening_exchange_order_row_id=:order_id"
        ), {"order_id": second_prepared.exchange_order_row_id}).one()
        admin.execute(text(
            "UPDATE okx_demo_canary_consent_handoffs SET consent_deadline_at="
            "clock_timestamp()-interval '1 second',consented_at="
            "clock_timestamp()-interval '2 seconds',bundle_expires_at="
            "clock_timestamp()-interval '1 second' WHERE handoff_id=:handoff"
        ), {"handoff": successor.handoff_id})
        admin.execute(text(
            "UPDATE okx_demo_canary_lifecycles SET deadline_at="
            "created_at+interval '1 microsecond' WHERE lifecycle_id=:lifecycle"
        ), {"lifecycle": second_lifecycle_id})
        admin.execute(text(
            "UPDATE okx_order_write_attempts SET dispatch_not_after="
            "clock_timestamp()-interval '1 second' WHERE id=:attempt"
        ), {"attempt": second_prepared.attempt_id})
        second_observed_at = admin.execute(text(
            "SELECT last_attempt_at FROM okx_order_write_attempts WHERE id=:attempt"
        ), {"attempt": second_prepared.attempt_id}).scalar_one()
        second_evidence = {
            "absolute_submission_claim": False,
            "attempt_count": 1,
            "client_order_id": second_prepared.client_order_id,
            "exchange_result_code": "51603",
            "exchange_result_state": "NOT_FOUND",
            "fill_count": 0,
            "instrument_id": "BTC-USDT-SWAP",
            "query_kind": "exact_get",
            "request_digest": second_prepared.request_digest,
            "restart_resubmission_count": 0,
        }
        second_evidence_digest = admin.execute(text(
            "SELECT encode(digest(convert_to(canonical_jsonb_text(CAST(:evidence AS jsonb)),"
            "'UTF8'),'sha256'),'hex')"
        ), {"evidence": json.dumps(second_evidence)}).scalar_one()
        second_acceptance_digest = admin.execute(text(
            "SELECT encode(digest(convert_to(canonical_jsonb_text(jsonb_build_object("
            "'acceptance_kind','USER_ACCEPTED_NOT_FOUND_NO_FILL_V2',"
            "'absolute_submission_claim',false,'attempt_id',CAST(:attempt AS bigint),"
            "'evidence_digest',CAST(:evidence_digest AS text),'evidence_observed_at',"
            "CAST(:observed_at AS timestamptz),'exchange_order_row_id',"
            "CAST(:order_id AS bigint),'lifecycle_id',CAST(:lifecycle AS text),"
            "'parent_terminal_receipt_id',1,'predecessor_grant_id',CAST(:grant AS text),"
            "'predecessor_handoff_id',CAST(:handoff AS text),'receipt_depth',2,"
            "'request_digest',CAST(:request_digest AS text),'source_job_id',22)),"
            "'UTF8'),'sha256'),'hex')"
        ), {
            "attempt": second_prepared.attempt_id,
            "evidence_digest": second_evidence_digest,
            "observed_at": second_observed_at,
            "order_id": second_prepared.exchange_order_row_id,
            "lifecycle": second_lifecycle_id,
            "grant": second_grant_id,
            "handoff": successor.handoff_id,
            "request_digest": second_prepared.request_digest,
        }).scalar_one()
        second_payload = {
            "acceptance_digest": second_acceptance_digest,
            "attempt_id": second_prepared.attempt_id,
            "evidence_digest": second_evidence_digest,
            "evidence_observed_at": second_observed_at.isoformat(),
            "evidence_snapshot": second_evidence,
            "expected_fencing_version": second_fencing,
            "lifecycle_id": second_lifecycle_id,
            "parent_terminal_receipt_id": 1,
        }

    postgres_writer_engine.dispose()

    def terminalize_second(_index: int):
        with postgres_writer_engine.begin() as owner:
            return owner.execute(text(
                "SELECT terminalize_second_accepted_not_found_no_fill("
                "CAST(:payload AS jsonb))"
            ), {"payload": json.dumps(second_payload)}).scalar_one()

    with ThreadPoolExecutor(max_workers=2) as executor:
        second_receipts = list(executor.map(terminalize_second, (1, 2)))
    assert {int(item["terminalization_id"]) for item in second_receipts} == {2}
    assert sorted(bool(item["idempotent"]) for item in second_receipts) == [False, True]
    assert all(item["absolute_submission_claim"] is False for item in second_receipts)

    with pytest.raises(SQLAlchemyError, match="identity drift"):
        drifted = dict(second_payload)
        drifted["acceptance_digest"] = "f" * 64
        with postgres_writer_engine.begin() as owner:
            owner.execute(text(
                "SELECT terminalize_second_accepted_not_found_no_fill("
                "CAST(:payload AS jsonb))"
            ), {"payload": json.dumps(drifted)})

    with pytest.raises(SQLAlchemyError):
        with postgres_writer_engine.begin() as runtime:
            runtime.execute(text("SET LOCAL ROLE freqtrade"))
            runtime.execute(text(
                "SELECT terminalize_second_accepted_not_found_no_fill("
                "CAST(:payload AS jsonb))"
            ), {"payload": json.dumps(second_payload)})

    with Session(postgres_writer_engine) as runtime:
        runtime.execute(text("SET LOCAL ROLE freqtrade"))
        final_successor = OkxDemoCanaryPreparationService(
            runtime
        ).request_final_attestation_consent(
            idempotency_key="issue-627-v33-final-successor",
            operator_token="synthetic-test-operator-token",
        )
        assert final_successor.operation_status == "REQUESTED"

    with Session(postgres_writer_engine) as runtime:
        runtime.execute(text("SET LOCAL ROLE freqtrade"))
        with pytest.raises(
            OkxDemoCanaryPreparationBlocked,
            match="accepted terminalization successor is unavailable",
        ):
            OkxDemoCanaryPreparationService(runtime).request_final_attestation_consent(
                idempotency_key="issue-627-v33-forbidden-depth-three",
                operator_token="synthetic-test-operator-token",
            )

    with postgres_writer_engine.connect() as admin:
        rows = admin.execute(text(
            "SELECT receipt_depth,parent_terminal_receipt_id,absolute_submission_claim "
            "FROM okx_demo_accepted_not_found_terminalizations ORDER BY receipt_depth"
        )).all()
        assert rows == [(1, None, False), (2, 1, False)]
        assert admin.execute(text(
            "SELECT count(*) FROM okx_demo_canary_consent_handoffs "
            "WHERE terminal_receipt_id=2"
        )).scalar_one() == 1

    final_runtime_id = "RuntimeV34FinalAcceptedSuccessor"
    with postgres_writer_engine.connect() as connection, Session(bind=connection) as runtime:
        runtime.execute(text("SET LOCAL ROLE freqtrade"))
        store = SqlAlchemyOrderWriterStore(runtime)
        assert acquire_one_shot_runtime_lock(runtime) is True
        final_prepared_result = process_pending_canary_consent_handoff(
            read_client=read_type(30), db=runtime,
            runtime_instance_id=final_runtime_id,
            fresh_reconciliation=lambda: {"reconciliation_run_id": run_id},
            safety_check=lambda: True,
            atomic_holder_token_digest=store.atomic_process_token_digest,
            now=datetime.now(timezone.utc),
        )
        assert final_prepared_result is not None
        assert final_prepared_result.atomic_receipt is not None
        final_prepared = SimpleNamespace(**dict(final_prepared_result.atomic_receipt))
        assert runtime.execute(text(
            "SELECT claim_atomic_okx_demo_canary_dispatch("
            ":attempt,:runtime,:token,:generation,:digest)"
        ), {
            "attempt": final_prepared.attempt_id, "runtime": final_runtime_id,
            "token": store.atomic_process_token,
            "generation": final_prepared.lease_generation,
            "digest": final_prepared.request_digest,
        }).scalar_one() is not None
        runtime.commit()
        assert release_one_shot_runtime_lock(runtime) is True
        runtime.commit()

    with postgres_writer_engine.begin() as admin:
        admin.execute(text(
            "UPDATE okx_order_writer_leases SET acquired_at=clock_timestamp()-interval '3 seconds',"
            "heartbeat_at=clock_timestamp()-interval '2 seconds',"
            "expires_at=clock_timestamp()-interval '1 second'"
        ))
    with Session(postgres_writer_engine) as runtime:
        result = OkxDemoOrderWriter(
            read_client=SecondNotFound(),
            write_transport=SimpleNamespace(post=lambda **_: pytest.fail("POST forbidden")),
            store=SqlAlchemyOrderWriterStore(runtime),
        ).reconcile_unresolved(final_prepared.attempt_id)
        assert result.status == "RECOVERY_REQUIRED"

    with postgres_writer_engine.connect() as admin:
        final_not_before, database_now = admin.execute(text(
            "SELECT GREATEST(g.expires_at,lease.expires_at),clock_timestamp() "
            "FROM okx_demo_submission_grants g "
            "JOIN okx_order_write_attempts a ON a.approval_id=g.approval_id "
            "JOIN okx_order_writer_leases lease ON lease.execution_target_id='OKX_DEMO' "
            "WHERE a.id=:attempt"
        ), {"attempt": final_prepared.attempt_id}).one()
    time.sleep(max(0.0, (final_not_before - database_now).total_seconds()) + 0.1)

    with postgres_writer_engine.begin() as admin:
        final_lifecycle_id, final_fencing, final_grant_id = admin.execute(text(
            "SELECT l.lifecycle_id,l.fencing_version,l.submission_grant_id "
            "FROM okx_demo_canary_lifecycles l "
            "WHERE l.opening_exchange_order_row_id=:order_id"
        ), {"order_id": final_prepared.exchange_order_row_id}).one()
        admin.execute(text(
            "UPDATE okx_demo_canary_consent_handoffs SET consent_deadline_at="
            "clock_timestamp()-interval '1 second',consented_at="
            "clock_timestamp()-interval '2 seconds',bundle_expires_at="
            "clock_timestamp()-interval '1 second' WHERE handoff_id=:handoff"
        ), {"handoff": final_successor.handoff_id})
        admin.execute(text(
            "UPDATE okx_demo_canary_lifecycles SET deadline_at="
            "created_at+interval '1 microsecond' WHERE lifecycle_id=:lifecycle"
        ), {"lifecycle": final_lifecycle_id})
        admin.execute(text(
            "UPDATE okx_order_write_attempts SET dispatch_not_after="
            "clock_timestamp()-interval '1 second' WHERE id=:attempt"
        ), {"attempt": final_prepared.attempt_id})
        final_observed_at = admin.execute(text(
            "SELECT last_attempt_at FROM okx_order_write_attempts WHERE id=:attempt"
        ), {"attempt": final_prepared.attempt_id}).scalar_one()
        final_evidence = {
            "absolute_submission_claim": False,
            "attempt_count": 1,
            "client_order_id": final_prepared.client_order_id,
            "exchange_result_code": "51603",
            "exchange_result_state": "NOT_FOUND",
            "fill_count": 0,
            "instrument_id": "BTC-USDT-SWAP",
            "query_kind": "exact_get",
            "request_digest": final_prepared.request_digest,
            "restart_resubmission_count": 0,
        }
        final_evidence_digest = admin.execute(text(
            "SELECT encode(digest(convert_to(canonical_jsonb_text(CAST(:evidence AS jsonb)),"
            "'UTF8'),'sha256'),'hex')"
        ), {"evidence": json.dumps(final_evidence)}).scalar_one()
        final_acceptance_digest = admin.execute(text(
            "SELECT encode(digest(convert_to(canonical_jsonb_text(jsonb_build_object("
            "'acceptance_kind','USER_ACCEPTED_NOT_FOUND_NO_FILL_FINAL_V1',"
            "'absolute_submission_claim',false,'attempt_id',CAST(:attempt AS bigint),"
            "'evidence_digest',CAST(:evidence_digest AS text),'evidence_observed_at',"
            "CAST(:observed_at AS timestamptz),'exchange_order_row_id',"
            "CAST(:order_id AS bigint),'lifecycle_id',CAST(:lifecycle AS text),"
            "'parent_terminal_receipt_id',2,'predecessor_grant_id',CAST(:grant AS text),"
            "'predecessor_handoff_id',CAST(:handoff AS text),'receipt_depth',3,"
            "'request_digest',CAST(:request_digest AS text),'source_job_id',22,"
            "'successor_allowed',false)),'UTF8'),'sha256'),'hex')"
        ), {
            "attempt": final_prepared.attempt_id,
            "evidence_digest": final_evidence_digest,
            "observed_at": final_observed_at,
            "order_id": final_prepared.exchange_order_row_id,
            "lifecycle": final_lifecycle_id,
            "grant": final_grant_id,
            "handoff": final_successor.handoff_id,
            "request_digest": final_prepared.request_digest,
        }).scalar_one()
        final_payload = {
            "acceptance_digest": final_acceptance_digest,
            "attempt_id": final_prepared.attempt_id,
            "evidence_digest": final_evidence_digest,
            "evidence_observed_at": final_observed_at.isoformat(),
            "evidence_snapshot": final_evidence,
            "expected_fencing_version": final_fencing,
            "lifecycle_id": final_lifecycle_id,
            "parent_terminal_receipt_id": 2,
        }

    postgres_writer_engine.dispose()

    def terminalize_final(_index: int):
        with postgres_writer_engine.begin() as owner:
            return owner.execute(text(
                "SELECT terminalize_final_accepted_not_found_no_fill("
                "CAST(:payload AS jsonb))"
            ), {"payload": json.dumps(final_payload)}).scalar_one()

    with ThreadPoolExecutor(max_workers=2) as executor:
        final_receipts = list(executor.map(terminalize_final, (1, 2)))
    assert {int(item["terminalization_id"]) for item in final_receipts} == {3}
    assert sorted(bool(item["idempotent"]) for item in final_receipts) == [False, True]
    assert all(item["absolute_submission_claim"] is False for item in final_receipts)
    assert all(item["successor_allowed"] is False for item in final_receipts)

    with pytest.raises(SQLAlchemyError):
        with postgres_writer_engine.begin() as runtime:
            runtime.execute(text("SET LOCAL ROLE freqtrade"))
            runtime.execute(text(
                "SELECT terminalize_final_accepted_not_found_no_fill("
                "CAST(:payload AS jsonb))"
            ), {"payload": json.dumps(final_payload)})

    # A fresh connection models restart recovery: no additional attempt, POST,
    # fill, position, grant, or successor is created after the final receipt.
    postgres_writer_engine.dispose()
    with postgres_writer_engine.connect() as restarted:
        assert restarted.execute(text(
            "SELECT count(*),sum(attempt_count) FROM okx_order_write_attempts"
        )).one() == (3, 3)
        assert restarted.execute(text(
            "SELECT count(*) FROM okx_demo_accepted_not_found_terminalizations"
        )).scalar_one() == 3
        assert restarted.execute(text(
            "SELECT count(*) FROM exchange_fills"
        )).scalar_one() == 0
        assert restarted.execute(text(
            "SELECT count(*) FROM exchange_positions WHERE quantity<>0"
        )).scalar_one() == 0
        assert restarted.execute(text(
            "SELECT count(*) FROM okx_demo_submission_grants WHERE status='ACTIVE'"
        )).scalar_one() == 0
        assert restarted.execute(text(
            "SELECT okx_demo_canary_consent_eligibility()->>'eligibility_state'"
        )).scalar_one() == "BLOCKED"
        receipts_before_reinstall = restarted.execute(text(
            "SELECT jsonb_agg(to_jsonb(receipt) ORDER BY receipt_depth) "
            "FROM okx_demo_accepted_not_found_terminalizations receipt"
        )).scalar_one()

    # Future migrations replay the preceding installers before adding their own
    # boundary.  Reinstall v33 then v34 over durable R1/R2/R3 evidence and prove
    # that no receipt is rewritten and the owner-only ACL remains closed.
    with postgres_writer_engine.begin() as owner:
        _add_bounded_second_accepted_not_found_boundary(owner)
        _add_final_accepted_not_found_boundary(owner)
    assert schema_problems(postgres_writer_engine) == []
    with postgres_writer_engine.connect() as restarted:
        receipts_after_reinstall = restarted.execute(text(
            "SELECT jsonb_agg(to_jsonb(receipt) ORDER BY receipt_depth) "
            "FROM okx_demo_accepted_not_found_terminalizations receipt"
        )).scalar_one()
        assert receipts_after_reinstall == receipts_before_reinstall
        assert restarted.execute(text(
            "SELECT has_function_privilege('freqtrade',"
            "'terminalize_final_accepted_not_found_no_fill(jsonb)','EXECUTE')"
        )).scalar_one() is False
        assert restarted.execute(text(
            "SELECT has_table_privilege('freqtrade',"
            "'okx_demo_accepted_not_found_terminalizations','INSERT,UPDATE,DELETE')"
        )).scalar_one() is False


def test_postgresql_v31_upgrades_to_v32_terminalization_boundary(
    postgres_writer_engine, monkeypatch,
) -> None:
    assert upgrade_database(postgres_writer_engine) == SCHEMA_VERSION
    now, run_id, predecessor, current, read_type = _prepare_v31_atomic_successor(
        postgres_writer_engine, monkeypatch, suffix="nonempty-upgrade"
    )
    runtime_id = "RuntimeV31NonemptyUpgrade"
    with postgres_writer_engine.connect() as connection, Session(bind=connection) as runtime:
        runtime.execute(text("SET LOCAL ROLE freqtrade"))
        store = SqlAlchemyOrderWriterStore(runtime)
        assert acquire_one_shot_runtime_lock(runtime) is True
        finalized = process_pending_canary_consent_handoff(
            read_client=read_type(30), db=runtime, runtime_instance_id=runtime_id,
            fresh_reconciliation=lambda: {"reconciliation_run_id": run_id},
            safety_check=lambda: True,
            atomic_holder_token_digest=store.atomic_process_token_digest, now=now,
        )
        prepared = SimpleNamespace(**dict(finalized.atomic_receipt))
        assert runtime.execute(text(
            "SELECT claim_atomic_okx_demo_canary_dispatch("
            ":attempt,:runtime,:token,:generation,:digest)"
        ), {
            "attempt": prepared.attempt_id, "runtime": runtime_id,
            "token": store.atomic_process_token,
            "generation": prepared.lease_generation,
            "digest": prepared.request_digest,
        }).scalar_one() is not None
        runtime.commit()
        assert release_one_shot_runtime_lock(runtime) is True
        runtime.commit()
    with postgres_writer_engine.begin() as admin:
        admin.execute(text(
            "UPDATE okx_order_writer_leases SET "
            "acquired_at=clock_timestamp()-interval '3 seconds',"
            "heartbeat_at=clock_timestamp()-interval '2 seconds',"
            "expires_at=clock_timestamp()-interval '1 second'"
        ))

    class UpgradeNotFound:
        def order(self, inst_id, *, order_id=None, client_order_id=None):
            raise OkxReadAdapterError(
                kind="BUSINESS_ERROR", status="FAILED", message="not found",
                okx_code="51603",
            )

    with Session(postgres_writer_engine) as runtime:
        result = OkxDemoOrderWriter(
            read_client=UpgradeNotFound(),
            write_transport=SimpleNamespace(post=lambda **_: pytest.fail("POST forbidden")),
            store=SqlAlchemyOrderWriterStore(runtime),
        ).reconcile_unresolved(prepared.attempt_id)
        assert result.status == "RECOVERY_REQUIRED"
    with postgres_writer_engine.connect() as admin:
        before = admin.execute(text(
            "SELECT a.state,a.reason_code,a.attempt_count,a.safe_response_snapshot::jsonb,"
            "(h.supersedes_handoff_id IS NOT NULL),h.status,g.status,ae.status "
            "FROM okx_order_write_attempts a JOIN okx_demo_submission_grants g "
            "ON g.approval_id=a.approval_id JOIN okx_demo_canary_consent_handoffs h "
            "ON h.grant_id=g.grant_id JOIN approved_executions ae ON ae.id=a.approval_id "
            "WHERE a.id=:attempt"
        ), {"attempt": prepared.attempt_id}).one()
    with postgres_writer_engine.begin() as admin:
        admin.execute(text(
            "DROP TRIGGER IF EXISTS okx_order_write_attempts_accepted_not_found_guard "
            "ON okx_order_write_attempts"
        ))
        admin.execute(text(
            "DROP FUNCTION IF EXISTS guard_accepted_not_found_attempt_transition()"
        ))
        admin.execute(text(
            "CREATE OR REPLACE FUNCTION guard_okx_demo_exchange_order() "
            "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$"
        ))
        admin.execute(text(
            "ALTER TABLE okx_demo_canary_lifecycles "
            "DROP COLUMN accepted_terminalization_id CASCADE"
        ))
        admin.execute(text(
            "ALTER TABLE okx_demo_canary_consent_handoffs "
            "DROP COLUMN terminal_receipt_id CASCADE"
        ))
        admin.execute(text(
            "DROP TABLE okx_demo_accepted_not_found_terminalizations CASCADE"
        ))
        admin.execute(text(
            "DROP INDEX IF EXISTS okx_demo_canary_one_successor_ever_idx"
        ))
        admin.execute(text("DELETE FROM {}".format(VERSION_TABLE)))
        admin.execute(text(
            "INSERT INTO {}(version) VALUES(:version)".format(VERSION_TABLE)
        ), {"version": "20260803_31"})

    assert upgrade_database(postgres_writer_engine) == SCHEMA_VERSION
    assert schema_problems(postgres_writer_engine) == []
    with postgres_writer_engine.connect() as admin:
        after = admin.execute(text(
            "SELECT a.state,a.reason_code,a.attempt_count,a.safe_response_snapshot::jsonb,"
            "(h.supersedes_handoff_id IS NOT NULL),h.status,g.status,ae.status "
            "FROM okx_order_write_attempts a JOIN okx_demo_submission_grants g "
            "ON g.approval_id=a.approval_id JOIN okx_demo_canary_consent_handoffs h "
            "ON h.grant_id=g.grant_id JOIN approved_executions ae ON ae.id=a.approval_id "
            "WHERE a.id=:attempt"
        ), {"attempt": prepared.attempt_id}).one()
        assert after == before
        assert admin.execute(text(
            "SELECT to_regclass('okx_demo_accepted_not_found_terminalizations') "
            "IS NOT NULL"
        )).scalar_one() is True
        assert admin.execute(text(
            "SELECT has_function_privilege('freqtrade',"
            "'terminalize_accepted_not_found_no_fill(jsonb)','EXECUTE')"
        )).scalar_one() is False
        assert "USER_ACCEPTED_NOT_FOUND_NO_FILL" in admin.execute(text(
            "SELECT prosrc FROM pg_proc WHERE oid="
            "to_regprocedure('guard_okx_demo_exchange_order()')"
        )).scalar_one()
        assert admin.execute(text(
            "SELECT count(*)=1 FROM pg_trigger WHERE tgrelid="
            "to_regclass('okx_order_write_attempts') AND "
            "tgname='okx_order_write_attempts_accepted_not_found_guard' "
            "AND NOT tgisinternal"
        )).scalar_one() is True

    # Re-run the exact recorded v32 -> v33 route over non-empty unresolved
    # state.  The migration must preserve the current attempt byte-for-byte
    # and cannot create a receipt or dispatch another POST.
    with postgres_writer_engine.begin() as admin:
        admin.execute(text(
            "DROP TRIGGER IF EXISTS okx_demo_canary_bounded_accepted_successor_guard "
            "ON okx_demo_canary_consent_handoffs"
        ))
        for signature in (
            "terminalize_second_accepted_not_found_no_fill(jsonb)",
            "guard_bounded_accepted_successor_handoff()",
            "exact_bounded_accepted_not_found_predecessor(text)",
        ):
            admin.execute(text("DROP FUNCTION IF EXISTS {}".format(signature)))
        admin.execute(text(
            "ALTER TABLE okx_demo_accepted_not_found_terminalizations "
            "DROP COLUMN IF EXISTS parent_terminal_receipt_id CASCADE,"
            "DROP COLUMN IF EXISTS receipt_depth CASCADE"
        ))
        admin.execute(text("DELETE FROM {}".format(VERSION_TABLE)))
        admin.execute(text(
            "INSERT INTO {}(version) VALUES(:version)".format(VERSION_TABLE)
        ), {"version": BOUNDED_SECOND_ACCEPTANCE_BASE_VERSION})
    assert upgrade_database(postgres_writer_engine) == SCHEMA_VERSION
    with postgres_writer_engine.connect() as admin:
        restarted = admin.execute(text(
            "SELECT a.state,a.reason_code,a.attempt_count,a.safe_response_snapshot::jsonb,"
            "(h.supersedes_handoff_id IS NOT NULL),h.status,g.status,ae.status "
            "FROM okx_order_write_attempts a JOIN okx_demo_submission_grants g "
            "ON g.approval_id=a.approval_id JOIN okx_demo_canary_consent_handoffs h "
            "ON h.grant_id=g.grant_id JOIN approved_executions ae ON ae.id=a.approval_id "
            "WHERE a.id=:attempt"
        ), {"attempt": prepared.attempt_id}).one()
        assert restarted == before
        assert admin.execute(text(
            "SELECT count(*) FROM okx_demo_accepted_not_found_terminalizations"
        )).scalar_one() == 0
    assert schema_problems(postgres_writer_engine) == []


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
    test_now = datetime.now(timezone.utc).replace(microsecond=0)
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
        "observed_at": test_now.isoformat(),
        "received_at": test_now.isoformat(),
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
        "observed_at": test_now.isoformat(),
        "received_at": test_now.isoformat(),
    }
    account_event = {
        "schema_version": RECONCILIATION_EVENT_SCHEMA_VERSION,
        "execution_target": "OKX_DEMO",
        "source": "REST",
        "entity_kind": "ACCOUNT",
        "entity_key": "account",
        "source_sequence": 3,
        "stream_generation": 1,
        "observed_at": test_now.isoformat(),
        "received_at": test_now.isoformat(),
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
            overlap_started_at=test_now - timedelta(seconds=5),
            observed_at=test_now,
            completed_at=test_now,
        )
        result = service.reconcile(now=test_now)
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
            now_provider=lambda: test_now,
        )
        store.acquire_recovery_lease(grant_id, now=test_now)
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
            now_provider=lambda: test_now,
        )
        store.acquire_recovery_lease(reduce_grant_id, now=test_now)
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
