import os
import json
from datetime import datetime, timedelta, timezone
from threading import Barrier, Lock, Thread
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.adapters.okx_demo import read_adapter as read_boundary
from app.adapters.okx_demo.errors import OkxReadAdapterError
from app.adapters.okx_demo.transport import OkxReadHttpResponse
from app.db.migrations import (
    ATTESTATION_ACL_BASE_VERSION,
    ATTESTED_SESSION_BASE_VERSION,
    RISK_CHAIN_BASE_VERSION,
    RISK_CHAIN_HARDENING_BASE_VERSION,
    SCHEMA_VERSION,
    SchemaMigrationBlocked,
    TRUSTED_SNAPSHOT_BASE_VERSION,
    VERSION_TABLE,
    _add_trusted_snapshot_boundary,
    harden_attestation_access_boundary,
    schema_problems,
    upgrade_database,
    verify_schema,
)
from app.db.session import create_database_engine, create_session_factory
from app.models import (
    ApprovedExecution,
    BacktestResult,
    BacktestRun,
    BacktestTask,
    Base,
    OkxDemoAttestedSession,
    OkxDemoTrustedSnapshot,
    RiskBudget,
    RiskDecision,
    Strategy,
    StrategyScore,
    StrategyVersion,
    TradeIntent,
)
from app.models.execution_lineage import OKX_DEMO_TARGET_ID
from app.repositories.execution_lineage import ensure_execution_scope_catalog
from app.services.risk_chain import (
    RiskChainService,
    _issue_attested_session_capability,
    _normalize_attested_snapshot,
    _persist_attested_session,
    _revoke_attested_session,
    _write_attested_snapshot,
    canonical_digest,
)


POSTGRES_WORKER_URL = os.environ.get("POSTGRES_WORKER_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_WORKER_URL,
    reason="POSTGRES_WORKER_URL is required for the PostgreSQL risk-chain gate",
)


@pytest.fixture()
def postgres_engine():
    assert POSTGRES_WORKER_URL is not None
    schema = "risk_chain_{}".format(uuid4().hex)
    admin = create_database_engine(POSTGRES_WORKER_URL)
    with admin.begin() as connection:
        connection.execute(text('CREATE SCHEMA "{}"'.format(schema)))
    engine = create_engine(
        POSTGRES_WORKER_URL,
        pool_pre_ping=True,
        connect_args={"options": "-csearch_path={}".format(schema)},
    )
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text('DROP SCHEMA "{}" CASCADE'.format(schema)))
        admin.dispose()


def _seed(factory) -> dict[str, int]:
    with factory.begin() as db:
        ensure_execution_scope_catalog(db)
        strategy = Strategy(name="PG risk chain", slug="pg-risk-chain")
        db.add(strategy)
        db.flush()
        version = StrategyVersion(
            strategy_id=strategy.id,
            version_number=1,
            blueprint={},
            generated_code="class PGRiskChain: pass",
            file_path="/tmp/pg-risk-chain.py",
            validation_status="passed",
        )
        db.add(version)
        db.flush()
        strategy.current_version_id = version.id
        run = BacktestRun(
            execution_scope_id=OKX_DEMO_TARGET_ID,
            strategy_version_id=version.id,
            config_snapshot={},
            status="succeeded",
        )
        db.add(run)
        db.flush()
        task = BacktestTask(
            backtest_run_id=run.id,
            pair="BTC/USDT:USDT",
            timeframe="15m",
            status="succeeded",
        )
        db.add(task)
        db.flush()
        result = BacktestResult(
            backtest_run_id=run.id,
            backtest_task_id=task.id,
            result_path="/tmp/pg-result.json",
            metrics_snapshot={},
        )
        db.add(result)
        db.flush()
        score = StrategyScore(
            strategy_id=strategy.id,
            strategy_version_id=version.id,
            backtest_result_id=result.id,
            scoring_version="risk-chain-pg-v1",
            total_score=80,
        )
        db.add(score)
        db.flush()
        return {
            "strategy_id": strategy.id,
            "strategy_version_id": version.id,
            "backtest_run_id": run.id,
            "backtest_task_id": task.id,
            "backtest_result_id": result.id,
            "strategy_score_id": score.id,
        }


def _request(
    lineage: dict[str, int],
    now: datetime,
    factory=None,
    capability_sink=None,
) -> dict:
    expiry = (now + timedelta(minutes=5)).isoformat()
    instrument = {
        "execution_target": OKX_DEMO_TARGET_ID,
        "source": "okx_demo_rest",
        "resource": "instrument",
        "stale": False,
        "authenticated": False,
        "instId": "BTC-USDT-SWAP",
        "instrument_type": "SWAP",
        "ctVal": "1",
        "ctValCcy": "BTC",
        "lotSz": "0.001",
        "minSz": "0.001",
        "tickSz": "0.1",
        "contract_shape": "linear",
        "expires_at": expiry,
    }
    market = {
        "execution_target": OKX_DEMO_TARGET_ID,
        "source": "okx_demo_rest",
        "resource": "market",
        "stale": False,
        "authenticated": False,
        "instrument_id": "BTC-USDT-SWAP",
        "reference_price": "50000",
        "as_of": now.isoformat(),
        "expires_at": expiry,
    }
    account = {
        "execution_target": OKX_DEMO_TARGET_ID,
        "source": "okx_demo_rest",
        "resource": "account",
        "stale": False,
        "authenticated": True,
        "account_mode": "net",
        "margin_mode": "isolated",
        "current_exposure": "0",
        "open_positions": 0,
        "leverage": "2",
        "as_of": now.isoformat(),
        "expires_at": expiry,
    }
    def envelope(name: str, content: dict) -> dict:
        digest = canonical_digest(content)
        return {
            "ref": "{}:{}".format(name, digest[:24]),
            "digest": digest,
            "expires_at": expiry,
            "content": content,
        }

    request = {
        "execution_target": OKX_DEMO_TARGET_ID,
        "lineage": lineage,
        "snapshots": {
            name: envelope(name, content)
            for name, content in (
                ("instrument", instrument),
                ("market", market),
                ("account", account),
            )
        },
        "instrument_id": "BTC-USDT-SWAP",
        "side": "buy",
        "position_side": "net",
        "order_type": "limit",
        "quantity": "0.012",
        "limit_price": "50000",
        "reference_price": "50000",
        "leverage": "2",
        "margin_mode": "isolated",
        "stop_loss": "48000",
        "take_profit": "54000",
        "reduce_only": False,
    }
    if factory is not None:
        snapshots = request.pop("snapshots")
        snapshot_ids = {}
        capability = _issue_attested_session_capability(
            attestation_hmac_key=b"t" * 32,
            pinned_fingerprint_sha256="e" * 64,
            created_at=now,
            expires_at=now + timedelta(minutes=10),
        )
        if capability_sink is not None:
            capability_sink.append(capability)
        with factory.begin() as db:
            for name, envelope in snapshots.items():
                snapshot_expiry = datetime.fromisoformat(
                    envelope["content"]["expires_at"].replace("Z", "+00:00")
                )
                normalized = _normalize_attested_snapshot(
                    capability,
                    kind=name,
                    content=envelope["content"],
                    observed_at=now,
                    expires_at=snapshot_expiry,
                )
                row = _write_attested_snapshot(
                    db,
                    capability,
                    normalized,
                    now=now,
                )
                snapshot_ids[name] = row.snapshot_id
        request["snapshot_ids"] = snapshot_ids
    return request


