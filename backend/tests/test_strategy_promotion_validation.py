from __future__ import annotations

from pathlib import Path

import pytest

from app.db.session import create_database_engine, create_session_factory
from app.models import (
    BacktestResult,
    BacktestRun,
    BacktestTask,
    Base,
    Strategy,
    StrategyVersion,
)
from app.repositories import ensure_execution_scope_catalog
from app.services.strategy_promotion_validation import (
    StrategyPromotionValidationBlocked,
    StrategyPromotionValidationService,
)


@pytest.fixture()
def db(tmp_path: Path):
    engine = create_database_engine(
        f"sqlite+pysqlite:///{tmp_path / 'promotion-validation.sqlite'}"
    )
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory() as session:
        ensure_execution_scope_catalog(session)
        session.commit()
        yield session
    engine.dispose()


def seed_result(db, *, trades: list[dict]) -> BacktestResult:
    strategy = Strategy(
        name="PromotionValidationStrategy",
        slug="promotion-validation-strategy",
        source="ai_generated",
    )
    db.add(strategy)
    db.flush()
    version = StrategyVersion(
        strategy_id=strategy.id,
        version_number=1,
        blueprint={
            "schema_version": "2",
            "name": strategy.name,
            "slug": strategy.slug,
            "class_name": strategy.name,
            "description": "promotion validation",
            "timeframe": "5m",
            "indicators": [{"name": "rsi", "kind": "rsi", "period": 14}],
            "entry_rules": [{"indicator": "rsi", "operator": "<", "value": 30}],
            "exit_rules": [{"indicator": "rsi", "operator": ">", "value": 70}],
            "tags": [],
        },
        generated_code="class PromotionValidationStrategy: pass",
        file_path="user_data/strategies/PromotionValidationStrategy.py",
        validation_status="passed",
    )
    db.add(version)
    db.flush()
    run = BacktestRun(
        execution_scope_id="LOCAL_DRY_RUN",
        strategy_version_id=version.id,
        profile_name="promotion-validation",
        status="succeeded",
        requested_task_count=1,
    )
    db.add(run)
    db.flush()
    task = BacktestTask(
        backtest_run_id=run.id,
        pair="BTC/USDT:USDT",
        timeframe="5m",
        status="succeeded",
        result_path="reports/backtests/promotion-validation.json",
    )
    db.add(task)
    db.flush()
    result = BacktestResult(
        backtest_run_id=run.id,
        backtest_task_id=task.id,
        result_path=task.result_path,
        metrics_snapshot={
            "parser_metadata": {"source": "freqtrade_result_parser"},
            "starting_balance": 1000.0,
            "trades": trades,
        },
        profit_total=90.0,
        profit_pct=0.09,
        max_drawdown_pct=0.05,
        win_rate=0.6,
        total_trades=len(trades),
    )
    db.add(result)
    db.commit()
    return result


def real_trade(index: int) -> dict:
    open_rate, close_rate = {
        0: (100.0, 101.0),
        1: (100.0, 99.0),
        2: (100.0, 100.1),
    }[index % 3]
    opened = 1_704_067_200_000 + index * 300_000
    return {
        "open_timestamp": opened,
        "close_timestamp": opened + 240_000,
        "open_rate": open_rate,
        "close_rate": close_rate,
        "fee_open": 0.0005,
        "fee_close": 0.0005,
        "funding_fees": 0.0,
        "profit_abs": 1.0,
        "profit_ratio": 0.001,
    }


def test_attach_uses_persisted_fee_trade_and_chronological_evidence(db) -> None:
    result = seed_result(db, trades=[real_trade(index) for index in range(90)])

    evidence = StrategyPromotionValidationService(db).attach(result.id)

    assert evidence["source"] == "persisted_freqtrade_backtest_trades"
    assert evidence["source_backtest_result_id"] == result.id
    assert evidence["net_of_costs"] is True
    assert evidence["fee_fields_verified"] is True
    assert evidence["out_of_sample"]["passed"] is True
    assert evidence["out_of_sample"]["total_trades"] == 30
    assert evidence["walk_forward"]["passed"] is True
    assert evidence["walk_forward"]["market_states"] == ["bear", "bull", "range"]
    assert len(evidence["walk_forward"]["folds"]) == 3
    db.refresh(result)
    assert result.metrics_snapshot["promotion_evidence"] == evidence


def test_attach_refuses_non_freqtrade_or_incomplete_fee_evidence(db) -> None:
    trade = real_trade(0)
    trade.pop("fee_close")
    result = seed_result(db, trades=[trade])

    with pytest.raises(
        StrategyPromotionValidationBlocked,
        match="fee_close",
    ):
        StrategyPromotionValidationService(db).attach(result.id)

    db.refresh(result)
    assert "promotion_evidence" not in result.metrics_snapshot
