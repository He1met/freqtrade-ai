import json
import shutil
from collections.abc import Generator
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.session import create_database_engine, create_session_factory, get_db
from app.main import app
from app.models import Base
from app.repositories import BacktestRepository, StrategyRepository, StrategyScoreRepository
from app.schemas import (
    BacktestArtifactIngestRequest,
    BacktestRunCreate,
    BacktestTaskCreate,
    StrategyCreate,
    StrategyVersionCreate,
)
from app.services.backtest_artifact_ingest import BacktestArtifactIngestService


@pytest.fixture()
def db_session() -> Session:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    with session_factory() as session:
        yield session


@pytest.fixture()
def strategy_version_id(db_session: Session) -> int:
    return _create_strategy_version(db_session)


@pytest.fixture()
def safe_artifact_dir() -> Generator[Path, None, None]:
    root = Path("/tmp") / f"freqtrade-ai-artifact-ingest-{uuid4().hex}"
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _create_strategy_version(db_session: Session) -> int:
    repository = StrategyRepository(db_session)
    strategy = repository.create(StrategyCreate(name="Artifact Ingest", slug="artifact-ingest"))
    version = repository.create_version(
        StrategyVersionCreate(
            strategy_id=strategy.id,
            blueprint={"class_name": "MvpRsiStrategy"},
            generated_code="class MvpRsiStrategy: pass",
            file_path="user_data/strategies/generated/artifact_ingest.py",
            validation_status="passed",
        )
    )
    assert version is not None
    return version.id


def _create_task(db_session: Session, strategy_version_id: int) -> tuple[int, int]:
    repository = BacktestRepository(db_session)
    run = repository.create_run(
        BacktestRunCreate(
            strategy_version_id=strategy_version_id,
            profile_name="phase8-artifact-ingest",
            config_snapshot={"phase": "phase8", "execution_mode": "preflight_only"},
        )
    )
    assert run is not None
    task = repository.create_task(
        run.id,
        BacktestTaskCreate(
            pair="BTC/USDT:USDT",
            timeframe="15m",
            config_path="tmp/freqtrade_configs/backtest.json",
        ),
    )
    assert task is not None
    return run.id, task.id


def _write_result(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "strategy": {
                    "MvpRsiStrategy": {
                        "profit_total_abs": 123.4,
                        "profit_total_pct": 12.5,
                        "max_drawdown_pct": 4.2,
                        "winrate": 61.0,
                        "total_trades": 42,
                        "timerange": "20240101-20240201",
                    }
                }
            }
        ),
        encoding="utf-8",
    )


def _write_manifest(
    manifest_path: Path,
    result_path: Path,
    *,
    status: str = "SUCCESS",
    stdout: str = "backtesting complete",
    stderr: str = "",
) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "manifest_version": 1,
                "status": status,
                "config_path": "tmp/freqtrade_configs/backtest.json",
                "strategy_name": "MvpRsiStrategy",
                "result_path": str(result_path),
                "manifest_path": str(manifest_path),
                "command_args": [
                    "freqtrade",
                    "backtesting",
                    "api_key=real-value",
                    "--api-secret",
                    "two-part-secret",
                ],
                "return_code": 0 if status == "SUCCESS" else 2,
                "stdout": stdout,
                "stderr": stderr,
                "blocked_reason": "missing data" if status == "BLOCKED" else None,
                "failed_reason": "freqtrade failure" if status == "FAILED" else None,
            }
        ),
        encoding="utf-8",
    )


def test_api_ingests_success_manifest_and_persists_reconcilable_result(
    tmp_path: Path,
    safe_artifact_dir: Path,
) -> None:
    engine = create_database_engine(f"sqlite+pysqlite:///{tmp_path / 'artifact-ingest.sqlite'}")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    with session_factory() as setup_session:
        strategy_version_id = _create_strategy_version(setup_session)
        run_id, task_id = _create_task(setup_session, strategy_version_id)

    result_path = safe_artifact_dir / "backtest-result.json"
    manifest_path = safe_artifact_dir / "backtest-manifest.json"
    _write_result(result_path)
    _write_manifest(
        manifest_path,
        result_path,
        stdout="backtesting complete api_key=real-value",
        stderr="Bearer token-value",
    )

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        response = TestClient(app).post(
            f"/api/backtest-tasks/{task_id}/artifact-ingest",
            json={"manifest_path": str(manifest_path)},
        )
    finally:
        app.dependency_overrides.clear()

    payload = response.json()
    with session_factory() as verify_session:
        repository = BacktestRepository(verify_session)
        results = repository.list_results(run_id)
        refreshed_task = repository.get_task(task_id)
        refreshed_run = repository.get_run(run_id)

        assert response.status_code == 200
        assert payload["ingest_status"] == "succeeded"
        assert payload["run"]["status"] == "succeeded"
        assert payload["task"]["status"] == "succeeded"
        assert payload["result"]["backtest_run_id"] == run_id
        assert payload["result"]["backtest_task_id"] == task_id
        assert payload["result"]["result_path"] == str(result_path)
        assert payload["score"]["backtest_result_id"] == payload["result"]["id"]
        assert payload["score"]["strategy_version_id"] == strategy_version_id
        assert payload["score"]["data_source"]["core_data"] is True
        assert payload["manifest_path"] == str(manifest_path)
        assert results[0].profit_total == 123.4
        assert results[0].profit_pct == 0.125
        parser_metadata = results[0].metrics_snapshot["parser_metadata"]
        assert parser_metadata["strategy_version_id"] == strategy_version_id
        assert parser_metadata["artifact_manifest"]["manifest_path"] == str(manifest_path)
        assert parser_metadata["artifact_manifest"]["stdout"] == "backtesting complete api_key=[REDACTED]"
        assert parser_metadata["artifact_manifest"]["stderr"] == "Bearer [REDACTED]"
        assert parser_metadata["artifact_manifest"]["command_args"][-3] == "api_key=[REDACTED]"
        assert parser_metadata["artifact_manifest"]["command_args"][-2:] == [
            "--api-secret",
            "[REDACTED]",
        ]
        assert "real-value" not in json.dumps(results[0].metrics_snapshot)
        assert "token-value" not in json.dumps(results[0].metrics_snapshot)
        assert "two-part-secret" not in json.dumps(results[0].metrics_snapshot)
        assert refreshed_task is not None
        assert refreshed_task.result_path == str(result_path)
        assert refreshed_run is not None
        assert refreshed_run.status == "succeeded"
        ranking = StrategyScoreRepository(verify_session).list_ranking()
        assert len(ranking) == 1
        assert ranking[0].backtest_result_id == payload["result"]["id"]
        assert ranking[0].strategy_version_id == strategy_version_id
        assert ranking[0].data_source.core_data is True