def _policy() -> dict:
    return {
        "allowed_instruments": ["BTC-USDT-SWAP"],
        "allowed_sides": ["buy", "sell"],
        "allowed_order_types": ["limit", "market"],
        "max_leverage": "3",
        "max_order_notional": "1000",
        "max_total_exposure": "600",
        "max_positions": 1,
        "max_price_deviation_pct": "0.02",
        "min_strategy_score": "70",
        "scoring_version": "risk-chain-pg-v1",
    }


def test_20260727_02_upgrades_to_risk_chain_atomically(postgres_engine) -> None:
    Base.metadata.create_all(postgres_engine)
    with postgres_engine.begin() as connection:
        for table_name in (
            "full_chain_signal_snapshots",
            "full_chain_stage_runs",
            "strategy_candidate_approvals",
            "full_chain_runs",
        ):
            connection.execute(text('DROP TABLE "{}"'.format(table_name)))
        connection.execute(text("DROP TABLE approved_executions"))
        connection.execute(text("DROP TABLE risk_budgets"))
        connection.execute(
            text(
                "ALTER TABLE trade_intents "
                "DROP CONSTRAINT trade_intents_intent_id_key, "
                "DROP CONSTRAINT trade_intents_target_idempotency_unique, "
                "DROP CONSTRAINT trade_intents_approval_identity_unique, "
                "DROP CONSTRAINT trade_intents_status_check, "
                "DROP CONSTRAINT trade_intents_intent_id_format_check, "
                "DROP CONSTRAINT trade_intents_canonical_hash_format_check, "
                "DROP CONSTRAINT trade_intents_policy_digest_format_check, "
                "DROP CONSTRAINT trade_intents_idempotency_digest_format_check, "
                "DROP CONSTRAINT trade_intents_side_check, "
                "DROP CONSTRAINT trade_intents_position_side_check, "
                "DROP CONSTRAINT trade_intents_margin_mode_check, "
                "DROP CONSTRAINT trade_intents_order_type_check, "
                "DROP CONSTRAINT trade_intents_order_combo_check, "
                "DROP COLUMN intent_id, DROP COLUMN canonical_hash, "
                "DROP COLUMN policy_digest, DROP COLUMN idempotency_key_digest, "
                "DROP COLUMN strategy_id, "
                "DROP COLUMN backtest_run_id, DROP COLUMN backtest_result_id, "
                "DROP COLUMN strategy_score_id, DROP COLUMN expires_at, "
                "DROP COLUMN reference_price, DROP COLUMN leverage, "
                "DROP COLUMN margin_mode, DROP COLUMN stop_loss, "
                "DROP COLUMN take_profit, DROP COLUMN reduce_only"
            )
        )
        connection.execute(
            text(
                "ALTER TABLE risk_decisions "
                "DROP CONSTRAINT risk_decisions_id_intent_unique, "
                "DROP CONSTRAINT risk_decisions_decision_check"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE {} (version VARCHAR(64) PRIMARY KEY, "
                "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())".format(VERSION_TABLE)
            )
        )
        connection.execute(
            text("INSERT INTO {} (version) VALUES (:version)".format(VERSION_TABLE)),
            {"version": RISK_CHAIN_BASE_VERSION},
        )

    assert upgrade_database(postgres_engine) == SCHEMA_VERSION
    assert verify_schema(postgres_engine).ready is True


def test_20260727_03_hardens_existing_risk_chain(postgres_engine) -> None:
    Base.metadata.create_all(postgres_engine)
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE approved_executions "
                "DROP CONSTRAINT approved_executions_intent_identity_fkey, "
                "DROP CONSTRAINT approved_executions_decision_intent_fkey, "
                "DROP CONSTRAINT approved_executions_payload_identity_fkey, "
                "DROP CONSTRAINT approved_executions_claim_required_check, "
                "DROP CONSTRAINT approved_executions_status_check, "
                "DROP CONSTRAINT approved_executions_approved_state_check, "
                "DROP CONSTRAINT approved_executions_reservation_check, "
                "DROP CONSTRAINT approved_executions_client_order_id_format_check, "
                "DROP CONSTRAINT approved_executions_intent_id_format_check, "
                "DROP CONSTRAINT approved_executions_authorization_schema_check, "
                "DROP CONSTRAINT approved_executions_canonical_hash_format_check, "
                "DROP CONSTRAINT approved_executions_policy_digest_format_check, "
                    "DROP CONSTRAINT approved_executions_payload_hash_format_check, "
                    "DROP CONSTRAINT approved_executions_instrument_snapshot_fkey, "
                    "DROP CONSTRAINT approved_executions_market_snapshot_fkey, "
                    "DROP CONSTRAINT approved_executions_account_snapshot_fkey, "
                "DROP COLUMN status, DROP COLUMN decision, "
                "DROP COLUMN intent_status, DROP COLUMN reserved_notional, "
                "DROP COLUMN authorization_schema_version, "
                    "DROP COLUMN canonical_hash, DROP COLUMN policy_digest, "
                    "DROP COLUMN approved_payload_hash, "
                    "DROP COLUMN instrument_snapshot_id, "
                    "DROP COLUMN market_snapshot_id, "
                    "DROP COLUMN account_snapshot_id"
            )
        )
        connection.execute(
            text(
                "ALTER TABLE trade_intents "
                "DROP CONSTRAINT trade_intents_approval_identity_unique, "
                "DROP CONSTRAINT trade_intents_approved_payload_unique, "
                "DROP CONSTRAINT trade_intents_status_check, "
                "DROP CONSTRAINT trade_intents_authorization_schema_check, "
                "DROP CONSTRAINT trade_intents_scope_contract_check, "
                "DROP CONSTRAINT trade_intents_intent_id_format_check, "
                "DROP CONSTRAINT trade_intents_canonical_hash_format_check, "
                "DROP CONSTRAINT trade_intents_policy_digest_format_check, "
                "DROP CONSTRAINT trade_intents_idempotency_digest_format_check, "
                "DROP CONSTRAINT trade_intents_side_check, "
                "DROP CONSTRAINT trade_intents_position_side_check, "
                "DROP CONSTRAINT trade_intents_margin_mode_check, "
                "DROP CONSTRAINT trade_intents_order_type_check, "
                "DROP CONSTRAINT trade_intents_order_combo_check, "
                "DROP COLUMN authorization_schema_version, "
                "DROP COLUMN approved_payload_hash, "
                "DROP COLUMN policy_digest, DROP COLUMN reference_price, "
                "DROP COLUMN leverage, DROP COLUMN margin_mode, "
                "DROP COLUMN stop_loss, DROP COLUMN take_profit, "
                "DROP COLUMN reduce_only, "
                "ALTER COLUMN instrument_id SET NOT NULL, "
                "ALTER COLUMN side SET NOT NULL, "
                "ALTER COLUMN position_side SET NOT NULL, "
                "ALTER COLUMN order_type SET NOT NULL, "
                "ALTER COLUMN quantity SET NOT NULL"
            )
        )
        connection.execute(
            text(
                "ALTER TABLE risk_decisions "
                "DROP CONSTRAINT risk_decisions_id_intent_unique, "
                "DROP CONSTRAINT risk_decisions_decision_check, "
                "DROP CONSTRAINT risk_decisions_authorization_schema_check, "
                "DROP CONSTRAINT risk_decisions_policy_digest_format_check, "
                "DROP COLUMN authorization_schema_version, "
                "DROP COLUMN policy_digest"
            )
        )
        connection.execute(text("DROP TABLE okx_demo_trusted_snapshots"))
        connection.execute(
            text(
                "ALTER TABLE risk_budgets "
                "DROP CONSTRAINT risk_budgets_nonnegative_check"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE {} (version VARCHAR(64) PRIMARY KEY, "
                "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())".format(VERSION_TABLE)
            )
        )
        connection.execute(
            text("INSERT INTO {} (version) VALUES (:version)".format(VERSION_TABLE)),
            {"version": RISK_CHAIN_HARDENING_BASE_VERSION},
        )

    assert upgrade_database(postgres_engine) == SCHEMA_VERSION
    assert verify_schema(postgres_engine).ready is True


def test_20260727_04_adds_trusted_registry_and_immutability_triggers(
    postgres_engine,
) -> None:
    Base.metadata.create_all(postgres_engine)
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE approved_executions "
                "DROP CONSTRAINT approved_executions_instrument_snapshot_fkey, "
                "DROP CONSTRAINT approved_executions_market_snapshot_fkey, "
                "DROP CONSTRAINT approved_executions_account_snapshot_fkey, "
                "DROP COLUMN instrument_snapshot_id, "
                "DROP COLUMN market_snapshot_id, "
                "DROP COLUMN account_snapshot_id"
            )
        )
        connection.execute(text("DROP TABLE okx_demo_trusted_snapshots"))
        connection.execute(
            text(
                "CREATE TABLE {} (version VARCHAR(64) PRIMARY KEY, "
                "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())".format(VERSION_TABLE)
            )
        )
        connection.execute(
            text("INSERT INTO {} (version) VALUES (:version)".format(VERSION_TABLE)),
            {"version": TRUSTED_SNAPSHOT_BASE_VERSION},
        )

    assert upgrade_database(postgres_engine) == SCHEMA_VERSION
    assert verify_schema(postgres_engine).ready is True
    with postgres_engine.connect() as connection:
        triggers = set(
            connection.execute(
                text(
                    "SELECT tgname FROM pg_trigger "
                    "WHERE NOT tgisinternal"
                )
            ).scalars()
        )
    assert "trade_intents_active_approval_immutable" in triggers
    assert "okx_demo_trusted_snapshots_immutable" in triggers


