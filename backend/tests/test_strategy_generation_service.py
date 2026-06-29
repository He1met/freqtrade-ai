import pytest
from sqlalchemy.orm import Session

from app.adapters.freqtrade.strategy_file_manager import StrategyFileManager
from app.db.session import create_database_engine, create_session_factory
from app.models import Base
from app.repositories import StrategyGenerationRunRepository, StrategyRepository
from app.schemas.strategy_blueprint import StrategyBlueprint
from app.services.strategy_generation import (
    FakeStrategyBlueprintProvider,
    StrategyGenerationService,
)


@pytest.fixture()
def db_session() -> Session:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    with session_factory() as session:
        yield session


def test_fake_provider_generation_creates_version_and_file(
    db_session: Session,
    tmp_path,
) -> None:
    service = StrategyGenerationService(
        db_session,
        provider=FakeStrategyBlueprintProvider(),
        file_manager=StrategyFileManager(output_dir=tmp_path),
    )

    version_ids = service.run_once("Generate one conservative RSI strategy.", requested_count=1)

    run = StrategyGenerationRunRepository(db_session).list()[0]
    assert run.status == "succeeded"
    assert run.generated_count == 1
    assert run.accepted_count == 1
    assert run.failed_count == 0

    assert len(version_ids) == 1
    strategy = StrategyRepository(db_session).get_by_slug("mvp-rsi-strategy")
    assert strategy is not None
    version = StrategyRepository(db_session).get_latest_version(strategy.id)
    assert version is not None
    assert version.generation_run_id == run.id
    assert version.validation_status == "passed"
    assert version.blueprint["class_name"] == "MvpRsiStrategy"
    assert "class MvpRsiStrategy(IStrategy):" in version.generated_code
    assert tmp_path.joinpath("mvp_rsi_strategy_run_1_1.py").exists()


class FailingProvider(FakeStrategyBlueprintProvider):
    provider_name = "fake-failure"

    def generate(self, prompt_summary: str, requested_count: int) -> list[StrategyBlueprint]:
        raise RuntimeError("provider unavailable")


def test_generation_failure_marks_run_failed_without_strategy_version(
    db_session: Session,
    tmp_path,
) -> None:
    service = StrategyGenerationService(
        db_session,
        provider=FailingProvider(),
        file_manager=StrategyFileManager(output_dir=tmp_path),
    )

    with pytest.raises(RuntimeError):
        service.run_once("Generate one strategy.", requested_count=1)

    run = StrategyGenerationRunRepository(db_session).list()[0]
    assert run.status == "failed"
    assert run.failed_count == 1
    assert run.error_message == "provider unavailable"
    assert StrategyRepository(db_session).get_by_slug("mvp-rsi-strategy") is None
    assert list(tmp_path.iterdir()) == []
