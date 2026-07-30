import os
from decimal import Decimal
from threading import Barrier, Lock, Thread

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import DataError, IntegrityError

from app.core.exceptions import ConfigurationError
from app.db.migrations import (
    EARLY_TARGET_LINEAGE_VERSION,
    FILL_SNAPSHOT_REPEAT_BASE_VERSION,
    LEGACY_SCHEMA_VERSION,
    PREVIOUS_SCHEMA_VERSION,
    RESEARCH_RECOVERY_BASE_VERSION,
    SCHEMA_VERSION,
    STRATEGY_DEPLOYMENT_BASE_VERSION,
    TARGET_LINEAGE_BASE_VERSION,
    VERSION_TABLE,
    upgrade_database,
    verify_schema,
)
from app.db.session import create_database_engine, create_session_factory
from app.models import BacktestRun, Base, ResearchJob, Strategy, StrategyGenerationRun
from app.models.execution_lineage import (
    LOCAL_DRY_RUN_SCOPE_ID,
    OKX_DEMO_TARGET_ID,
    TradeIntent,
    UNKNOWN_LEGACY_SCOPE_ID,
)
from app.repositories import (
    BacktestRepository,
    ExecutionLineageRepository,
    ResearchJobLinkageBlocked,
    ResearchJobRepository,
    StrategyGenerationRunRepository,
    StrategyRepository,
    ensure_execution_scope_catalog,
)
from app.schemas import BacktestRunCreate, StrategyCreate, StrategyVersionCreate
from app.schemas.strategy_generation_run import StrategyGenerationRunCreate


POSTGRES_WORKER_URL = os.environ.get("POSTGRES_WORKER_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_WORKER_URL,
    reason="POSTGRES_WORKER_URL is required for the PostgreSQL worker gate",
)


@pytest.fixture()
def postgres_engine():
    assert POSTGRES_WORKER_URL is not None
    engine = create_database_engine(POSTGRES_WORKER_URL)
    _reset_schema(engine)
    yield engine
    _reset_schema(engine)
    upgrade_database(engine)
    engine.dispose()


def _reset_schema(engine) -> None:
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))


def _detach_signal_evaluations_from_full_chain(connection) -> None:
    connection.execute(
        text(
            "ALTER TABLE full_chain_runs "
            "DROP CONSTRAINT IF EXISTS "
            "full_chain_runs_signal_evaluation_id_fkey"
        )
    )


def test_fill_snapshot_repeat_upgrade_drops_only_cross_generation_unique(
    postgres_engine,
) -> None:
    assert upgrade_database(postgres_engine) == SCHEMA_VERSION
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE okx_demo_fill_snapshots ADD CONSTRAINT "
                "okx_demo_fill_snapshots_fill_unique UNIQUE "
                "(execution_target_id, exchange_fill_id)"
            )
        )
        connection.execute(text(f"DELETE FROM {VERSION_TABLE}"))
        connection.execute(
            text(f"INSERT INTO {VERSION_TABLE} (version) VALUES (:version)"),
            {"version": FILL_SNAPSHOT_REPEAT_BASE_VERSION},
        )

    assert upgrade_database(postgres_engine) == SCHEMA_VERSION
    constraints = {
        item["name"]
        for item in inspect(postgres_engine).get_unique_constraints(
            "okx_demo_fill_snapshots"
        )
    }
    assert "okx_demo_fill_snapshots_fill_unique" not in constraints
    assert "okx_demo_fill_snapshots_event_unique" in constraints