def test_20260727_05_adds_private_attested_session_boundary(
    postgres_engine,
) -> None:
    Base.metadata.create_all(postgres_engine)
    with postgres_engine.begin() as connection:
        _add_trusted_snapshot_boundary(connection)
        connection.execute(
            text(
                "ALTER TABLE okx_demo_trusted_snapshots "
                "DROP CONSTRAINT okx_demo_trusted_snapshots_session_fkey, "
                "DROP CONSTRAINT okx_demo_trusted_snapshots_time_check, "
                "DROP COLUMN attested_session_expires_at"
            )
        )
        connection.execute(text("DROP TABLE okx_demo_attested_sessions"))
        connection.execute(
            text(
                "CREATE TABLE {} (version VARCHAR(64) PRIMARY KEY, "
                "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())".format(VERSION_TABLE)
            )
        )
        connection.execute(
            text("INSERT INTO {} (version) VALUES (:version)".format(VERSION_TABLE)),
            {"version": ATTESTED_SESSION_BASE_VERSION},
        )

    assert upgrade_database(postgres_engine) == SCHEMA_VERSION
    readiness = verify_schema(postgres_engine)
    assert readiness.ready is True
    with postgres_engine.connect() as connection:
        boundaries = connection.execute(
            text(
                "SELECT c.relname, r.rolname "
                "FROM pg_class c JOIN pg_roles r ON r.oid = c.relowner "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = current_schema() "
                "AND c.relname IN ('okx_demo_attested_sessions', "
                "'okx_demo_trusted_snapshots')"
            )
        ).all()
    assert set(boundaries) == {
        ("okx_demo_attested_sessions", "freqtrade_ai_attestor"),
        ("okx_demo_trusted_snapshots", "freqtrade_ai_attestor"),
    }


def test_20260727_07_adds_approval_snapshot_foreign_keys(
    postgres_engine,
) -> None:
    assert upgrade_database(postgres_engine) == SCHEMA_VERSION
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE approved_executions "
                "DROP CONSTRAINT approved_executions_instrument_snapshot_fkey, "
                "DROP CONSTRAINT approved_executions_market_snapshot_fkey, "
                "DROP CONSTRAINT approved_executions_account_snapshot_fkey, "
                "DROP COLUMN instrument_snapshot_id, "
                "DROP COLUMN market_snapshot_id, "
                "DROP COLUMN account_snapshot_id"
            )
        )
        connection.execute(text("DELETE FROM {}".format(VERSION_TABLE)))
        connection.execute(
            text(
                "INSERT INTO {} (version) VALUES (:version)".format(
                    VERSION_TABLE
                )
            ),
            {"version": ATTESTATION_ACL_BASE_VERSION},
        )
    assert upgrade_database(postgres_engine) == SCHEMA_VERSION
    assert verify_schema(postgres_engine).ready is True


def test_readiness_fails_closed_before_peer_admin_hardening(
    postgres_engine,
) -> None:
    Base.metadata.create_all(postgres_engine)
    problems = schema_problems(postgres_engine)
    assert any(
        problem.startswith("attestation owner mismatch:")
        for problem in problems
    )
    assert any(
        problem.startswith("attestation function boundary mismatch:")
        for problem in problems
    )


