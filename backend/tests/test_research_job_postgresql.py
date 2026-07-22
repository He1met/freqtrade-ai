import os
from threading import Barrier, Lock, Thread

import pytest
from sqlalchemy import inspect, text

from app.db.migrations import (
    PREVIOUS_SCHEMA_VERSION,
    SCHEMA_VERSION,
    VERSION_TABLE,
    upgrade_database,
    verify_schema,
)
from app.db.session import create_database_engine, create_session_factory
from app.models import Base, Strategy
from app.repositories import ResearchJobRepository, StrategyRepository
from app.schemas import StrategyCreate


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
        if table.name not in {"research_jobs", "research_worker_control"}
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
            {"version": PREVIOUS_SCHEMA_VERSION},
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