def test_strategy_deployment_queue_upgrades_from_previous_schema(
    postgres_engine,
) -> None:
    assert upgrade_database(postgres_engine) == SCHEMA_VERSION
    with postgres_engine.begin() as connection:
        _detach_signal_evaluations_from_full_chain(connection)
        connection.execute(text("DROP TABLE signal_evaluations"))
        connection.execute(text("DROP TABLE strategy_deployments"))
        connection.execute(text(f"DELETE FROM {VERSION_TABLE}"))
        connection.execute(
            text(f"INSERT INTO {VERSION_TABLE} (version) VALUES (:version)"),
            {"version": STRATEGY_DEPLOYMENT_BASE_VERSION},
        )

    assert upgrade_database(postgres_engine) == SCHEMA_VERSION
    readiness = verify_schema(postgres_engine)
    assert readiness.ready is True
    assert readiness.problems == ()
    with postgres_engine.connect() as connection:
        trigger_names = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT tgname FROM pg_trigger "
                    "WHERE tgname IN ("
                    "'strategy_validation_plans_immutable', "
                    "'strategy_validation_windows_immutable') "
                    "AND NOT tgisinternal"
                )
            )
        }
        plan_table_update, plan_status_update, plan_digest_update = connection.execute(
            text(
                "SELECT "
                "has_table_privilege('freqtrade', "
                "'strategy_validation_plans', 'UPDATE'), "
                "has_column_privilege('freqtrade', "
                "'strategy_validation_plans', 'status', 'UPDATE'), "
                "has_column_privilege('freqtrade', "
                "'strategy_validation_plans', 'plan_digest', 'UPDATE')"
            )
        ).one()
        execution_unique = {
            frozenset(item["column_names"])
            for item in inspect(connection).get_unique_constraints(
                "strategy_validation_windows"
            )
        }
    assert trigger_names == {
        "strategy_validation_plans_immutable",
        "strategy_validation_windows_immutable",
    }
    assert plan_table_update is False
    assert plan_status_update is True
    assert plan_digest_update is False
    assert frozenset({"execution_id"}) in execution_unique

    inspector = inspect(postgres_engine)
    assert {"strategy_deployments", "signal_evaluations"}.issubset(
        inspector.get_table_names()
    )
    unique_constraints = {
        item["name"]
        for item in inspector.get_unique_constraints("signal_evaluations")
    }
    assert "signal_evaluations_deployment_candle_unique" in unique_constraints
    indexes = {
        item["name"]: item
        for item in inspector.get_indexes("signal_evaluations")
    }
    single_consumer = indexes["signal_evaluations_single_consumer_idx"]
    assert single_consumer["unique"] is True
    assert "status" in str(single_consumer.get("dialect_options"))


def test_validation_matrix_fresh_and_upgrade_schema_match_orm(
    postgres_engine,
) -> None:
    assert upgrade_database(postgres_engine) == SCHEMA_VERSION
    assert {
        "strategy_validation_plans",
        "strategy_validation_windows",
    }.issubset(set(inspect(postgres_engine).get_table_names()))

    with postgres_engine.begin() as connection:
        connection.execute(text("DROP TABLE strategy_validation_windows"))
        connection.execute(text("DROP TABLE strategy_validation_plans"))
        connection.execute(text(f"DELETE FROM {VERSION_TABLE}"))
        connection.execute(
            text(f"INSERT INTO {VERSION_TABLE} (version) VALUES (:version)"),
            {"version": RESEARCH_RECOVERY_BASE_VERSION},
        )

    assert upgrade_database(postgres_engine) == SCHEMA_VERSION
    readiness = verify_schema(postgres_engine)
    assert readiness.ready is True
    assert readiness.problems == ()