@pytest.mark.parametrize(
    ("tamper_sql", "expected_problem"),
    [
        (
            "ALTER ROLE freqtrade_ai_attestor LOGIN INHERIT",
            "attestor role boundary mismatch",
        ),
        (
            "GRANT SELECT ON okx_demo_attestation_secrets TO freqtrade",
            "runtime can read attestation secret table",
        ),
        (
            """
            CREATE OR REPLACE FUNCTION write_okx_demo_attested_session(
                p_session_id text,
                p_target text,
                p_fingerprint text,
                p_created_micros bigint,
                p_expires_micros bigint,
                p_nonce text,
                p_signature text
            ) RETURNS void
            LANGUAGE plpgsql SECURITY DEFINER
            SET search_path = pg_catalog
            AS $$ BEGIN RETURN; END; $$;
            ALTER FUNCTION write_okx_demo_attested_session(
                text,text,text,bigint,bigint,text,text
            ) OWNER TO freqtrade_ai_attestor;
            REVOKE ALL ON FUNCTION write_okx_demo_attested_session(
                text,text,text,bigint,bigint,text,text
            ) FROM PUBLIC;
            GRANT EXECUTE ON FUNCTION write_okx_demo_attested_session(
                text,text,text,bigint,bigint,text,text
            ) TO freqtrade
            """,
            "attestation function definition mismatch: "
            "write_okx_demo_attested_session",
        ),
    ],
)
def test_attestation_verifier_detects_role_acl_and_body_tampering(
    postgres_engine,
    tamper_sql,
    expected_problem,
) -> None:
    upgrade_database(postgres_engine)
    with postgres_engine.begin() as connection:
        connection.execute(text(tamper_sql))
    readiness = verify_schema(postgres_engine)
    assert readiness.ready is False
    assert any(
        expected_problem in problem
        for problem in readiness.problems
    )


def test_attestation_hardening_removes_runtime_membership(
    postgres_engine,
) -> None:
    upgrade_database(postgres_engine)
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "GRANT freqtrade_ai_attestor TO freqtrade "
                "WITH ADMIN TRUE, INHERIT FALSE, SET TRUE"
            )
        )
    readiness = verify_schema(postgres_engine)
    assert readiness.ready is False
    assert any(
        "runtime role membership reaches attestor owner role" in problem
        for problem in readiness.problems
    )
    harden_attestation_access_boundary(postgres_engine)
    assert verify_schema(postgres_engine).ready is True


def test_attestation_hardening_converges_current_schema_proof_key(
    postgres_engine,
) -> None:
    upgrade_database(postgres_engine)
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE okx_demo_attestation_secrets "
                "SET hmac_key = :mismatched_key "
                "WHERE secret_id = 'ACTIVE'"
            ),
            {"mismatched_key": b"x" * 32},
        )
    harden_attestation_access_boundary(postgres_engine)
    with postgres_engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT hmac_key = :expected_key "
                "FROM okx_demo_attestation_secrets "
                "WHERE secret_id = 'ACTIVE'"
            ),
            {"expected_key": b"t" * 32},
        ).scalar_one() is True
    with pytest.raises(DBAPIError):
        with postgres_engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE freqtrade"))
            connection.execute(
                text("SELECT hmac_key FROM okx_demo_attestation_secrets")
            )


def test_attestation_hardening_refuses_key_rotation_with_active_session(
    postgres_engine,
    monkeypatch,
) -> None:
    upgrade_database(postgres_engine)
    factory = create_session_factory(postgres_engine)
    lineage = _seed(factory)
    now = datetime.now(timezone.utc)
    raw_request = _request(lineage, now)
    envelope = raw_request["snapshots"]["instrument"]
    capability = _issue_attested_session_capability(
        attestation_hmac_key=b"t" * 32,
        pinned_fingerprint_sha256="d" * 64,
        created_at=now,
        expires_at=now + timedelta(minutes=10),
    )
    normalized = _normalize_attested_snapshot(
        capability,
        kind="instrument",
        content=envelope["content"],
        observed_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    with factory.begin() as db:
        db.execute(text("SET LOCAL ROLE freqtrade"))
        _write_attested_snapshot(db, capability, normalized, now=now)
    monkeypatch.setenv(
        "FREQTRADE_AI_OKX_DEMO_ATTESTATION_PROOF_KEY",
        "78" * 32,
    )
    with pytest.raises(
        SchemaMigrationBlocked,
        match="blocked by an active session",
    ):
        harden_attestation_access_boundary(postgres_engine)


@pytest.mark.parametrize(
    "privilege",
    [
        "SUPERUSER",
        "CREATEROLE",
        "CREATEDB",
        "REPLICATION",
        "BYPASSRLS",
    ],
)
def test_attestation_hardening_removes_attestor_role_privileges(
    postgres_engine,
    privilege,
) -> None:
    upgrade_database(postgres_engine)
    with postgres_engine.begin() as connection:
        connection.execute(
            text("ALTER ROLE freqtrade_ai_attestor {}".format(privilege))
        )
    readiness = verify_schema(postgres_engine)
    assert readiness.ready is False
    assert any(
        "attestor role boundary mismatch" in problem
        for problem in readiness.problems
    )
    harden_attestation_access_boundary(postgres_engine)
    assert verify_schema(postgres_engine).ready is True


@pytest.mark.parametrize(
    "table_name",
    [
        "okx_demo_attested_sessions",
        "okx_demo_attestation_secrets",
        "okx_demo_trusted_snapshots",
    ],
)
def test_reharden_removes_set_only_role_table_delete_and_truncate(
    postgres_engine,
    table_name,
) -> None:
    upgrade_database(postgres_engine)
    intermediary = "freqtrade_ai_acl_{}".format(uuid4().hex[:12])
    try:
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    'CREATE ROLE "{}" NOLOGIN NOINHERIT; '
                    'GRANT "{}" TO freqtrade '
                    'WITH INHERIT FALSE, SET TRUE; '
                    'GRANT DELETE, TRUNCATE ON {} TO "{}"'.format(
                        intermediary,
                        intermediary,
                        table_name,
                        intermediary,
                    )
                )
            )
        readiness = verify_schema(postgres_engine)
        assert readiness.ready is False
        assert any(
            "runtime reachable table privileges are not revoked" in problem
            for problem in readiness.problems
        )
        harden_attestation_access_boundary(postgres_engine)
        assert verify_schema(postgres_engine).ready is True
        with pytest.raises(DBAPIError):
            with postgres_engine.begin() as connection:
                connection.execute(
                    text('SET LOCAL ROLE "{}"'.format(intermediary))
                )
                connection.execute(text("TRUNCATE TABLE {}".format(table_name)))
    finally:
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    'REVOKE "{}" FROM freqtrade; DROP ROLE "{}"'.format(
                        intermediary, intermediary
                    )
                )
            )


