import json
from pathlib import Path
from typing import Optional

import pytest
from sqlalchemy.orm import Session

from app.db.session import create_database_engine, create_session_factory
from app.models import Base
from app.repositories import BacktestRepository, StrategyRepository
from app.schemas import BacktestRunCreate, BacktestTaskCreate, StrategyCreate, StrategyVersionCreate
from app.services.backtest_execution import BacktestTaskExecutionService


class FakeBacktestRunner:
    def __init__(self, result_path: Path, fail: bool = False) -> None:
        self.result_path = result_path
        self.fail = fail
        self.calls = []

    def run_backtest(
        self,
        config_path: Path,
        strategy_name: str,
        result_path: Optional[Path] = None,
        timeout_seconds: Optional[int] = None,
    ) -> Path:
        self.calls.append((config_path, strategy_name, result_path, timeout_seconds))
        if self.fail:
            raise RuntimeError("fake runner failed")
        return self.result_path


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
    strategy = repository.create(StrategyCreate(name="MVP RSI", slug="mvp-rsi"))
    version = repository.create_version(
        StrategyVersionCreate(
            strategy_id=strategy.id,
            blueprint={"class_name": "MvpRsiStrategy"},
            generated_code="class MvpRsiStrategy: pass",
            file_path="user_data/strategies/generated/mvp_rsi.py",
        )
    )
    assert version is not None
    return version.id


def _write_result(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "strategy": {
                    "MvpRsiStrategy": {
                        "profit_total_abs": 10.5,
                        "profit_total_pct": 3.2,
                        "max_drawdown_pct": 1.1,
                        "winrate": 55.0,
                        "total_trades": 20,
                        "timerange": "20240101-20240201",
                    }
                }
            }
        ),
        encoding="utf-8",
    )


def test_execute_next_pending_task_saves_result(
    db_session: Session,
    strategy_version_id: int,
    tmp_path,
) -> None:
    result_path = tmp_path / "result.json"
    _write_result(result_path)
    repository = BacktestRepository(db_session)
    run = repository.create_run(BacktestRunCreate(strategy_version_id=strategy_version_id))
    assert run is not None
    task = repository.create_task(
        run.id,
        BacktestTaskCreate(
            pair="BTC/USDT",
            timeframe="5m",
            config_path="tmp/freqtrade_configs/backtest.json",
        ),
    )
    assert task is not None
    runner = FakeBacktestRunner(result_path)

    updated = BacktestTaskExecutionService(db_session, runner).execute_next_pending(
        run.id,
        strategy_name="MvpRsiStrategy",
        timeout_seconds=30,
    )
    results = repository.list_results(run.id)
    refreshed_run = repository.get_run(run.id)

    assert updated is not None
    assert updated.status == "succeeded"
    assert updated.result_path == str(result_path)
    assert results[0].profit_total == 10.5
    assert results[0].profit_pct == 0.032
    assert results[0].win_rate == 0.55
    assert refreshed_run is not None
    assert refreshed_run.status == "succeeded"
    assert runner.calls == [
        (Path("tmp/freqtrade_configs/backtest.json"), "MvpRsiStrategy", None, 30)
    ]


def test_execute_next_pending_task_records_runner_failure(
    db_session: Session,
    strategy_version_id: int,
    tmp_path,
) -> None:
    repository = BacktestRepository(db_session)
    run = repository.create_run(BacktestRunCreate(strategy_version_id=strategy_version_id))
    assert run is not None
    task = repository.create_task(
        run.id,
        BacktestTaskCreate(
            pair="BTC/USDT",
            timeframe="5m",
            config_path="tmp/freqtrade_configs/backtest.json",
        ),
    )
    assert task is not None

    updated = BacktestTaskExecutionService(
        db_session,
        FakeBacktestRunner(tmp_path / "missing.json", fail=True),
    ).execute_next_pending(run.id, strategy_name="MvpRsiStrategy")
    refreshed_run = repository.get_run(run.id)

    assert updated is not None
    assert updated.status == "failed"
    assert updated.error_message == "fake runner failed"
    assert refreshed_run is not None
    assert refreshed_run.status == "failed"


def test_execute_next_pending_task_records_parse_failure(
    db_session: Session,
    strategy_version_id: int,
    tmp_path,
) -> None:
    repository = BacktestRepository(db_session)
    run = repository.create_run(BacktestRunCreate(strategy_version_id=strategy_version_id))
    assert run is not None
    task = repository.create_task(
        run.id,
        BacktestTaskCreate(
            pair="BTC/USDT",
            timeframe="5m",
            config_path="tmp/freqtrade_configs/backtest.json",
        ),
    )
    assert task is not None

    updated = BacktestTaskExecutionService(
        db_session,
        FakeBacktestRunner(tmp_path / "missing.json"),
    ).execute_next_pending(run.id, strategy_name="MvpRsiStrategy")

    assert updated is not None
    assert updated.status == "failed"
    assert "does not exist" in (updated.error_message or "")
