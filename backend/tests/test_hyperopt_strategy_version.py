from typing import Optional

import pytest
from sqlalchemy.orm import Session

from app.adapters.freqtrade.result_parser import HyperoptResultParsed
from app.adapters.freqtrade.strategy_file_manager import StrategyFileManager
from app.db.session import create_database_engine, create_session_factory
from app.models import Base
from app.repositories import StrategyRepository
from app.schemas import StrategyCreate, StrategyVersionCreate
from app.services.hyperopt_strategy_version import (
    HyperoptStrategyVersionError,
    HyperoptStrategyVersionService,
)
from app.services.strategy_file_validation import StrategyFileValidationBlocked


@pytest.fixture()
def db_session() -> Session:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    with session_factory() as session:
        yield session


def parsed_hyperopt_result(best_params: Optional[dict] = None) -> HyperoptResultParsed:
    return HyperoptResultParsed(
        result_path="reports/hyperopt/run-42/result.json",
        strategy_name="LineageRsiStrategy",
        best_epoch=42,
        loss=-1.25,
        score=0.91,
        is_best=True,
        spaces=["buy", "sell", "roi", "stoploss"],
        best_params=best_params
        if best_params is not None
        else {
            "buy": {"rsi_value": 31},
            "sell": {"sell_rsi": 74},
            "roi": {"0": 0.05, "60": 0.02},
            "stoploss": -0.12,
        },
        metrics_snapshot={
            "normalized_metrics": {
                "profit_total": 42.0,
                "profit_pct": 0.08,
                "max_drawdown_pct": 0.03,
            }
        },
    )


def create_parent_version(db_session: Session):
    repository = StrategyRepository(db_session)
    strategy = repository.create(
        StrategyCreate(
            name="Lineage RSI",
            slug="lineage-rsi",
            description="Strategy used to verify hyperopt child versions.",
        )
    )
    parent = repository.create_version(
        StrategyVersionCreate(
            strategy_id=strategy.id,
            blueprint={
                "schema_version": "2",
                "class_name": "LineageRsiStrategy",
                "entry_rules": [{"indicator": "rsi", "operator": "<", "value": 35}],
            },
            generated_code="class LineageRsiStrategy: pass\n",
            file_path="user_data/strategies/generated/lineage_rsi_v1.py",
            validation_status="passed",
        )
    )
    assert parent is not None
    return parent


def test_creates_optimized_child_strategy_version_without_overwriting_parent(
    db_session: Session,
    tmp_path,
) -> None:
    parent = create_parent_version(db_session)
    output_dir = tmp_path / "strategies"
    output_dir.mkdir()
    service = HyperoptStrategyVersionService(
        db_session,
        file_manager=StrategyFileManager(output_dir=output_dir, approved_roots=[output_dir]),
    )

    result = service.create_optimized_version(
        parent_version_id=parent.id,
        hyperopt_run_id="hyperopt-run-42",
        hyperopt_result=parsed_hyperopt_result(),
        artifact_manifest_path="reports/hyperopt/run-42/manifest.json",
    )

    child = result.optimized_version
    assert child.id != parent.id
    assert child.strategy_id == parent.strategy_id
    assert child.parent_version_id == parent.id
    assert child.version_number == 2
    assert child.file_path != parent.file_path
    assert child.validation_status == "passed"
    assert child.validation_errors == []
    assert child.code_hash is not None
    assert result.strategy_file.write_status == "written"
    assert result.strategy_file.validation_status == "passed"
    assert child.change_summary == (
        f"Derived from StrategyVersion {parent.id} using Hyperopt run "
        "hyperopt-run-42 best epoch 42."
    )

    child_path = tmp_path / "strategies" / "lineage_rsi_v1_hyperopt_hyperopt_run_42_v2.py"
    assert child.file_path == str(child_path)
    assert child_path.read_text(encoding="utf-8") == child.generated_code
    assert "HYPEROPT_DERIVATION" in child.generated_code
    assert "dry-run" in child.generated_code

    derivation = child.blueprint["hyperopt_derivation"]
    assert derivation["source"] == "freqtrade_hyperopt"
    assert derivation["hyperopt_run_id"] == "hyperopt-run-42"
    assert derivation["artifact_manifest_path"] == "reports/hyperopt/run-42/manifest.json"
    assert derivation["best_params"]["buy"] == {"rsi_value": 31}
    assert derivation["metrics_snapshot"]["normalized_metrics"]["profit_total"] == 42.0
    assert derivation["safety"] == {
        "dry_run_enabled": False,
        "live_trading_enabled": False,
        "exchange_connection_enabled": False,
        "download_enabled": False,
    }

    assert child.diff_snapshot["changed_fields"] == [
        "hyperopt_derivation",
        "generated_code.HYPEROPT_DERIVATION",
        "file_path",
        "code_hash",
        "strategy_file_validation",
    ]
    assert child.diff_snapshot["before"]["file_path"] == parent.file_path
    assert child.diff_snapshot["after"]["parent_version_id"] == parent.id
    assert child.diff_snapshot["after"]["best_params"]["stoploss"] == -0.12
    assert child.diff_snapshot["after"]["file_path"] == child.file_path
    assert child.diff_snapshot["after"]["code_hash"] == child.code_hash
    assert (
        child.diff_snapshot["after"]["strategy_file_validation"]["checksum"]
        == child.code_hash
    )

    repository = StrategyRepository(db_session)
    reloaded_parent = repository.get_version(parent.id)
    assert reloaded_parent is not None
    assert reloaded_parent.file_path == "user_data/strategies/generated/lineage_rsi_v1.py"
    assert "hyperopt_derivation" not in reloaded_parent.blueprint
    assert reloaded_parent.generated_code == "class LineageRsiStrategy: pass\n"


