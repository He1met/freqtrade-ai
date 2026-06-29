import pytest
from sqlalchemy.orm import Session

from app.db.session import create_database_engine, create_session_factory
from app.models import Base
from app.repositories import StrategyRepository
from app.schemas import StrategyCreate, StrategyVersionCreate


@pytest.fixture()
def db_session() -> Session:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    with session_factory() as session:
        yield session


def test_create_strategy_and_version(db_session: Session) -> None:
    repository = StrategyRepository(db_session)

    strategy = repository.create(
        StrategyCreate(
            name="Mean Reversion BTC",
            slug="mean-reversion-btc",
            description="Generated strategy candidate.",
            tags=["btc", "mvp"],
        )
    )
    version = repository.create_version(
        StrategyVersionCreate(
            strategy_id=strategy.id,
            blueprint={"entry": {"indicator": "rsi", "threshold": 30}},
            generated_code="class MeanReversionStrategy: pass",
            code_hash="code-hash",
            file_path="user_data/strategies/generated/mean_reversion_btc.py",
            validation_status="passed",
        )
    )

    assert version is not None
    assert version.version_number == 1
    assert version.blueprint["entry"]["indicator"] == "rsi"
    assert version.validation_status == "passed"

    found = repository.get(strategy.id)
    assert found is not None
    assert found.current_version_id == version.id


def test_create_version_increments_per_strategy(db_session: Session) -> None:
    repository = StrategyRepository(db_session)
    strategy = repository.create(StrategyCreate(name="Breakout ETH", slug="breakout-eth"))

    first = repository.create_version(
        StrategyVersionCreate(
            strategy_id=strategy.id,
            blueprint={"name": "breakout-eth", "version": 1},
            generated_code="class BreakoutStrategy: pass",
            file_path="user_data/strategies/generated/breakout_eth_v1.py",
        )
    )
    second = repository.create_version(
        StrategyVersionCreate(
            strategy_id=strategy.id,
            blueprint={"name": "breakout-eth", "version": 2},
            generated_code="class BreakoutStrategyV2: pass",
            file_path="user_data/strategies/generated/breakout_eth_v2.py",
        )
    )

    assert first is not None
    assert second is not None
    assert first.version_number == 1
    assert second.version_number == 2


def test_get_latest_version_returns_highest_version_number(db_session: Session) -> None:
    repository = StrategyRepository(db_session)
    strategy = repository.create(StrategyCreate(name="Scalping SOL", slug="scalping-sol"))

    repository.create_version(
        StrategyVersionCreate(
            strategy_id=strategy.id,
            version_number=3,
            blueprint={"name": "scalping-sol", "version": 3},
            generated_code="class ScalpingStrategyV3: pass",
            file_path="user_data/strategies/generated/scalping_sol_v3.py",
        )
    )
    repository.create_version(
        StrategyVersionCreate(
            strategy_id=strategy.id,
            version_number=4,
            blueprint={"name": "scalping-sol", "version": 4},
            generated_code="class ScalpingStrategyV4: pass",
            file_path="user_data/strategies/generated/scalping_sol_v4.py",
        )
    )

    latest = repository.get_latest_version(strategy.id)

    assert latest is not None
    assert latest.version_number == 4
    assert latest.blueprint["version"] == 4


def test_create_version_returns_none_for_missing_strategy(db_session: Session) -> None:
    repository = StrategyRepository(db_session)

    version = repository.create_version(
        StrategyVersionCreate(
            strategy_id=999,
            blueprint={"name": "missing"},
            generated_code="class MissingStrategy: pass",
            file_path="user_data/strategies/generated/missing.py",
        )
    )

    assert version is None
