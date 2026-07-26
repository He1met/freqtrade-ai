from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.adapters.okx_demo.write_semantics import OkxDemoWriteBlocked
from app.adapters.okx_demo.writer_repository import SqlAlchemyOrderWriterStore
from app.db.migrations import (
    ORDER_WRITER_BASE_VERSION,
    SCHEMA_VERSION,
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
    RiskDecision,
    TradeIntent,
)
from app.models.order_writer import OkxOrderWriteAttempt
from app.repositories.execution_lineage import ensure_execution_scope_catalog
from app.services.risk_chain import (
    _issue_attested_session_capability,
    _normalize_attested_snapshot,
    _write_attested_snapshot,
    canonical_digest,
)


NOW = datetime(2026, 7, 27, tzinfo=timezone.utc)


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


def _seed_approved_order(session: Session) -> tuple[int, int]:
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
    canonical_input = {"request": "postgresql-writer-test"}
    canonical_hash = canonical_digest(canonical_input)
    lineage = {"test_lineage": True}
    notional = Decimal("57000")
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
        position_side="net",
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
    return approval.id, order.id


def test_postgresql_fresh_schema_and_any_predicate_verify(
    postgres_writer_engine,
) -> None:
    assert upgrade_database(postgres_writer_engine) == SCHEMA_VERSION
    readiness = verify_schema(postgres_writer_engine)

    assert readiness.ready is True
    assert readiness.problems == ()


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