def test_attestation_verifier_detects_indirect_membership_and_privileged_parent(
    postgres_engine,
) -> None:
    upgrade_database(postgres_engine)
    intermediary = "freqtrade_ai_membership_{}".format(uuid4().hex[:12])
    privileged = "freqtrade_ai_privileged_{}".format(uuid4().hex[:12])
    try:
        with postgres_engine.begin() as connection:
            connection.execute(
                text('CREATE ROLE "{}" NOLOGIN'.format(intermediary))
            )
            connection.execute(
                text('CREATE ROLE "{}" NOLOGIN CREATEDB'.format(privileged))
            )
            connection.execute(
                text(
                    'GRANT freqtrade_ai_attestor TO "{}"; '
                    'GRANT "{}" TO freqtrade; '
                    'GRANT "{}" TO freqtrade_ai_attestor'.format(
                        intermediary,
                        intermediary,
                        privileged,
                    )
                )
            )
        readiness = verify_schema(postgres_engine)
        assert readiness.ready is False
        assert any(
            "runtime role membership reaches attestor owner role" in problem
            for problem in readiness.problems
        )
        assert any(
            "attestor role membership reaches a privileged role" in problem
            for problem in readiness.problems
        )
    finally:
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    'REVOKE freqtrade_ai_attestor FROM "{}"; '
                    'REVOKE "{}" FROM freqtrade; '
                    'REVOKE "{}" FROM freqtrade_ai_attestor; '
                    'DROP ROLE "{}"; DROP ROLE "{}"'.format(
                        intermediary,
                        intermediary,
                        privileged,
                        intermediary,
                        privileged,
                    )
                )
            )


def test_attestation_hardening_removes_column_privilege_bypasses(
    postgres_engine,
) -> None:
    upgrade_database(postgres_engine)
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "GRANT SELECT (hmac_key) ON okx_demo_attestation_secrets "
                "TO freqtrade; "
                "GRANT UPDATE (revoked_at) ON okx_demo_attested_sessions "
                "TO freqtrade"
            )
        )
    readiness = verify_schema(postgres_engine)
    assert readiness.ready is False
    assert any(
        "runtime can read attestation secret column" in problem
        for problem in readiness.problems
    )
    assert any(
        "runtime column DML is not revoked" in problem
        for problem in readiness.problems
    )
    harden_attestation_access_boundary(postgres_engine)
    assert verify_schema(postgres_engine).ready is True
    with pytest.raises(DBAPIError):
        with postgres_engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE freqtrade"))
            connection.execute(
                text("SELECT hmac_key FROM okx_demo_attestation_secrets")
            )


@pytest.mark.parametrize(
    "constraint_name",
    [
        "approved_executions_no_submission_check",
        "approved_executions_claim_required_check",
        "approved_executions_approved_state_check",
        "risk_decisions_decision_check",
        "trade_intents_status_check",
    ],
)
def test_schema_verifier_detects_critical_check_tampering(
    postgres_engine, constraint_name
) -> None:
    upgrade_database(postgres_engine)
    table_name = (
        "approved_executions"
        if constraint_name.startswith("approved_executions")
        else "risk_decisions"
        if constraint_name.startswith("risk_decisions")
        else "trade_intents"
    )
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                'ALTER TABLE "{}" DROP CONSTRAINT "{}", '
                'ADD CONSTRAINT "{}" CHECK (TRUE)'.format(
                    table_name,
                    constraint_name,
                    constraint_name,
                )
            )
        )
    readiness = verify_schema(postgres_engine)
    assert readiness.ready is False
    assert any(
        "check definition mismatch: {}.{}".format(table_name, constraint_name)
        in problem
        for problem in readiness.problems
    )


def test_schema_verifier_detects_composite_lineage_fk_removal(
    postgres_engine,
) -> None:
    upgrade_database(postgres_engine)
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE approved_executions DROP CONSTRAINT "
                "approved_executions_decision_intent_fkey"
            )
        )
    readiness = verify_schema(postgres_engine)
    assert readiness.ready is False
    assert any(
        "missing foreign key: approved_executions" in problem
        for problem in readiness.problems
    )


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE approved_executions SET order_submission_authorized = TRUE",
        "UPDATE approved_executions SET claim_required = FALSE",
        "UPDATE approved_executions SET reserved_notional = 0",
        "UPDATE approved_executions SET intent_id = 'bad'",
        "UPDATE trade_intents SET side = 'hold'",
        "UPDATE trade_intents SET quantity = quantity + 1",
        "UPDATE trade_intents SET leverage = leverage + 1",
        "UPDATE trade_intents SET stop_loss = stop_loss - 1",
        "UPDATE trade_intents SET take_profit = take_profit + 1",
        "UPDATE trade_intents SET instrument_id = 'ETH-USDT-SWAP'",
        "UPDATE trade_intents SET canonical_hash = repeat('0', 64)",
        "UPDATE trade_intents SET policy_digest = repeat('0', 64)",
        "UPDATE trade_intents SET policy_digest = NULL, side = 'hold'",
        "UPDATE trade_intents SET status = 'FORGED'",
        "UPDATE risk_decisions SET decision = 'FORGED'",
    ],
)
def test_database_rejects_direct_authorization_tampering(
    postgres_engine, statement
) -> None:
    upgrade_database(postgres_engine)
    factory = create_session_factory(postgres_engine)
    lineage = _seed(factory)
    now = datetime.now(timezone.utc)
    with factory() as db:
        result = RiskChainService(db).evaluate(
            idempotency_key="tamper-target",
            request=_request(lineage, now, factory),
            policy=_policy(),
            now=now,
        )
    assert result.status == "APPROVED"
    with pytest.raises(DBAPIError):
        with postgres_engine.begin() as connection:
            connection.execute(text(statement))
    with factory() as db:
        assert db.get(ApprovedExecution, result.approved_execution_id).status == "ACTIVE"
        assert db.get(TradeIntent, result.trade_intent_id).status == "APPROVED"


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE okx_demo_trusted_snapshots SET digest = repeat('0', 64)",
        "DELETE FROM okx_demo_trusted_snapshots",
    ],
)
def test_trusted_snapshot_registry_is_database_immutable(
    postgres_engine, statement
) -> None:
    upgrade_database(postgres_engine)
    factory = create_session_factory(postgres_engine)
    request = _request(
        _seed(factory),
        datetime.now(timezone.utc),
        factory,
    )
    with pytest.raises(DBAPIError):
        with postgres_engine.begin() as connection:
            connection.execute(text(statement))
    with factory() as db:
        assert len(db.scalars(select(OkxDemoTrustedSnapshot)).all()) == 3
        assert set(request["snapshot_ids"].values()) == {
            row.snapshot_id
            for row in db.scalars(select(OkxDemoTrustedSnapshot)).all()
        }


@pytest.mark.parametrize(
    "statement",
    [
        (
            "INSERT INTO okx_demo_attested_sessions (session_id) "
            "VALUES ('forged-session')"
        ),
        (
            "INSERT INTO okx_demo_trusted_snapshots (snapshot_id) "
            "VALUES ('forged-snapshot')"
        ),
    ],
)
def test_runtime_role_cannot_directly_insert_attestation_rows(
    postgres_engine, statement
) -> None:
    upgrade_database(postgres_engine)
    with pytest.raises(DBAPIError):
        with postgres_engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE freqtrade"))
            connection.execute(text(statement))


