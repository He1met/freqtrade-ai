import pytest
from sqlalchemy.orm import Session

from app.db.session import create_database_engine, create_session_factory
from app.models import Base
from app.repositories import BacktestRepository, StrategyRepository
from app.schemas import (
    BacktestResultCreate,
    BacktestRunCreate,
    BacktestRunStatusUpdate,
    BacktestTaskCreate,
    BacktestTaskStatusUpdate,
    StrategyCreate,
    StrategyVersionCreate,
)


@pytest.fixture()
def db_session() -> Session:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    with session_factory() as session:
        yield session


@pytest.fixture()
def strategy_version_id(db_session: Session) -> int:
    repository = StrategyRepository(db_session)
    strategy = repository.create(StrategyCreate(name="Backtest Candidate", slug="backtest-candidate"))
    version = repository.create_version(
        StrategyVersionCreate(
            strategy_id=strategy.id,
            blueprint={"entry": {"indicator": "rsi"}},
            generated_code="class BacktestCandidate: pass",
            file_path="user_data/strategies/generated/backtest_candidate.py",
        )
    )

    assert version is not None
    return version.id


def test_create_backtest_run_and_tasks(db_session: Session, strategy_version_id: int) -> None:
    repository = BacktestRepository(db_session)

    run = repository.create_run(
        BacktestRunCreate(
            strategy_version_id=strategy_version_id,
            profile_name="mvp-smoke",
            config_snapshot={"timerange": "20240101-20240201"},
        )
    )
    assert run is not None

    first = repository.create_task(
        run.id,
        BacktestTaskCreate(pair="BTC/USDT", timeframe="5m", config_path="tmp/btc.json"),
    )
    second = repository.create_task(
        run.id,
        BacktestTaskCreate(pair="ETH/USDT", timeframe="15m", config_path="tmp/eth.json"),
    )
    tasks = repository.list_tasks(run.id)

    assert first is not None
    assert second is not None
    assert run.requested_task_count == 2
    assert [task.pair for task in tasks] == ["BTC/USDT", "ETH/USDT"]


def test_update_task_status_records_success_and_failure(
    db_session: Session,
    strategy_version_id: int,
) -> None:
    repository = BacktestRepository(db_session)
    run = repository.create_run(BacktestRunCreate(strategy_version_id=strategy_version_id))
    assert run is not None

    success = repository.create_task(run.id, BacktestTaskCreate(pair="BTC/USDT", timeframe="5m"))
    failure = repository.create_task(run.id, BacktestTaskCreate(pair="ETH/USDT", timeframe="5m"))
    assert success is not None
    assert failure is not None

    updated_success = repository.update_task_status(
        success.id,
        BacktestTaskStatusUpdate(
            status="succeeded",
            result_path="reports/backtests/btc-result.json",
        ),
    )
    updated_failure = repository.update_task_status(
        failure.id,
        BacktestTaskStatusUpdate(
            status="failed",
            error_message="Freqtrade CLI exited with code 2",
        ),
    )

    assert updated_success is not None
    assert updated_success.status == "succeeded"
    assert updated_success.result_path == "reports/backtests/btc-result.json"
    assert updated_success.completed_at is not None
    assert updated_failure is not None
    assert updated_failure.status == "failed"
    assert updated_failure.error_message == "Freqtrade CLI exited with code 2"
    assert updated_failure.completed_at is not None


def test_save_result_summary_and_list_by_run(
    db_session: Session,
    strategy_version_id: int,
) -> None:
    repository = BacktestRepository(db_session)
    run = repository.create_run(BacktestRunCreate(strategy_version_id=strategy_version_id))
    assert run is not None
    task = repository.create_task(run.id, BacktestTaskCreate(pair="BTC/USDT", timeframe="5m"))
    assert task is not None

    result = repository.save_result(
        task.id,
        BacktestResultCreate(
            result_path="reports/backtests/backtest-result.json",
            metrics_snapshot={"strategy": {"profit_total": 123.4}},
            profit_total=123.4,
            profit_pct=0.123,
            max_drawdown_pct=0.045,
            win_rate=0.61,
            total_trades=42,
            timerange="20240101-20240201",
        ),
    )
    results = repository.list_results(run.id)

    assert result is not None
    assert result.backtest_run_id == run.id
    assert result.backtest_task_id == task.id
    assert result.result_path == "reports/backtests/backtest-result.json"
    assert result.profit_total == 123.4
    assert result.win_rate == 0.61
    assert result.total_trades == 42
    assert [stored.id for stored in results] == [result.id]


def test_update_run_status_and_missing_records(
    db_session: Session,
    strategy_version_id: int,
) -> None:
    repository = BacktestRepository(db_session)

    assert repository.create_run(BacktestRunCreate(strategy_version_id=999)) is None
    assert repository.create_task(999, BacktestTaskCreate(pair="BTC/USDT", timeframe="5m")) is None
    assert repository.update_task_status(999, BacktestTaskStatusUpdate(status="failed")) is None
    assert repository.save_result(999, BacktestResultCreate(result_path="missing.json")) is None

    run = repository.create_run(BacktestRunCreate(strategy_version_id=strategy_version_id))
    assert run is not None

    running = repository.update_run_status(run.id, BacktestRunStatusUpdate(status="running"))
    succeeded = repository.update_run_status(run.id, BacktestRunStatusUpdate(status="succeeded"))

    assert running is not None
    assert running.started_at is not None
    assert succeeded is not None
    assert succeeded.status == "succeeded"
    assert succeeded.completed_at is not None
