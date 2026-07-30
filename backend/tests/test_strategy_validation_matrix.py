import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.db.session import create_database_engine, create_session_factory
from app.models import (
    BacktestResult,
    BacktestRun,
    Base,
    StrategyScore,
)
from app.repositories import BacktestRepository, StrategyRepository
from app.schemas import (
    BacktestRunCreate,
    BacktestTaskCreate,
    StrategyCreate,
    StrategyVersionCreate,
)
from app.services.strategy_promotion import (
    StrategyPromotionBlocked,
    assess_strategy_promotion,
)
from app.services.strategy_validation_matrix import (
    StrategyValidationBlocked,
    StrategyValidationMatrixService,
    ValidationWindowSpec,
    _market_data_digest,
)


@pytest.fixture()
def db() -> Session:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory() as session:
        yield session


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _seed_primary(db: Session, tmp_path: Path):
    strategy_path = tmp_path / "MatrixStrategy.py"
    strategy_path.write_text("class MatrixStrategy: pass\n", encoding="utf-8")
    strategies = StrategyRepository(db)
    strategy = strategies.create(StrategyCreate(name="Matrix", slug="matrix"))
    version = strategies.create_version(
        StrategyVersionCreate(
            strategy_id=strategy.id,
            blueprint={"class_name": "MatrixStrategy"},
            generated_code=strategy_path.read_text(),
            code_hash=_sha(strategy_path),
            file_path=str(strategy_path),
            validation_status="passed",
        )
    )
    assert version is not None
    backtests = BacktestRepository(db)
    run = backtests.create_run(
        BacktestRunCreate(
            strategy_version_id=version.id,
            profile_name="primary",
            config_snapshot={"provider": "freqtrade"},
        )
    )
    assert run is not None
    task = backtests.create_task(
        run.id, BacktestTaskCreate(pair="BTC/USDT:USDT", timeframe="15m")
    )
    assert task is not None
    primary = BacktestResult(
        backtest_run_id=run.id,
        backtest_task_id=task.id,
        result_path=str(tmp_path / "primary.json"),
        metrics_snapshot={},
        profit_pct=0.08,
        max_drawdown_pct=0.10,
        total_trades=50,
        timerange="20230101-20240101",
    )
    db.add(primary)
    db.commit()
    db.refresh(primary)
    return version, primary


def _specs(market_digest: str) -> list[ValidationWindowSpec]:
    return [
        ValidationWindowSpec(
            window_kind="OOS",
            timerange="20240101-20240201",
            profile={"profile_name": "oos"},
            expected_market_data_digest=market_digest,
        ),
        ValidationWindowSpec(
            window_kind="WALK_FORWARD",
            market_state="bull",
            timerange="20240201-20240301",
            profile={"profile_name": "wf-bull"},
            expected_market_data_digest=market_digest,
        ),
        ValidationWindowSpec(
            window_kind="WALK_FORWARD",
            market_state="bear",
            timerange="20240301-20240401",
            profile={"profile_name": "wf-bear"},
            expected_market_data_digest=market_digest,
        ),
        ValidationWindowSpec(
            window_kind="WALK_FORWARD",
            market_state="range",
            timerange="20240401-20240501",
            profile={"profile_name": "wf-range"},
            expected_market_data_digest=market_digest,
        ),
    ]