def test_runtime_role_can_only_write_with_private_capability_functions(
    postgres_engine,
) -> None:
    upgrade_database(postgres_engine)
    factory = create_session_factory(postgres_engine)
    lineage = _seed(factory)
    now = datetime.now(timezone.utc)
    raw_request = _request(lineage, now)
    envelope = raw_request["snapshots"]["instrument"]
    capability = _issue_attested_session_capability(
        attestation_hmac_key=b"t" * 32,
        pinned_fingerprint_sha256="d" * 64,
        created_at=now,
        expires_at=now + timedelta(minutes=10),
    )
    normalized = _normalize_attested_snapshot(
        capability,
        kind="instrument",
        content=envelope["content"],
        observed_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    with factory.begin() as db:
        db.execute(text("SET LOCAL ROLE freqtrade"))
        row = _write_attested_snapshot(
            db, capability, normalized, now=now
        )
        database_id = row.database_id
        snapshot_id = row.snapshot_id
        session_id = row.attested_session_id
    with factory() as db:
        assert db.get(OkxDemoTrustedSnapshot, database_id).snapshot_id == snapshot_id
        assert db.get(OkxDemoAttestedSession, session_id).revoked_at is None


def test_runtime_role_cannot_self_mint_session_with_forged_hmac(
    postgres_engine,
) -> None:
    upgrade_database(postgres_engine)
    now = datetime.now(timezone.utc)
    capability = _issue_attested_session_capability(
        attestation_hmac_key=b"x" * 32,
        pinned_fingerprint_sha256="d" * 64,
        created_at=now,
        expires_at=now + timedelta(minutes=1),
    )
    content = {
        "execution_target": "OKX_DEMO",
        "source": "okx_demo_rest",
        "resource": "instrument",
        "stale": False,
        "expires_at": (now + timedelta(seconds=30)).isoformat(),
    }
    normalized = _normalize_attested_snapshot(
        capability,
        kind="instrument",
        content=content,
        observed_at=now,
        expires_at=now + timedelta(seconds=30),
    )
    factory = create_session_factory(postgres_engine)
    with pytest.raises(DBAPIError):
        with factory.begin() as db:
            db.execute(text("SET LOCAL ROLE freqtrade"))
            _write_attested_snapshot(db, capability, normalized, now=now)


def test_runtime_role_durable_revoke_blocks_old_session(
    postgres_engine,
) -> None:
    upgrade_database(postgres_engine)
    factory = create_session_factory(postgres_engine)
    lineage = _seed(factory)
    now = datetime.now(timezone.utc)
    raw_request = _request(lineage, now)
    envelope = raw_request["snapshots"]["instrument"]
    envelope["content"]["expires_at"] = (
        now + timedelta(seconds=30)
    ).isoformat()
    capability = _issue_attested_session_capability(
        attestation_hmac_key=b"t" * 32,
        pinned_fingerprint_sha256="d" * 64,
        created_at=now,
        expires_at=now + timedelta(minutes=1),
    )
    normalized = _normalize_attested_snapshot(
        capability,
        kind="instrument",
        content=envelope["content"],
        observed_at=now,
        expires_at=now + timedelta(seconds=30),
    )
    with factory.begin() as db:
        db.execute(text("SET LOCAL ROLE freqtrade"))
        row = _write_attested_snapshot(db, capability, normalized, now=now)
        session_id = row.attested_session_id
    with factory.begin() as db:
        db.execute(text("SET LOCAL ROLE freqtrade"))
        _revoke_attested_session(
            db,
            capability,
            reason="IDENTITY_DRIFT",
            revoked_at=now + timedelta(seconds=1),
        )
    with factory() as db:
        session = db.get(OkxDemoAttestedSession, session_id)
        assert session is not None
        assert session.revoke_reason == "IDENTITY_DRIFT"
        assert session.revoked_at is not None


def test_runtime_role_persists_unused_session_before_durable_revoke(
    postgres_engine,
) -> None:
    upgrade_database(postgres_engine)
    factory = create_session_factory(postgres_engine)
    now = datetime.now(timezone.utc)
    capability = _issue_attested_session_capability(
        attestation_hmac_key=b"t" * 32,
        pinned_fingerprint_sha256="d" * 64,
        created_at=now,
        expires_at=now + timedelta(minutes=1),
    )
    with factory.begin() as db:
        db.execute(text("SET LOCAL ROLE freqtrade"))
        persisted = _persist_attested_session(
            db,
            capability,
            now=now,
        )
        session_id = persisted.session_id
    with factory.begin() as db:
        db.execute(text("SET LOCAL ROLE freqtrade"))
        persisted = db.get(OkxDemoAttestedSession, session_id)
        assert persisted is not None
        assert persisted.revoked_at is None
        _revoke_attested_session(
            db,
            capability,
            reason="FACTORY_CLOSE",
            revoked_at=now + timedelta(seconds=1),
        )
    with factory() as db:
        persisted = db.get(OkxDemoAttestedSession, session_id)
        assert persisted is not None
        assert persisted.revoke_reason == "FACTORY_CLOSE"
        assert persisted.revoked_at is not None


def test_approved_execution_snapshot_foreign_keys_restrict_registry_delete(
    postgres_engine,
) -> None:
    upgrade_database(postgres_engine)
    factory = create_session_factory(postgres_engine)
    lineage = _seed(factory)
    now = datetime.now(timezone.utc)
    request = _request(lineage, now, factory)
    with factory() as db:
        result = RiskChainService(db).evaluate(
            idempotency_key="snapshot-fk-restrict",
            request=request,
            policy=_policy(),
            now=now,
        )
    with factory() as db:
        approved = db.get(ApprovedExecution, result.approved_execution_id)
        assert approved is not None
        snapshot_id = approved.instrument_snapshot_id
    with pytest.raises(DBAPIError):
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    "DROP TRIGGER okx_demo_trusted_snapshots_immutable "
                    "ON okx_demo_trusted_snapshots"
                )
            )
            connection.execute(
                text(
                    "DELETE FROM okx_demo_trusted_snapshots "
                    "WHERE snapshot_id = :snapshot_id"
                ),
                {"snapshot_id": snapshot_id},
            )


