import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.db.session import create_database_engine, create_session_factory
from app.models import Base
from app.repositories import BacktestRepository, StrategyRepository, StrategyScoreRepository
from app.schemas import (
    BacktestResultCreate,
    BacktestResultRead,
    BacktestRunCreate,
    BacktestRunRead,
    BacktestTaskCreate,
    BacktestTaskRead,
    DataSourceTrace,
    StrategyCreate,
    StrategyRead,
    StrategyScoreRead,
    StrategyVersionCreate,
    StrategyVersionRead,
    fallback_source,
    fixture_source,
    unknown_source,
)
from app.services.strategy_scoring import StrategyScoringService


@pytest.fixture()
def db_session() -> Session:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    with session_factory() as session:
        yield session


def write_strategy_file(output_dir: Path, filename: str, code: str) -> tuple[Path, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    file_path = output_dir / filename
    file_path.write_text(code, encoding="utf-8")
    return file_path, hashlib.sha256(code.encode("utf-8")).hexdigest()


def strategy_file_snapshot(output_dir: Path, checksum: str) -> dict:
    return {
        "strategy_file_validation": {
            "approved_root": str(output_dir),
            "checksum": checksum,
            "validation_status": "passed",
            "write_status": "written",
        }
    }


def test_core_database_read_models_include_traceable_database_source(
    db_session: Session,
    tmp_path: Path,
) -> None:
    strategy_repository = StrategyRepository(db_session)
    strategy = strategy_repository.create(
        StrategyCreate(name="Phase 8 Source Contract", slug="phase8-source-contract")
    )
    strategy_code = "class Phase8SourceContract:\n    pass\n"
    strategy_file, checksum = write_strategy_file(
        tmp_path / "generated",
        "phase8_source_contract.py",
        strategy_code,
    )
    version = strategy_repository.create_version(
        StrategyVersionCreate(
            strategy_id=strategy.id,
            blueprint={"class_name": "Phase8SourceContract"},
            generated_code=strategy_code,
            code_hash=checksum,
            file_path=str(strategy_file),
            validation_status="passed",
            diff_snapshot=strategy_file_snapshot(strategy_file.parent, checksum),
        )
    )
    assert version is not None

    strategy_read = StrategyRead.model_validate(strategy)
    version_read = StrategyVersionRead.model_validate(version)

    assert strategy_read.data_source.source_type == "database"
    assert strategy_read.data_source.core_data is True
    assert strategy_read.data_source.database_ids == {"strategy_id": strategy.id}
    assert version_read.data_source.database_ids["strategy_version_id"] == version.id
    assert version_read.data_source.database_ids["strategy_id"] == strategy.id
    assert version_read.file_state.status == "READY"
    assert version_read.file_state.checksum_matches is True
    assert (
        version_read.data_source.artifact_refs["strategy_file_path"]
        == str(strategy_file)
    )


def test_strategy_version_read_marks_missing_file_as_non_core(
    db_session: Session,
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "generated"
    output_dir.mkdir()
    strategy_repository = StrategyRepository(db_session)
    strategy = strategy_repository.create(
        StrategyCreate(name="Missing File Source Contract", slug="missing-file-source-contract")
    )
    code = "class MissingFileSourceContract:\n    pass\n"
    checksum = hashlib.sha256(code.encode("utf-8")).hexdigest()
    missing_file = output_dir / "missing_file_source_contract.py"
    version = strategy_repository.create_version(
        StrategyVersionCreate(
            strategy_id=strategy.id,
            blueprint={"class_name": "MissingFileSourceContract"},
            generated_code=code,
            code_hash=checksum,
            file_path=str(missing_file),
            validation_status="passed",
            diff_snapshot=strategy_file_snapshot(output_dir, checksum),
        )
    )
    assert version is not None

    version_read = StrategyVersionRead.model_validate(version)

    assert version_read.file_state.status == "BLOCKED"
    assert version_read.file_state.exists is False
    assert "strategy file does not exist" in (version_read.file_state.blocked_reason or "")
    assert version_read.data_source.source_type == "database"
    assert version_read.data_source.core_data is False
    assert "strategy file does not exist" in (version_read.data_source.blocked_reason or "")


def test_strategy_version_read_marks_tampered_file_as_failed(
    db_session: Session,
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "generated"
    original_code = "class TamperedFileSourceContract:\n    value = 1\n"
    strategy_file, checksum = write_strategy_file(
        output_dir,
        "tampered_file_source_contract.py",
        original_code,
    )
    strategy_repository = StrategyRepository(db_session)
    strategy = strategy_repository.create(
        StrategyCreate(name="Tampered File Source Contract", slug="tampered-file-source-contract")
    )
    version = strategy_repository.create_version(
        StrategyVersionCreate(
            strategy_id=strategy.id,
            blueprint={"class_name": "TamperedFileSourceContract"},
            generated_code=original_code,
            code_hash=checksum,
            file_path=str(strategy_file),
            validation_status="passed",
            diff_snapshot=strategy_file_snapshot(output_dir, checksum),
        )
    )
    assert version is not None
    strategy_file.write_text("class TamperedFileSourceContract:\n    value = 2\n", encoding="utf-8")

    version_read = StrategyVersionRead.model_validate(version)

    assert version_read.file_state.status == "FAILED"
    assert version_read.file_state.checksum_matches is False
    assert "checksum does not match" in (version_read.file_state.blocked_reason or "")
    assert version_read.data_source.core_data is False
    assert version_read.data_source.artifact_refs["strategy_file_state"] == "FAILED"


def test_backtest_and_score_read_models_include_database_traceability(db_session: Session) -> None:
    strategy_repository = StrategyRepository(db_session)
    strategy = strategy_repository.create(
        StrategyCreate(name="Phase 8 Backtest Trace", slug="phase8-backtest-trace")
    )
    version = strategy_repository.create_version(
        StrategyVersionCreate(
            strategy_id=strategy.id,
            blueprint={"class_name": "Phase8BacktestTrace"},
            generated_code="class Phase8BacktestTrace: pass",
            file_path="user_data/strategies/generated/phase8_backtest_trace.py",
        )
    )
    assert version is not None

    backtest_repository = BacktestRepository(db_session)
    run = backtest_repository.create_run(
        BacktestRunCreate(strategy_version_id=version.id, profile_name="phase8-local")
    )
    assert run is not None
    task = backtest_repository.create_task(
        run.id,
        BacktestTaskCreate(
            pair="BTC/USDT",
            timeframe="15m",
            config_path="reports/phase8/config.json",
        ),
    )
    assert task is not None
    result = backtest_repository.save_result(
        task.id,
        BacktestResultCreate(
            result_path="reports/phase8/backtest-result.json",
            profit_pct=0.08,
            max_drawdown_pct=0.02,
            win_rate=0.62,
            total_trades=20,
        ),
    )
    assert result is not None

    score = StrategyScoringService(db_session).score_backtest_result(result.id)
    assert score is not None
    ranking = StrategyScoreRepository(db_session).list_ranking()

    assert BacktestRunRead.model_validate(run).data_source.database_ids == {
        "backtest_run_id": run.id,
        "strategy_version_id": version.id,
    }
    task_source = BacktestTaskRead.model_validate(task).data_source
    assert task_source.database_ids["backtest_task_id"] == task.id
    assert task_source.artifact_refs["config_path"] == "reports/phase8/config.json"
    result_source = BacktestResultRead.model_validate(result).data_source
    assert result_source.database_ids["backtest_result_id"] == result.id
    assert result_source.artifact_refs["result_path"] == "reports/phase8/backtest-result.json"
    score_source = StrategyScoreRead.model_validate(score).data_source
    assert score_source.database_ids["backtest_result_id"] == result.id
    assert ranking[0].data_source.source_type == "api_aggregate"
    assert ranking[0].data_source.core_data is True
    assert ranking[0].data_source.database_ids["strategy_score_id"] == score.id
    assert ranking[0].data_source.database_ids["backtest_result_id"] == result.id


def test_fixture_fallback_and_unknown_sources_cannot_claim_core_success() -> None:
    assert fixture_source("local fixture").core_data is False
    assert fallback_source("frontend fallback").core_data is False
    assert unknown_source("unknown source").core_data is False

    with pytest.raises(ValidationError, match="fallback data cannot satisfy core success"):
        DataSourceTrace(
            source_type="fallback",
            source_detail="frontend fallback",
            core_data=True,
        )