def _create_frozen_pre_lineage_research_jobs(connection) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE research_jobs (
                id BIGSERIAL PRIMARY KEY,
                job_type VARCHAR(80) NOT NULL,
                operation VARCHAR(120) NOT NULL,
                idempotency_key_digest VARCHAR(64) NOT NULL,
                request_hash VARCHAR(64) NOT NULL,
                request_payload JSON NOT NULL,
                status VARCHAR(32) NOT NULL DEFAULT 'PENDING',
                stage VARCHAR(80) NOT NULL DEFAULT 'QUEUED',
                lease_owner VARCHAR(160),
                lease_token VARCHAR(64),
                lease_expires_at TIMESTAMPTZ,
                heartbeat_at TIMESTAMPTZ,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 1,
                cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
                provider_attempted_at TIMESTAMPTZ,
                provider_completed_at TIMESTAMPTZ,
                strategy_generation_run_id BIGINT
                    REFERENCES strategy_generation_runs(id) ON DELETE SET NULL,
                strategy_id BIGINT REFERENCES strategies(id) ON DELETE SET NULL,
                strategy_version_id BIGINT
                    REFERENCES strategy_versions(id) ON DELETE SET NULL,
                backtest_run_id BIGINT REFERENCES backtest_runs(id) ON DELETE SET NULL,
                backtest_task_id BIGINT REFERENCES backtest_tasks(id) ON DELETE SET NULL,
                backtest_result_id BIGINT REFERENCES backtest_results(id) ON DELETE SET NULL,
                strategy_score_id BIGINT REFERENCES strategy_scores(id) ON DELETE SET NULL,
                evidence_snapshot JSON NOT NULL DEFAULT '{}',
                error_message TEXT,
                started_at TIMESTAMPTZ,
                completed_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                CONSTRAINT research_jobs_status_check CHECK (
                    status IN ('PENDING', 'RUNNING', 'SUCCESS', 'FAILED',
                    'BLOCKED', 'CANCELLED', 'STALE')
                ),
                CONSTRAINT research_jobs_attempt_count_check CHECK (attempt_count >= 0),
                CONSTRAINT research_jobs_max_attempts_check CHECK (max_attempts >= 1),
                CONSTRAINT frozen_global_idempotency_name_is_not_trusted
                    UNIQUE (operation, idempotency_key_digest)
            )
            """
        )
    )
    connection.execute(
        text(
            "CREATE INDEX research_jobs_claim_idx "
            "ON research_jobs (status, created_at, id)"
        )
    )
    connection.execute(
        text(
            "CREATE INDEX research_jobs_lease_expiry_idx "
            "ON research_jobs (status, lease_expires_at)"
        )
    )


def _create_frozen_early_target_lineage_schema(connection) -> None:
    """Reproduce the published 710afdf ``20260727_01`` contract."""

    Base.metadata.create_all(bind=connection)
    connection.execute(
        text(
            "ALTER TABLE trade_intents "
            "DROP CONSTRAINT trade_intents_scope_contract_check, "
            "ALTER COLUMN authorization_schema_version SET DEFAULT 'LEGACY'"
        )
    )
    connection.execute(
        text(
            "ALTER TABLE execution_scopes "
            "DROP CONSTRAINT execution_scopes_known_contract_check, "
            "DROP COLUMN exchange_capable, "
            "DROP COLUMN order_submission_authorized, "
            "ADD CONSTRAINT execution_scopes_known_contract_check CHECK ("
            "(scope_id = 'OKX_DEMO' AND scope_kind = 'EXCHANGE_TARGET' "
            "AND executable = TRUE AND exchange_writes = TRUE) OR "
            "(scope_id = 'LOCAL_DRY_RUN' AND scope_kind = 'NON_EXCHANGE' "
            "AND executable = TRUE AND exchange_writes = FALSE) OR "
            "(scope_id = 'UNKNOWN_LEGACY' AND scope_kind = 'LEGACY' "
            "AND executable = FALSE AND exchange_writes = FALSE))"
        )
    )
    connection.execute(
        text(
            """
            INSERT INTO execution_scopes
                (scope_id, scope_kind, executable, exchange_writes)
            VALUES
                ('OKX_DEMO', 'EXCHANGE_TARGET', TRUE, TRUE),
                ('LOCAL_DRY_RUN', 'NON_EXCHANGE', TRUE, FALSE),
                ('UNKNOWN_LEGACY', 'LEGACY', FALSE, FALSE)
            """
        )
    )
    for table_name in ("trade_intents", "exchange_orders"):
        connection.execute(
            text(
                f"ALTER TABLE {table_name} "
                f"DROP CONSTRAINT {table_name}_client_order_id_format_check, "
                f"ADD CONSTRAINT {table_name}_client_order_id_length_check "
                "CHECK (length(client_order_id) BETWEEN 1 AND 32)"
            )
        )
    connection.execute(
        text(
            "ALTER TABLE execution_manifests "
            "DROP CONSTRAINT execution_manifests_authorization_check, "
            "ADD CONSTRAINT execution_manifests_legacy_not_executable_check "
            "CHECK (execution_scope_id <> 'UNKNOWN_LEGACY' OR executable_evidence = FALSE)"
        )
    )
    connection.execute(
        text(
            """
            INSERT INTO execution_manifests (
                execution_scope_id, manifest_kind, schema_version, artifact_path,
                artifact_sha256, database_ids, executable_evidence
            ) VALUES (
                'OKX_DEMO', 'frozen-early-lineage', '1', '/tmp/frozen.json',
                :sha, '{}', TRUE
            )
            """
        ),
        {"sha": "a" * 64},
    )
    connection.execute(
        text(
            f"CREATE TABLE {VERSION_TABLE} ("
            "version VARCHAR(64) PRIMARY KEY, "
            "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
    )
    connection.execute(
        text(f"INSERT INTO {VERSION_TABLE} (version) VALUES (:version)"),
        {"version": EARLY_TARGET_LINEAGE_VERSION},
    )


def test_incremental_worker_migration_preserves_existing_runtime_rows(postgres_engine) -> None:
    old_tables = [
        table
        for table in Base.metadata.tables.values()
        if table.name
        not in {
            "approved_executions",
            "exchange_fills",
            "exchange_orders",
            "exchange_positions",
            "execution_manifests",
            "full_chain_runs",
            "full_chain_signal_snapshots",
            "full_chain_stage_runs",
            "okx_order_write_attempts",
            "okx_order_writer_leases",
            "okx_demo_account_snapshots",
            "okx_demo_exchange_events",
            "okx_demo_fill_snapshots",
            "okx_demo_order_snapshots",
            "okx_demo_position_snapshots",
            "okx_demo_reconciliation_states",
            "okx_demo_recovery_batches",
            "okx_demo_recovery_grants",
            "reconciliation_runs",
            "research_job_attempts",
            "research_jobs",
            "research_worker_control",
            "risk_decisions",
            "risk_budgets",
            "strategy_candidate_approvals",
            "trade_intents",
        }
    ]
    Base.metadata.create_all(postgres_engine, tables=old_tables)
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                f"CREATE TABLE {VERSION_TABLE} ("
                "version VARCHAR(64) PRIMARY KEY, "
                "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
        )
        connection.execute(
            text(f"INSERT INTO {VERSION_TABLE} (version) VALUES (:version)"),
            {"version": LEGACY_SCHEMA_VERSION},
        )

    session_factory = create_session_factory(postgres_engine)
    with session_factory() as db:
        strategy = StrategyRepository(db).create(
            StrategyCreate(name="Preserved migration strategy", slug="preserved-migration-strategy")
        )
        strategy_id = strategy.id

    assert upgrade_database(postgres_engine) == SCHEMA_VERSION
    readiness = verify_schema(postgres_engine)
    assert readiness.ready is True
    assert readiness.schema_version == SCHEMA_VERSION
    recovery_fks = inspect(postgres_engine).get_foreign_keys(
        "okx_order_write_attempts"
    )
    assert any(
        foreign_key.get("constrained_columns")
        == ["recovery_grant_database_id"]
        and foreign_key.get("referred_table")
        == "okx_demo_recovery_grants"
        and (foreign_key.get("options") or {}).get("ondelete")
        == "RESTRICT"
        for foreign_key in recovery_fks
    )
    assert {"research_jobs", "research_worker_control"}.issubset(
        set(inspect(postgres_engine).get_table_names())
    )
    with postgres_engine.connect() as connection:
        status_constraint = connection.execute(
            text(
                "SELECT pg_get_constraintdef(oid) "
                "FROM pg_constraint "
                "WHERE conname = 'research_jobs_status_check'"
            )
        ).scalar_one()
    assert "AWAITING_APPROVAL" in status_constraint
    with session_factory() as db:
        preserved = db.get(Strategy, strategy_id)
        assert preserved is not None
        assert preserved.slug == "preserved-migration-strategy"


def test_published_20260727_01_upgrades_atomically_to_20260727_02(
    postgres_engine,
) -> None:
    with postgres_engine.begin() as connection:
        _create_frozen_early_target_lineage_schema(connection)

    assert upgrade_database(postgres_engine) == SCHEMA_VERSION
    readiness = verify_schema(postgres_engine)
    assert readiness.ready is True
    assert readiness.problems == ()

    with postgres_engine.connect() as connection:
        scopes = {
            row.scope_id: row
            for row in connection.execute(
                text(
                    "SELECT scope_id, exchange_capable, executable, exchange_writes, "
                    "order_submission_authorized FROM execution_scopes"
                )
            )
        }
        assert scopes["OKX_DEMO"].exchange_capable is True
        assert scopes["OKX_DEMO"].executable is False
        assert scopes["OKX_DEMO"].exchange_writes is False
        assert scopes["OKX_DEMO"].order_submission_authorized is False
        assert scopes["LOCAL_DRY_RUN"].executable is True
        assert connection.execute(
            text(
                "SELECT executable_evidence FROM execution_manifests "
                "WHERE manifest_kind = 'frozen-early-lineage'"
            )
        ).scalar_one() is False
        assert connection.execute(
            text(f"SELECT version FROM {VERSION_TABLE} ORDER BY version")
        ).scalars().all() == [EARLY_TARGET_LINEAGE_VERSION, SCHEMA_VERSION]


def test_published_20260727_01_upgrade_rolls_back_on_incompatible_data(
    postgres_engine,
) -> None:
    with postgres_engine.begin() as connection:
        _create_frozen_early_target_lineage_schema(connection)
        connection.execute(
            text(
                """
                INSERT INTO trade_intents (
                    execution_target_id, client_order_id, instrument_id, side,
                    position_side, order_type, quantity, status, request_snapshot
                ) VALUES (
                    'OKX_DEMO', 'old-id-with-hyphen', 'BTC-USDT-SWAP', 'buy',
                    'long', 'market', 1, 'PENDING_RISK', '{}'
                )
                """
            )
        )

    with pytest.raises(ConfigurationError, match="Database migration failed"):
        upgrade_database(postgres_engine)

    with postgres_engine.connect() as connection:
        columns = {
            column["name"]
            for column in inspect(connection).get_columns("execution_scopes")
        }
        assert "exchange_capable" not in columns
        assert "order_submission_authorized" not in columns
        assert connection.execute(
            text(
                "SELECT executable AND exchange_writes FROM execution_scopes "
                "WHERE scope_id = 'OKX_DEMO'"
            )
        ).scalar_one() is True
        assert connection.execute(
            text(f"SELECT version FROM {VERSION_TABLE}")
        ).scalar_one() == EARLY_TARGET_LINEAGE_VERSION
        assert connection.execute(
            text(
                "SELECT executable_evidence FROM execution_manifests "
                "WHERE manifest_kind = 'frozen-early-lineage'"
            )
        ).scalar_one() is True


def test_cleanup_migration_drops_only_empty_retired_debug_table(postgres_engine) -> None:
    Base.metadata.create_all(postgres_engine)
    with postgres_engine.begin() as connection:
        connection.execute(text('CREATE TABLE debug_mvp_seed_payloads (endpoint_key VARCHAR(120) PRIMARY KEY)'))
        connection.execute(
            text(
                f"CREATE TABLE {VERSION_TABLE} ("
                "version VARCHAR(64) PRIMARY KEY, "
                "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
        )
        connection.execute(
            text(f"INSERT INTO {VERSION_TABLE} (version) VALUES (:version)"),
            {"version": PREVIOUS_SCHEMA_VERSION},
        )

    assert upgrade_database(postgres_engine) == SCHEMA_VERSION
    assert "debug_mvp_seed_payloads" not in inspect(postgres_engine).get_table_names()
    assert verify_schema(postgres_engine).ready is True


def test_postgresql_two_workers_claim_only_one_global_job(postgres_engine) -> None:
    assert upgrade_database(postgres_engine) == SCHEMA_VERSION
    session_factory = create_session_factory(postgres_engine)
    with session_factory() as db:
        repository = ResearchJobRepository(db)
        repository.get_control()
        for index in range(2):
            repository.create(
                job_type="deepseek_backtest",
                operation="postgres-worker-test",
                idempotency_key_digest=f"digest-{index}",
                request_hash=f"hash-{index}",
                request_payload={"prompt_summary": f"job-{index}"},
            )

    barrier = Barrier(2)
    result_lock = Lock()
    claimed_ids = []
    failures = []

    def claim(owner: str) -> None:
        try:
            barrier.wait(timeout=5)
            with session_factory() as db:
                job = ResearchJobRepository(db).claim_next(owner=owner, lease_seconds=60)
                with result_lock:
                    claimed_ids.append(None if job is None else job.id)
        except Exception as exc:  # pragma: no cover - surfaced in assertion below
            with result_lock:
                failures.append(exc)

    threads = [Thread(target=claim, args=(f"worker-{index}",)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert failures == []
    assert all(not thread.is_alive() for thread in threads)
    assert len([job_id for job_id in claimed_ids if job_id is not None]) == 1
    assert claimed_ids.count(None) == 1
    with session_factory() as db:
        jobs = ResearchJobRepository(db).list()
        assert [job.status for job in jobs].count("RUNNING") == 1
        assert [job.status for job in jobs].count("PENDING") == 1
        running = next(job for job in jobs if job.status == "RUNNING")
        assert running.attempt_count == 1
        assert running.lease_token is not None


def test_target_lineage_migration_marks_existing_rows_unknown_legacy(postgres_engine) -> None:
    Base.metadata.create_all(postgres_engine)
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO execution_scopes (
                    scope_id, scope_kind, exchange_capable, executable,
                    exchange_writes, order_submission_authorized
                ) VALUES
                    (
                        'OKX_DEMO', 'EXCHANGE_TARGET',
                        TRUE, FALSE, FALSE, FALSE
                    ),
                    (
                        'LOCAL_DRY_RUN', 'NON_EXCHANGE',
                        FALSE, TRUE, FALSE, FALSE
                    ),
                    (
                        'UNKNOWN_LEGACY', 'LEGACY',
                        FALSE, FALSE, FALSE, FALSE
                    )
                """
            )
        )
    session_factory = create_session_factory(postgres_engine)
    with session_factory() as db:
        strategy_repository = StrategyRepository(db)
        strategy = strategy_repository.create(
            StrategyCreate(name="Legacy target migration", slug="legacy-target-migration")
        )
        version = strategy_repository.create_version(
            StrategyVersionCreate(
                strategy_id=strategy.id,
                blueprint={},
                generated_code="class LegacyStrategy: pass",
                code_hash="legacy-code-hash",
                file_path="/tmp/legacy.py",
            )
        )
        assert version is not None
        generation_run = StrategyGenerationRunRepository(db).create(
            StrategyGenerationRunCreate(provider="legacy", model="legacy")
        )
        backtest_run = BacktestRepository(db).create_run(
            BacktestRunCreate(strategy_version_id=version.id)
        )
        assert backtest_run is not None
        research_job = ResearchJobRepository(db).create(
            job_type="deepseek_backtest",
            operation="legacy-target-migration",
            idempotency_key_digest="legacy-digest",
            request_hash="legacy-hash",
            request_payload={},
        )
        ids = (generation_run.id, backtest_run.id, research_job.id)

    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                f"CREATE TABLE {VERSION_TABLE} ("
                "version VARCHAR(64) PRIMARY KEY, "
                "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
        )
        connection.execute(
            text(f"INSERT INTO {VERSION_TABLE} (version) VALUES (:version)"),
            {"version": TARGET_LINEAGE_BASE_VERSION},
        )
        for table_name in (
            "strategy_generation_runs",
            "backtest_runs",
            "research_jobs",
        ):
            connection.execute(
                text(
                    f'ALTER TABLE "{table_name}" '
                    "DROP COLUMN execution_scope_id CASCADE"
                )
            )
        connection.execute(text("DELETE FROM execution_scopes"))

    assert upgrade_database(postgres_engine) == SCHEMA_VERSION
    assert verify_schema(postgres_engine).ready is True
    with session_factory() as db:
        assert db.get(StrategyGenerationRun, ids[0]).execution_scope_id == UNKNOWN_LEGACY_SCOPE_ID
        assert db.get(BacktestRun, ids[1]).execution_scope_id == UNKNOWN_LEGACY_SCOPE_ID
        assert db.get(ResearchJob, ids[2]).execution_scope_id == UNKNOWN_LEGACY_SCOPE_ID
        new_run = StrategyGenerationRunRepository(db).create(
            StrategyGenerationRunCreate(provider="new", model="new")
        )
        assert new_run.execution_scope_id == LOCAL_DRY_RUN_SCOPE_ID