def _attach_real_results(db: Session, plan, tmp_path: Path) -> None:
    datadir = tmp_path / "market"
    datadir.mkdir()
    (datadir / "BTC_USDT_USDT-15m.feather").write_bytes(b"declared")
    market_digest = _market_data_digest(datadir)
    assert market_digest is not None
    backtests = BacktestRepository(db)
    execution_ids = []
    for window in plan.windows:
        config_path = tmp_path / f"config-{window.ordinal}.json"
        result_path = tmp_path / f"result-{window.ordinal}.json"
        manifest_path = tmp_path / f"manifest-{window.ordinal}.json"
        config_path.write_text(
            json.dumps({"timerange": window.timerange}), encoding="utf-8"
        )
        result_path.write_text(
            json.dumps({"window": window.ordinal}), encoding="utf-8"
        )
        run = backtests.create_run(
            BacktestRunCreate(
                strategy_version_id=plan.strategy_version_id,
                profile_name=f"validation-{window.ordinal}",
                config_snapshot={"timerange": window.timerange},
            )
        )
        assert run is not None
        task = backtests.create_task(
            run.id,
            BacktestTaskCreate(
                pair="BTC/USDT:USDT",
                timeframe="15m",
                config_path=str(config_path),
            ),
        )
        assert task is not None
        execution_id = f"validation-plan-{plan.id}-window-{window.ordinal}"
        execution_ids.append(execution_id)
        manifest_path.write_text(
            json.dumps({"execution_id": execution_id, "window": window.ordinal}),
            encoding="utf-8",
        )
        checksums = {
            "config": _sha(config_path),
            "result": _sha(result_path),
            "strategy": plan.strategy_code_digest,
            "market_data": market_digest,
        }
        result = BacktestResult(
            backtest_run_id=run.id,
            backtest_task_id=task.id,
            result_path=str(result_path),
            timerange=window.timerange,
            profit_pct=0.02,
            max_drawdown_pct=0.08,
            total_trades=35,
            metrics_snapshot={
                "parser_metadata": {
                    "ingest_source": "local_backtest_artifact_ingest",
                    "artifact_manifest": {
                        "provider": "freqtrade",
                        "status": "SUCCESS",
                        "manifest_version": 2,
                        "execution_id": execution_id,
                        "manifest_path": str(manifest_path),
                        "result_path": str(result_path),
                        "config_path": str(config_path),
                        "datadir": str(datadir),
                        "manifest_checksum": _sha(manifest_path),
                        "checksums": checksums,
                    },
                }
            },
        )
        db.add(result)
        run.status = "succeeded"
        task.status = "succeeded"
        window.backtest_run_id = run.id
        window.backtest_task_id = task.id
        window.expected_config_digest = checksums["config"]
        db.commit()
    assert len(set(execution_ids)) == 4


def test_independent_matrix_persists_four_runs_and_is_required_for_promotion(
    db: Session, tmp_path: Path
) -> None:
    version, primary = _seed_primary(db, tmp_path)
    datadir = tmp_path / "declared-market"
    datadir.mkdir()
    (datadir / "BTC_USDT_USDT-15m.feather").write_bytes(b"declared")
    market_digest = _market_data_digest(datadir)
    assert market_digest is not None
    service = StrategyValidationMatrixService(db)
    plan = service.declare(
        promotion_backtest_result_id=primary.id,
        strategy_version_id=version.id,
        windows=_specs(market_digest),
    )
    _attach_real_results(db, plan, tmp_path)

    plan = service.evaluate(plan.id)

    assert plan.status == "PASSED"
    assert len({window.backtest_run_id for window in plan.windows}) == 4
    assert len({window.backtest_task_id for window in plan.windows}) == 4
    assert len({window.backtest_result_id for window in plan.windows}) == 4
    assert plan.promotion_evidence["market_states"] == ["bear", "bull", "range"]
    score = StrategyScore(
        id=99,
        strategy_id=version.strategy_id,
        strategy_version_id=version.id,
        backtest_result_id=primary.id,
        metrics_snapshot={"source": "backtest_result", "backtest_result_id": primary.id},
    )
    assessment = assess_strategy_promotion(
        primary,
        score,
        strategy_version=version,
    )
    assert assessment["net_of_costs"] is True


