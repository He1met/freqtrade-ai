import pytest
from sqlalchemy.orm import Session

from app.db.session import create_database_engine, create_session_factory
from app.models import Base
from app.repositories import StrategyGenerationRunRepository
from app.schemas import StrategyGenerationRunCreate, StrategyGenerationRunStatusUpdate


@pytest.fixture()
def db_session() -> Session:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    with session_factory() as session:
        yield session


def test_create_and_get_generation_run(db_session: Session) -> None:
    repository = StrategyGenerationRunRepository(db_session)

    created = repository.create(
        StrategyGenerationRunCreate(
            provider="mimo",
            model="mimo-test",
            prompt_hash="prompt-hash",
            prompt_summary="Generate a conservative strategy blueprint.",
            params_snapshot={"temperature": 0.2},
            requested_count=3,
        )
    )

    found = repository.get(created.id)

    assert found is not None
    assert found.provider == "mimo"
    assert found.model == "mimo-test"
    assert found.status == "pending"
    assert found.requested_count == 3
    assert found.generated_count == 0


def test_list_generation_runs_by_status(db_session: Session) -> None:
    repository = StrategyGenerationRunRepository(db_session)
    first = repository.create(StrategyGenerationRunCreate(provider="mimo", model="model-a"))
    second = repository.create(StrategyGenerationRunCreate(provider="openai", model="model-b"))

    repository.update_status(
        first.id,
        StrategyGenerationRunStatusUpdate(status="running"),
    )

    running_runs = repository.list(status="running")
    all_runs = repository.list()

    assert [run.id for run in running_runs] == [first.id]
    assert {run.id for run in all_runs} == {first.id, second.id}


def test_update_status_records_success_counts(db_session: Session) -> None:
    repository = StrategyGenerationRunRepository(db_session)
    run = repository.create(
        StrategyGenerationRunCreate(provider="mimo", model="mimo-test", requested_count=2)
    )

    updated = repository.update_status(
        run.id,
        StrategyGenerationRunStatusUpdate(
            status="succeeded",
            generated_count=2,
            accepted_count=1,
            failed_count=0,
        ),
    )

    assert updated is not None
    assert updated.status == "succeeded"
    assert updated.generated_count == 2
    assert updated.accepted_count == 1
    assert updated.failed_count == 0
    assert updated.completed_at is not None


def test_update_status_records_failure_message(db_session: Session) -> None:
    repository = StrategyGenerationRunRepository(db_session)
    run = repository.create(StrategyGenerationRunCreate(provider="mimo", model="mimo-test"))

    updated = repository.update_status(
        run.id,
        StrategyGenerationRunStatusUpdate(
            status="failed",
            failed_count=1,
            error_message="schema validation failed",
        ),
    )

    assert updated is not None
    assert updated.status == "failed"
    assert updated.failed_count == 1
    assert updated.error_message == "schema validation failed"
    assert updated.completed_at is not None


def test_update_status_returns_none_for_missing_run(db_session: Session) -> None:
    repository = StrategyGenerationRunRepository(db_session)

    updated = repository.update_status(
        999,
        StrategyGenerationRunStatusUpdate(status="failed", error_message="missing"),
    )

    assert updated is None