def test_postgresql_trade_intent_client_order_id_is_unique_per_target(
    postgres_engine,
) -> None:
    assert upgrade_database(postgres_engine) == SCHEMA_VERSION
    session_factory = create_session_factory(postgres_engine)
    with session_factory() as db:
        repository = ExecutionLineageRepository(db, "OKX_DEMO")
        repository.create_trade_intent(
            client_order_id="PgUnique1",
            instrument_id="BTC-USDT-SWAP",
            side="buy",
            position_side="long",
            order_type="market",
            quantity=Decimal("1"),
        )
        with pytest.raises(IntegrityError):
            repository.create_trade_intent(
                client_order_id="PgUnique1",
                instrument_id="BTC-USDT-SWAP",
                side="buy",
                position_side="long",
                order_type="market",
                quantity=Decimal("1"),
            )
        db.rollback()


def test_frozen_old_research_job_ddl_upgrades_data_and_removes_global_unique(
    postgres_engine,
) -> None:
    Base.metadata.create_all(postgres_engine)
    with postgres_engine.begin() as connection:
        _detach_signal_evaluations_from_full_chain(connection)
        connection.execute(text("DROP TABLE signal_evaluations"))
        connection.execute(text("DROP TABLE strategy_deployments"))
        for table_name in (
            "full_chain_signal_snapshots",
            "full_chain_stage_runs",
            "strategy_candidate_approvals",
            "full_chain_runs",
        ):
            connection.execute(text('DROP TABLE "{}"'.format(table_name)))
        connection.execute(text("DROP TABLE research_job_attempts"))
        connection.execute(text("DROP TABLE research_jobs"))
        _create_frozen_pre_lineage_research_jobs(connection)
        connection.execute(
            text(
                """
                INSERT INTO research_jobs (
                    job_type, operation, idempotency_key_digest, request_hash,
                    request_payload, evidence_snapshot
                ) VALUES (
                    'deepseek_backtest', 'frozen-old-operation', 'frozen-digest',
                    'frozen-hash', '{}', '{}'
                )
                """
            )
        )
        connection.execute(
            text(
                f"CREATE TABLE {VERSION_TABLE} ("
                "version VARCHAR(64) PRIMARY KEY, "
                "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
        )
        connection.execute(
            text(f"INSERT INTO {VERSION_TABLE} (version) VALUES (:version)"),
            {"version": TARGET_LINEAGE_BASE_VERSION},
        )

    assert upgrade_database(postgres_engine) == SCHEMA_VERSION
    assert verify_schema(postgres_engine).ready is True
    with postgres_engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT execution_scope_id, operation FROM research_jobs "
                "WHERE operation = 'frozen-old-operation'"
            )
        ).one()
        assert row.execution_scope_id == UNKNOWN_LEGACY_SCOPE_ID
        uniques = inspect(connection).get_unique_constraints("research_jobs")
        unique_columns = {
            frozenset(constraint["column_names"])
            for constraint in uniques
        }
        assert frozenset(("operation", "idempotency_key_digest")) not in unique_columns
        assert frozenset(
            ("execution_scope_id", "operation", "idempotency_key_digest")
        ) in unique_columns