def test_plan_rejects_missing_market_state_and_overlapping_windows(
    db: Session, tmp_path: Path
) -> None:
    version, primary = _seed_primary(db, tmp_path)
    digest = "a" * 64
    missing = _specs(digest)[:-1]
    service = StrategyValidationMatrixService(db)
    with pytest.raises(StrategyValidationBlocked, match="one OOS and at least three"):
        service.declare(
            promotion_backtest_result_id=primary.id,
            strategy_version_id=version.id,
            windows=missing,
        )

    overlap = _specs(digest)
    overlap[1] = ValidationWindowSpec(
        window_kind="WALK_FORWARD",
        market_state="bull",
        timerange="20240115-20240301",
        profile={},
        expected_market_data_digest=digest,
    )
    with pytest.raises(StrategyValidationBlocked, match="overlap"):
        service.declare(
            promotion_backtest_result_id=primary.id,
            strategy_version_id=version.id,
            windows=overlap,
        )


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("fixture", "fixture/offline"),
        ("checksum", "result checksum drift"),
        ("missing", "run/task is not succeeded"),
    ],
)
def test_fixture_checksum_and_missing_window_fail_closed(
    db: Session, tmp_path: Path, mutation: str, expected: str
) -> None:
    version, primary = _seed_primary(db, tmp_path)
    service = StrategyValidationMatrixService(db)
    plan = service.declare(
        promotion_backtest_result_id=primary.id,
        strategy_version_id=version.id,
        windows=_specs("a" * 64),
    )
    _attach_real_results(db, plan, tmp_path)
    first = plan.windows[0]
    result = db.query(BacktestResult).filter_by(
        backtest_run_id=first.backtest_run_id
    ).one()
    if mutation == "fixture":
        snapshot = dict(result.metrics_snapshot)
        metadata = dict(snapshot["parser_metadata"])
        metadata["ingest_source"] = "fixture"
        snapshot["parser_metadata"] = metadata
        result.metrics_snapshot = snapshot
    elif mutation == "checksum":
        Path(result.result_path).write_text("tampered", encoding="utf-8")
    else:
        result.run.status = "blocked"
    db.commit()

    plan = service.evaluate(plan.id)

    assert plan.status == "BLOCKED"
    assert expected in (plan.blocked_reason or "")
    assert plan.evidence_digest is None
    with pytest.raises(StrategyPromotionBlocked):
        assess_strategy_promotion(
            primary,
            StrategyScore(
                id=100,
                strategy_id=version.strategy_id,
                strategy_version_id=version.id,
                backtest_result_id=primary.id,
                metrics_snapshot={},
            ),
            strategy_version=version,
        )


def test_single_result_trade_slices_cannot_claim_oos_or_walk_forward() -> None:
    result = BacktestResult(
        id=1,
        profit_pct=0.10,
        max_drawdown_pct=0.05,
        total_trades=100,
        metrics_snapshot={
            "trades": [{"profit": 1}] * 100,
            "promotion_evidence": {
                "net_of_costs": True,
                "out_of_sample": {"passed": True, "profit_pct": 1, "total_trades": 30},
                "walk_forward": {
                    "passed": True,
                    "market_states": ["bull", "bear", "range"],
                },
            },
        },
    )
    score = StrategyScore(
        id=2,
        strategy_id=1,
        strategy_version_id=1,
        backtest_result_id=1,
        metrics_snapshot={},
    )
    with pytest.raises(StrategyPromotionBlocked, match="validation_matrix"):
        assess_strategy_promotion(result, score)


def test_prepare_recovery_does_not_duplicate_backtest_after_crash(
    db: Session, tmp_path: Path
) -> None:
    version, primary = _seed_primary(db, tmp_path)
    plan = StrategyValidationMatrixService(db).declare(
        promotion_backtest_result_id=primary.id,
        strategy_version_id=version.id,
        windows=_specs("a" * 64),
    )

    class CrashAfterPersist:
        def __init__(self) -> None:
            self.calls = 0

        def trigger(self, payload):
            self.calls += 1
            repository = BacktestRepository(db)
            run = repository.create_run(
                BacktestRunCreate(
                    strategy_version_id=version.id,
                    profile_name=payload.profile["profile_name"],
                    config_snapshot={"profile": payload.profile},
                )
            )
            assert run is not None
            task = repository.create_task(
                run.id,
                BacktestTaskCreate(pair="BTC/USDT:USDT", timeframe="15m"),
            )
            assert task is not None
            raise RuntimeError("simulated crash after trigger commit")

    crashing = CrashAfterPersist()
    with pytest.raises(RuntimeError, match="simulated crash"):
        StrategyValidationMatrixService(
            db, trigger_service=crashing
        ).prepare_runs(plan.id)
    assert crashing.calls == 1

    class StopAtNextWindow:
        def trigger(self, payload):
            raise RuntimeError("stop before preparing second window")

    with pytest.raises(RuntimeError, match="second window"):
        StrategyValidationMatrixService(
            db, trigger_service=StopAtNextWindow()
        ).prepare_runs(plan.id)
    db.refresh(plan)

    assert plan.windows[0].backtest_run_id is not None
    assert (
        db.query(BacktestRun)
        .filter(BacktestRun.profile_name.like("validation-%"))
        .count()
        == 1
    )