@pytest.mark.parametrize(
    "revoke_mode",
    ["IDENTITY_DRIFT", "FACTORY_CLOSE"],
)
def test_real_factory_revoke_blocks_idempotent_retry_and_releases_budget(
    postgres_engine,
    monkeypatch,
    revoke_mode,
) -> None:
    upgrade_database(postgres_engine)
    factory = create_session_factory(postgres_engine)
    lineage = _seed(factory)
    now = datetime.now(timezone.utc)
    current = [now]
    expected_fingerprint = "d" * 64
    monkeypatch.setattr(read_boundary, "_utc_now", lambda: current[0])
    monkeypatch.setattr(read_boundary, "run_preflight", lambda environment: None)
    monkeypatch.setattr(
        read_boundary,
        "require_pinned_account_fingerprint",
        lambda environment: expected_fingerprint,
    )
    monkeypatch.setattr(
        read_boundary,
        "_build_demo_authorization_headers",
        lambda *args, **kwargs: {
            "OK-ACCESS-KEY": "temporary-key",
            "OK-ACCESS-SIGN": "signature",
            "OK-ACCESS-TIMESTAMP": "2026-07-27T00:00:00.000Z",
            "OK-ACCESS-PASSPHRASE": "temporary-passphrase",
        },
    )

    class DriftTransport:
        def get(self, **kwargs):
            return OkxReadHttpResponse(
                status_code=200,
                payload={
                    "code": "0",
                    "msg": "",
                    "data": [
                        {
                            "uid": "drifted",
                            "mainUid": "drifted",
                            "acctLv": "2",
                            "posMode": "net_mode",
                            "perm": "read_only,trade",
                        }
                    ],
                },
                received_at=current[0],
            )

    monkeypatch.setattr(
        read_boundary,
        "UrllibOkxReadTransport",
        lambda: DriftTransport(),
    )
    client = read_boundary.create_attested_okx_demo_read_adapter(
        {
            "FREQTRADE_AI_EXECUTION_TARGET": "OKX_DEMO",
            "FREQTRADE_AI_ALLOW_REAL_FUNDS": "false",
            "FREQTRADE_AI_OKX_DEMO_REST_URL": "https://openapi.okx.com",
            "OKX_DEMO_API_KEY": "temporary-key",
            "OKX_DEMO_API_SECRET": "temporary-secret",
            "OKX_DEMO_API_PASSPHRASE": "temporary-passphrase",
            "OKX_DEMO_ACCOUNT_FINGERPRINT": expected_fingerprint,
            "FREQTRADE_AI_OKX_DEMO_ATTESTATION_PROOF_KEY": "74" * 32,
        }
    )
    request = _request(lineage, now)
    envelopes = request.pop("snapshots")
    snapshot_ids = {}
    with factory.begin() as db:
        db.execute(text("SET LOCAL ROLE freqtrade"))
        for kind, envelope in envelopes.items():
            envelope["content"]["expires_at"] = (
                now + timedelta(seconds=30)
            ).isoformat()
            row = client._persist_risk_snapshot(
                db,
                kind=kind,
                content=envelope["content"],
                observed_at=now,
                snapshot_expires_at=now + timedelta(seconds=30),
            )
            snapshot_ids[kind] = row.snapshot_id
            session_id = row.attested_session_id
    request["snapshot_ids"] = snapshot_ids
    with factory() as db:
        result = RiskChainService(db).evaluate(
            idempotency_key="factory-revoke-{}".format(revoke_mode.lower()),
            request=request,
            policy=_policy(),
            now=now,
        )
    assert result.status == "APPROVED"
    with factory() as db:
        approved = db.get(ApprovedExecution, result.approved_execution_id)
        budget = db.get(RiskBudget, OKX_DEMO_TARGET_ID)
        assert approved is not None and budget is not None
        reserved_before = budget.reserved_notional
        permission_reservation = approved.reserved_notional
    current[0] = now + timedelta(seconds=1)
    if revoke_mode == "IDENTITY_DRIFT":
        with pytest.raises(OkxReadAdapterError) as blocked:
            client.account_config()
        assert blocked.value.kind == "IDENTITY_DRIFT"
    else:
        client.close()
    with factory() as db:
        session = db.get(OkxDemoAttestedSession, session_id)
        assert session is not None
        assert session.revoke_reason == revoke_mode
        assert session.revoked_at is not None
    with factory() as db:
        retry = RiskChainService(db).evaluate(
            idempotency_key="factory-revoke-{}".format(revoke_mode.lower()),
            request=request,
            policy=_policy(),
            now=now + timedelta(seconds=2),
        )
    assert retry.status == "BLOCKED"
    assert retry.approved_execution_id is None
    with factory() as db:
        budget = db.get(RiskBudget, OKX_DEMO_TARGET_ID)
        assert budget is not None
        assert budget.reserved_notional == reserved_before - permission_reservation


@pytest.mark.parametrize(
    ("session_state", "expected_status"),
    [("REVOKED", "BLOCKED"), ("EXPIRED", "EXPIRED")],
)
def test_claim_active_approval_directly_invalidates_stale_session_atomically(
    postgres_engine,
    session_state,
    expected_status,
) -> None:
    upgrade_database(postgres_engine)
    factory = create_session_factory(postgres_engine)
    lineage = _seed(factory)
    now = datetime.now(timezone.utc)
    capabilities = []
    request = _request(
        lineage,
        now,
        factory,
        capability_sink=capabilities,
    )
    with factory() as db:
        result = RiskChainService(db).evaluate(
            idempotency_key="pg-direct-claim-{}".format(session_state.lower()),
            request=request,
            policy=_policy(),
            now=now,
        )
    with factory() as db:
        approved = db.get(ApprovedExecution, result.approved_execution_id)
        assert approved is not None
        snapshot = db.scalars(
            select(OkxDemoTrustedSnapshot).where(
                OkxDemoTrustedSnapshot.snapshot_id
                == approved.instrument_snapshot_id
            )
        ).one()
        budget = db.get(RiskBudget, OKX_DEMO_TARGET_ID)
        assert budget is not None
        session_id = snapshot.attested_session_id
        intent_id = approved.trade_intent_id
        decision_id = approved.risk_decision_id
        reserved_before = budget.reserved_notional
        positions_before = budget.approved_positions
        permission_reservation = approved.reserved_notional
    if session_state == "REVOKED":
        with factory.begin() as db:
            db.execute(text("SET LOCAL ROLE freqtrade"))
            _revoke_attested_session(
                db,
                capabilities[0],
                reason="IDENTITY_DRIFT",
                revoked_at=now + timedelta(seconds=1),
            )
        claim_now = now + timedelta(seconds=2)
    else:
        claim_now = now + timedelta(minutes=11)
    with factory() as db:
        assert (
            RiskChainService(db).claim_active_approval(
                result.approved_execution_id,
                now=claim_now,
            )
            is None
        )
    with factory() as db:
        intent = db.get(TradeIntent, intent_id)
        decision = db.get(RiskDecision, decision_id)
        budget = db.get(RiskBudget, OKX_DEMO_TARGET_ID)
        assert db.get(ApprovedExecution, result.approved_execution_id) is None
        assert intent is not None and intent.status == expected_status
        assert decision is not None and decision.decision == expected_status
        assert budget is not None
        assert budget.reserved_notional == reserved_before - permission_reservation
        assert budget.approved_positions == positions_before - 1