def test_fails_closed_when_strategy_output_directory_is_missing(
    db_session: Session,
    tmp_path,
) -> None:
    parent = create_parent_version(db_session)
    output_dir = tmp_path / "missing" / "strategies"
    service = HyperoptStrategyVersionService(
        db_session,
        file_manager=StrategyFileManager(output_dir=output_dir, approved_roots=[output_dir]),
    )

    with pytest.raises(StrategyFileValidationBlocked) as exc_info:
        service.create_optimized_version(
            parent_version_id=parent.id,
            hyperopt_run_id="hyperopt-run-42",
            hyperopt_result=parsed_hyperopt_result(),
        )

    result = exc_info.value.result
    assert result.write_status == "blocked"
    assert "strategy output directory does not exist" in result.blocked_reasons
    assert not output_dir.exists()


def test_fails_closed_when_parent_strategy_version_is_missing(db_session: Session, tmp_path) -> None:
    service = HyperoptStrategyVersionService(
        db_session,
        file_manager=StrategyFileManager(output_dir=tmp_path / "strategies"),
    )

    with pytest.raises(HyperoptStrategyVersionError, match="parent StrategyVersion"):
        service.create_optimized_version(
            parent_version_id=999,
            hyperopt_run_id="hyperopt-run-42",
            hyperopt_result=parsed_hyperopt_result(),
        )

    assert not (tmp_path / "strategies").exists()


def test_fails_closed_when_best_params_are_missing(db_session: Session, tmp_path) -> None:
    parent = create_parent_version(db_session)
    service = HyperoptStrategyVersionService(
        db_session,
        file_manager=StrategyFileManager(output_dir=tmp_path / "strategies"),
    )

    with pytest.raises(HyperoptStrategyVersionError, match="best params"):
        service.create_optimized_version(
            parent_version_id=parent.id,
            hyperopt_run_id="hyperopt-run-42",
            hyperopt_result=parsed_hyperopt_result(best_params={}),
        )

    assert not (tmp_path / "strategies").exists()


def test_fails_closed_when_best_params_contain_secret_shaped_keys(
    db_session: Session,
    tmp_path,
) -> None:
    parent = create_parent_version(db_session)
    service = HyperoptStrategyVersionService(
        db_session,
        file_manager=StrategyFileManager(output_dir=tmp_path / "strategies"),
    )

    with pytest.raises(HyperoptStrategyVersionError, match="forbidden key"):
        service.create_optimized_version(
            parent_version_id=parent.id,
            hyperopt_run_id="hyperopt-run-42",
            hyperopt_result=parsed_hyperopt_result(
                best_params={"buy": {"api_key": "should-not-be-recorded"}}
            ),
        )

    assert not (tmp_path / "strategies").exists()