def test_schema_verifier_rejects_weakened_constraints_and_masquerading_indexes(
    postgres_engine,
) -> None:
    assert upgrade_database(postgres_engine) == SCHEMA_VERSION
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE research_jobs ADD CONSTRAINT renamed_global_unique "
                "UNIQUE (operation, idempotency_key_digest)"
            )
        )
        connection.execute(
            text(
                "ALTER TABLE trade_intents "
                "DROP CONSTRAINT trade_intents_okx_demo_target_check"
            )
        )
        connection.execute(
            text(
                "ALTER TABLE trade_intents ADD CONSTRAINT "
                "trade_intents_okx_demo_target_check "
                "CHECK (execution_target_id IS NOT NULL)"
            )
        )
        connection.execute(text("DROP INDEX trade_intents_target_status_idx"))
        connection.execute(
            text(
                "CREATE UNIQUE INDEX trade_intents_target_status_idx "
                "ON trade_intents (execution_target_id, status) "
                "WHERE status <> 'IGNORED'"
            )
        )

    readiness = verify_schema(postgres_engine)

    assert readiness.ready is False
    assert any(
        "unexpected unique constraint: research_jobs" in problem
        for problem in readiness.problems
    )
    assert any(
        "check definition mismatch: trade_intents.trade_intents_okx_demo_target_check"
        in problem
        for problem in readiness.problems
    )
    assert any(
        "missing index: trade_intents(execution_target_id,status)" in problem
        for problem in readiness.problems
    )
    assert any(
        "unexpected unique index: trade_intents(execution_target_id,status)"
        in problem
        for problem in readiness.problems
    )