def test_security_definer_rejects_wrong_pinned_account(
    postgres_engine,
) -> None:
    upgrade_database(postgres_engine)
    factory = create_session_factory(postgres_engine)
    lineage = _seed(factory)
    now = datetime.now(timezone.utc)
    raw_request = _request(lineage, now)
    instrument = raw_request["snapshots"]["instrument"]
    capability = _issue_attested_session_capability(
        attestation_hmac_key=b"t" * 32,
        pinned_fingerprint_sha256="c" * 64,
        created_at=now,
        expires_at=now + timedelta(minutes=10),
    )
    normalized = _normalize_attested_snapshot(
        capability,
        kind="instrument",
        content=instrument["content"],
        observed_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    with factory.begin() as db:
        _write_attested_snapshot(db, capability, normalized, now=now)
    forged_account = {
        "execution_target": "OKX_DEMO",
        "source": "okx_demo_rest",
        "resource": "account",
        "stale": False,
        "authenticated": True,
        "pinned_account_fingerprint": "b" * 64,
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
    }
    with pytest.raises(DBAPIError):
        with postgres_engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE freqtrade"))
            connection.execute(
                text(
                    "SELECT write_okx_demo_trusted_snapshot("
                    "CAST(:session_id AS text), CAST(:proof AS text), "
                    "CAST(:snapshot_id AS text), 'account', "
                    "CAST(:content AS jsonb), CAST(:digest AS text), "
                    ":observed_at, :expires_at)"
                ),
                {
                    "session_id": capability._identity.session_id,
                    "proof": capability._proof,
                    "snapshot_id": "account:" + "0" * 48,
                    "content": json.dumps(forged_account),
                    "digest": "0" * 64,
                    "observed_at": now,
                    "expires_at": now + timedelta(minutes=5),
                },
            )


def test_revoked_or_expired_attested_session_blocks_authorization(
    postgres_engine,
) -> None:
    upgrade_database(postgres_engine)
    factory = create_session_factory(postgres_engine)
    now = datetime.now(timezone.utc)
    lineage = _seed(factory)
    revoked_request = _request(lineage, now, factory)
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE okx_demo_attested_sessions "
                "SET revoked_at = :revoked_at, revoke_reason = 'IDENTITY_DRIFT'"
            ),
            {"revoked_at": now},
        )
    with factory() as db:
        revoked = RiskChainService(db).evaluate(
            idempotency_key="revoked-attestation",
            request=revoked_request,
            policy=_policy(),
            now=now,
        )
    assert revoked.status == "BLOCKED"

    expired_request = _request(lineage, now, factory)
    with factory() as db:
        expired = RiskChainService(db).evaluate(
            idempotency_key="expired-attestation-session",
            request=expired_request,
            policy=_policy(),
            now=now + timedelta(minutes=11),
        )
    assert expired.status == "BLOCKED"


def test_legacy_authorization_row_cannot_become_active_approval(
    postgres_engine,
) -> None:
    upgrade_database(postgres_engine)
    factory = create_session_factory(postgres_engine)
    with factory.begin() as db:
        ensure_execution_scope_catalog(db)
        intent = TradeIntent(
            execution_target_id=OKX_DEMO_TARGET_ID,
            authorization_schema_version="LEGACY",
            client_order_id="LEGACY1",
            status="UNKNOWN_LEGACY",
            request_snapshot={},
        )
        db.add(intent)
        db.flush()
        decision = RiskDecision(
            execution_target_id=OKX_DEMO_TARGET_ID,
            trade_intent_id=intent.id,
            authorization_schema_version="LEGACY",
            policy_digest=None,
            decision="BLOCKED",
            policy_version="legacy",
            evidence_snapshot={},
        )
        db.add(decision)
        db.flush()
        legacy_intent_id = intent.id
        legacy_decision_id = decision.id

    with pytest.raises(DBAPIError):
        with factory.begin() as db:
            db.add(
                ApprovedExecution(
                    execution_target_id=OKX_DEMO_TARGET_ID,
                    trade_intent_id=legacy_intent_id,
                    risk_decision_id=legacy_decision_id,
                    intent_id="0" * 64,
                    client_order_id="LEGACY1",
                    authorization_schema_version="RISK_V1",
                    canonical_hash="0" * 64,
                    policy_digest="0" * 64,
                    approved_payload_hash="0" * 64,
                    decision="APPROVED",
                    intent_status="APPROVED",
                    reserved_notional=1,
                    order_submission_authorized=False,
                    claim_required=True,
                    status="ACTIVE",
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                    evidence_snapshot={},
                )
            )


def test_postgresql_budget_lock_allows_only_one_concurrent_permission(
    postgres_engine,
) -> None:
    upgrade_database(postgres_engine)
    factory = create_session_factory(postgres_engine)
    lineage = _seed(factory)
    now = datetime.now(timezone.utc)
    request = _request(lineage, now, factory)
    barrier = Barrier(2)
    results = []
    failures = []
    mutex = Lock()

    def worker(key: str) -> None:
        try:
            barrier.wait(timeout=5)
            with factory() as db:
                result = RiskChainService(db).evaluate(
                    idempotency_key=key,
                    request=request,
                    policy=_policy(),
                    now=now,
                )
            with mutex:
                results.append(result.status)
        except Exception as exc:  # pragma: no cover - assertion reports the exact type
            with mutex:
                failures.append(type(exc).__name__)

    threads = [
        Thread(target=worker, args=("concurrent-{}".format(index),))
        for index in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert failures == []
    assert sorted(results) == ["APPROVED", "REJECTED"]
    with factory() as db:
        budget = db.get(RiskBudget, OKX_DEMO_TARGET_ID)
        assert budget.reserved_notional == 600
        assert budget.approved_positions == 1
        assert len(db.scalars(select(ApprovedExecution)).all()) == 1
        assert len(db.scalars(select(TradeIntent)).all()) == 2


def test_postgresql_concurrent_idempotent_retry_reads_one_chain(
    postgres_engine,
) -> None:
    upgrade_database(postgres_engine)
    factory = create_session_factory(postgres_engine)
    lineage = _seed(factory)
    now = datetime.now(timezone.utc)
    request = _request(lineage, now, factory)
    barrier = Barrier(2)
    results = []
    failures = []
    mutex = Lock()

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            with factory() as db:
                result = RiskChainService(db).evaluate(
                    idempotency_key="same-concurrent-request",
                    request=request,
                    policy=_policy(),
                    now=now,
                )
            with mutex:
                results.append(result)
        except Exception as exc:  # pragma: no cover - assertion reports the exact type
            with mutex:
                failures.append(type(exc).__name__)

    threads = [Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert failures == []
    assert len(results) == 2
    assert results[0] == results[1]
    with factory() as db:
        budget = db.get(RiskBudget, OKX_DEMO_TARGET_ID)
        assert budget.reserved_notional == 600
        assert budget.approved_positions == 1
        assert len(db.scalars(select(ApprovedExecution)).all()) == 1
        assert len(db.scalars(select(TradeIntent)).all()) == 1
