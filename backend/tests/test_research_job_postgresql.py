import os
from decimal import Decimal
from threading import Barrier, Lock, Thread

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.db.migrations import (
    LEGACY_SCHEMA_VERSION,
    PREVIOUS_SCHEMA_VERSION,
    SCHEMA_VERSION,
    TARGET_LINEAGE_BASE_VERSION,
    VERSION_TABLE,
    upgrade_database,
    verify_schema,
)
from app.db.session import create_database_engine, create_session_factory
from app.models import BacktestRun, Base, ResearchJob, Strategy, StrategyGenerationRun
from app.models.execution_lineage import (
    LOCAL_DRY_RUN_SCOPE_ID,
    UNKNOWN_LEGACY_SCOPE_ID,
)
from app.repositories import (
    BacktestRepository,
    ExecutionLineageRepository,
    ResearchJobRepository,
    StrategyGenerationRunRepository,
    StrategyRepository,
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


def test_incremental_worker_migration_preserves_existing_runtime_rows(postgres_engine) -> None:
    old_tables = [
        table
        for table in Base.metadata.tables.values()
        if table.name
        not in {
            "exchange_fills",
            "exchange_orders",
            "exchange_positions",
            "execution_manifests",
            "reconciliation_runs",
            "research_job_attempts",
            "research_jobs",
            "research_worker_control",
            "risk_decisions",
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
    assert {"research_jobs", "research_worker_control"}.issubset(
        set(inspect(postgres_engine).get_table_names())
    )
    with session_factory() as db:
        preserved = db.get(Strategy, strategy_id)
        assert preserved is not None
        assert preserved.slug == "preserved-migration-strategy"


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