def test_postgresql_legacy_mutations_and_cross_scope_completion_are_blocked(
    postgres_engine,
) -> None:
    assert upgrade_database(postgres_engine) == SCHEMA_VERSION
    session_factory = create_session_factory(postgres_engine)
    with session_factory() as db:
        ensure_execution_scope_catalog(db)
        legacy_job = ResearchJob(
            execution_scope_id=UNKNOWN_LEGACY_SCOPE_ID,
            job_type="historical",
            operation="historical",
            idempotency_key_digest="historical",
            request_hash="historical",
            request_payload={},
            status="PENDING",
        )
        legacy_generation = StrategyGenerationRun(
            execution_scope_id=UNKNOWN_LEGACY_SCOPE_ID,
            provider="historical",
            model="unknown",
            params_snapshot={},
        )
        db.add_all((legacy_job, legacy_generation))
        db.commit()
        legacy_repository = ResearchJobRepository(
            db, execution_scope_id=UNKNOWN_LEGACY_SCOPE_ID
        )
        with pytest.raises(ValueError, match="read-only"):
            legacy_repository.cancel(legacy_job.id, "must not mutate")
        db.expire_all()
        assert db.get(ResearchJob, legacy_job.id).status == "PENDING"

        repository = ResearchJobRepository(db)
        job = repository.create(
            job_type="deepseek_backtest",
            operation="postgres-linkage",
            idempotency_key_digest="postgres-linkage",
            request_hash="postgres-linkage",
            request_payload={},
        )
        claimed = repository.claim_next(owner="postgres-worker", lease_seconds=60)
        assert claimed is not None and claimed.lease_token is not None
        with pytest.raises(ResearchJobLinkageBlocked, match="BLOCKED"):
            repository.complete(
                job.id,
                claimed.lease_token,
                status="SUCCESS",
                stage="COMPLETED",
                links={"strategy_generation_run_id": legacy_generation.id},
                evidence_snapshot={"status": "SUCCESS"},
                error_message=None,
                provider_completed=False,
            )
        db.expire_all()
        persisted = db.get(ResearchJob, job.id)
        assert persisted is not None
        assert persisted.status == "RUNNING"
        assert persisted.strategy_generation_run_id is None


@pytest.mark.parametrize("illegal", ["has-hyphen", "空", "x" * 33])
def test_postgresql_database_rejects_illegal_client_order_id(
    postgres_engine,
    illegal: str,
) -> None:
    assert upgrade_database(postgres_engine) == SCHEMA_VERSION
    session_factory = create_session_factory(postgres_engine)
    with session_factory() as db:
        ensure_execution_scope_catalog(db)
        db.commit()
        db.add(
            TradeIntent(
                execution_target_id=OKX_DEMO_TARGET_ID,
                client_order_id=illegal,
                instrument_id="BTC-USDT-SWAP",
                side="buy",
                position_side="long",
                order_type="market",
                quantity=Decimal("1"),
                request_snapshot={},
            )
        )

        with pytest.raises((IntegrityError, DataError)):
            db.commit()