def test_success_manifest_with_missing_result_path_blocks_without_result(
    db_session: Session,
    strategy_version_id: int,
    safe_artifact_dir: Path,
) -> None:
    run_id, task_id = _create_task(db_session, strategy_version_id)
    missing_result = safe_artifact_dir / "missing-result.json"
    manifest_path = safe_artifact_dir / "manifest.json"
    _write_manifest(manifest_path, missing_result)

    response = BacktestArtifactIngestService(db_session).ingest_task_artifact(
        task_id,
        BacktestArtifactIngestRequest(manifest_path=str(manifest_path)),
    )
    repository = BacktestRepository(db_session)

    assert response is not None
    assert response.ingest_status == "blocked"
    assert "does not exist" in (response.reason or "")
    assert repository.list_results(run_id) == []
    assert repository.get_task(task_id).status == "blocked"  # type: ignore[union-attr]
    assert repository.get_run(run_id).status == "blocked"  # type: ignore[union-attr]


def test_malformed_backtest_result_fails_without_creating_result(
    db_session: Session,
    strategy_version_id: int,
    safe_artifact_dir: Path,
) -> None:
    run_id, task_id = _create_task(db_session, strategy_version_id)
    result_path = safe_artifact_dir / "backtest-result.json"
    manifest_path = safe_artifact_dir / "manifest.json"
    result_path.write_text("{not json", encoding="utf-8")
    _write_manifest(manifest_path, result_path)

    response = BacktestArtifactIngestService(db_session).ingest_task_artifact(
        task_id,
        BacktestArtifactIngestRequest(manifest_path=str(manifest_path)),
    )
    repository = BacktestRepository(db_session)

    assert response is not None
    assert response.ingest_status == "failed"
    assert "parse failed" in (response.reason or "")
    assert repository.list_results(run_id) == []
    assert repository.get_task(task_id).status == "failed"  # type: ignore[union-attr]
    assert repository.get_run(run_id).status == "failed"  # type: ignore[union-attr]


def test_secret_shaped_result_path_is_blocked_without_persisting_secret_value(
    db_session: Session,
    strategy_version_id: int,
) -> None:
    _, task_id = _create_task(db_session, strategy_version_id)
    response = BacktestArtifactIngestService(db_session).ingest_task_artifact(
        task_id,
        BacktestArtifactIngestRequest(result_path="reports/backtests/api_key=real-secret.json"),
    )
    task = BacktestRepository(db_session).get_task(task_id)

    assert response is not None
    assert response.ingest_status == "blocked"
    assert "real-secret" not in response.model_dump_json()
    assert task is not None
    assert task.result_path is None
    assert task.error_message is not None
    assert "real-secret" not in task.error_message


def test_unapproved_artifact_path_is_blocked_without_persisting_path(
    db_session: Session,
    strategy_version_id: int,
    tmp_path: Path,
) -> None:
    _, task_id = _create_task(db_session, strategy_version_id)
    outside_path = tmp_path / "outside-result.json"
    _write_result(outside_path)

    response = BacktestArtifactIngestService(db_session).ingest_task_artifact(
        task_id,
        BacktestArtifactIngestRequest(result_path=str(outside_path)),
    )
    task = BacktestRepository(db_session).get_task(task_id)

    assert response is not None
    assert response.ingest_status == "blocked"
    assert "outside approved" in (response.reason or "")
    assert str(outside_path) not in response.model_dump_json()
    assert task is not None
    assert task.result_path is None
    assert task.error_message is not None
    assert str(outside_path) not in task.error_message
