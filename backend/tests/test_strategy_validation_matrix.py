import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
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
from app.services.backtest_artifact_ingest import backtest_ingest_receipt
from app.services.strategy_validation_matrix import (
    StrategyValidationBlocked,
    StrategyValidationMatrixService,
    ValidationWindowSpec,
    _market_data_digest,
    _market_data_lineage,
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


def _seed_primary(db: Session, tmp_path: Path, suffix: str = ""):
    tmp_path.mkdir(parents=True, exist_ok=True)
    strategy_path = tmp_path / f"MatrixStrategy{suffix}.py"
    strategy_path.write_text("class MatrixStrategy: pass\n", encoding="utf-8")
    strategies = StrategyRepository(db)
    strategy = strategies.create(
        StrategyCreate(name=f"Matrix{suffix}", slug=f"matrix{suffix}")
    )
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


def _write_market_data(datadir: Path, *, data_format: str = "json") -> None:
    datadir.mkdir()
    rows = [
        {"date": "2024-01-01T00:00:00Z", "close": 100},
        {"date": "2024-01-31T23:45:00Z", "close": 102},
        {"date": "2024-02-01T00:00:00Z", "close": 100},
        {"date": "2024-02-29T23:45:00Z", "close": 110},
        {"date": "2024-03-01T00:00:00Z", "close": 110},
        {"date": "2024-03-31T23:45:00Z", "close": 90},
        {"date": "2024-04-01T00:00:00Z", "close": 100},
        {"date": "2024-04-30T23:45:00Z", "close": 102},
    ]
    if data_format == "feather":
        import pandas as pd

        pd.DataFrame(rows).to_feather(datadir / "BTC_USDT_USDT-15m.feather")
        return
    if data_format != "json":
        raise ValueError("unsupported market-data test format")
    (datadir / "BTC_USDT_USDT-15m.json").write_text(
        json.dumps(rows), encoding="utf-8"
    )


def _attach_real_results(
    db: Session,
    plan,
    tmp_path: Path,
    *,
    market_data_format: str = "json",
) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    datadir = tmp_path / "market"
    _write_market_data(datadir, data_format=market_data_format)
    market_data_files = _market_data_lineage(
        datadir, pair="BTC/USDT:USDT", timeframe="15m"
    )
    market_digest = _market_data_digest(
        datadir, pair="BTC/USDT:USDT", timeframe="15m"
    )
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
        checksums = {
            "config": _sha(config_path),
            "result": _sha(result_path),
            "strategy": plan.strategy_code_digest,
            "market_data": market_digest,
        }
        manifest_path.write_text(
            json.dumps(
                {
                    "manifest_version": 2,
                    "status": "SUCCESS",
                    "execution_scope_id": "LOCAL_DRY_RUN",
                    "run_id": run.id,
                    "task_id": task.id,
                    "strategy_version_id": plan.strategy_version_id,
                    "execution_id": execution_id,
                    "result_path": str(result_path),
                    "config_path": str(config_path),
                    "strategy_path": plan.strategy_version.file_path,
                    "datadir": str(datadir),
                    "pair": task.pair,
                    "timeframe": task.timeframe,
                    "market_data_files": market_data_files,
                    "checksums": checksums,
                    "return_code": 0,
                    "command_args": [
                        "freqtrade",
                        "backtesting",
                        "--export",
                        "trades",
                    ],
                }
            ),
            encoding="utf-8",
        )
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
                    "ingest_receipt": backtest_ingest_receipt(
                        manifest_checksum=_sha(manifest_path),
                        backtest_run_id=run.id,
                        backtest_task_id=task.id,
                        strategy_version_id=plan.strategy_version_id,
                        execution_id=execution_id,
                    ),
                    "artifact_manifest": {
                        "provider": "freqtrade",
                        "status": "SUCCESS",
                        "manifest_version": 2,
                        "execution_id": execution_id,
                        "manifest_path": str(manifest_path),
                        "result_path": str(result_path),
                        "config_path": str(config_path),
                        "datadir": str(datadir),
                        "pair": task.pair,
                        "timeframe": task.timeframe,
                        "market_data_files": market_data_files,
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
    _write_market_data(datadir)
    market_digest = _market_data_digest(
        datadir, pair="BTC/USDT:USDT", timeframe="15m"
    )
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
    assert assessment["policy"]["policy_version"] == "strategy-promotion-v1"

    first_window_result = db.get(
        BacktestResult, plan.windows[0].backtest_result_id
    )
    assert first_window_result is not None
    Path(first_window_result.result_path).write_text("tampered-after-pass", encoding="utf-8")
    with pytest.raises(StrategyPromotionBlocked, match="stale or invalid"):
        assess_strategy_promotion(primary, score, strategy_version=version)
    assert service.evaluate(plan.id).status == "BLOCKED"


def test_independent_matrix_reads_real_feather_and_persists_regime_evidence(
    db: Session, tmp_path: Path
) -> None:
    version, primary = _seed_primary(db, tmp_path)
    datadir = tmp_path / "declared-market"
    _write_market_data(datadir, data_format="feather")
    market_digest = _market_data_digest(
        datadir, pair="BTC/USDT:USDT", timeframe="15m"
    )
    assert market_digest is not None
    service = StrategyValidationMatrixService(db)
    plan = service.declare(
        promotion_backtest_result_id=primary.id,
        strategy_version_id=version.id,
        windows=_specs(market_digest),
    )
    _attach_real_results(
        db,
        plan,
        tmp_path / "feather-validation",
        market_data_format="feather",
    )

    plan = service.evaluate(plan.id)

    assert plan.status == "PASSED"
    for window in plan.windows:
        evidence = window.market_state_evidence
        assert evidence["source"] == "persisted_market_data"
        assert evidence["source_artifacts"] == ["BTC_USDT_USDT-15m.feather"]
        assert evidence["observation_count"] >= 2


def test_market_regime_ignores_other_pairs_and_timeframes(
    db: Session, tmp_path: Path
) -> None:
    version, primary = _seed_primary(db, tmp_path)
    declared_datadir = tmp_path / "declared-market"
    _write_market_data(declared_datadir)
    digest = _market_data_digest(
        declared_datadir, pair="BTC/USDT:USDT", timeframe="15m"
    )
    assert digest is not None
    service = StrategyValidationMatrixService(db)
    plan = service.declare(
        promotion_backtest_result_id=primary.id,
        strategy_version_id=version.id,
        windows=_specs(digest),
    )
    validation_root = tmp_path / "mixed-validation"
    _attach_real_results(db, plan, validation_root)
    runtime_datadir = validation_root / "market"
    pollution = [
        {"date": "2024-01-01T00:00:00Z", "close": 1000},
        {"date": "2024-04-30T23:45:00Z", "close": 1},
    ]
    (runtime_datadir / "ETH_USDT_USDT-15m.json").write_text(
        json.dumps(pollution), encoding="utf-8"
    )
    (runtime_datadir / "BTC_USDT_USDT-1h.json").write_text(
        json.dumps(pollution), encoding="utf-8"
    )

    evaluated = service.evaluate(plan.id)

    assert evaluated.status == "PASSED"
    for window in evaluated.windows:
        assert window.market_state_evidence["pair"] == "BTC/USDT:USDT"
        assert window.market_state_evidence["timeframe"] == "15m"
        assert window.market_state_evidence["source_artifacts"] == [
            "BTC_USDT_USDT-15m.json"
        ]


def test_missing_or_drifted_market_lineage_blocks_validation(
    db: Session, tmp_path: Path
) -> None:
    version, primary = _seed_primary(db, tmp_path)
    declared_datadir = tmp_path / "declared-market"
    _write_market_data(declared_datadir)
    digest = _market_data_digest(
        declared_datadir, pair="BTC/USDT:USDT", timeframe="15m"
    )
    assert digest is not None
    service = StrategyValidationMatrixService(db)
    plan = service.declare(
        promotion_backtest_result_id=primary.id,
        strategy_version_id=version.id,
        windows=_specs(digest),
    )
    validation_root = tmp_path / "lineage-validation"
    _attach_real_results(db, plan, validation_root)
    first = plan.windows[0]
    result = db.query(BacktestResult).filter_by(
        backtest_run_id=first.backtest_run_id
    ).one()
    snapshot = dict(result.metrics_snapshot)
    metadata = dict(snapshot["parser_metadata"])
    manifest = dict(metadata["artifact_manifest"])
    manifest.pop("market_data_files", None)
    metadata["artifact_manifest"] = manifest
    snapshot["parser_metadata"] = metadata
    result.metrics_snapshot = snapshot
    db.commit()

    blocked = service.evaluate(plan.id)

    assert blocked.status == "BLOCKED"
    assert "exact market-data file lineage is missing" in (
        blocked.blocked_reason or ""
    )

    db.rollback()
    version2, primary2 = _seed_primary(db, tmp_path, suffix="drift")
    plan2 = service.declare(
        promotion_backtest_result_id=primary2.id,
        strategy_version_id=version2.id,
        windows=_specs(digest),
    )
    validation_root2 = tmp_path / "lineage-drift-validation"
    _attach_real_results(db, plan2, validation_root2)
    (validation_root2 / "market" / "BTC_USDT_USDT-15m.json").write_text(
        "drifted", encoding="utf-8"
    )

    drifted = service.evaluate(plan2.id)

    assert drifted.status == "BLOCKED"
    assert "file lineage drift" in (drifted.blocked_reason or "")


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

    primary_overlap = _specs(digest)
    primary_overlap[0] = ValidationWindowSpec(
        window_kind="OOS",
        timerange="20231215-20240115",
        profile={},
        expected_market_data_digest=digest,
    )
    with pytest.raises(StrategyValidationBlocked, match="primary"):
        service.declare(
            promotion_backtest_result_id=primary.id,
            strategy_version_id=version.id,
            windows=primary_overlap,
        )


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("fixture", "fixture/offline"),
        ("metadata", "session-backed artifact ingest receipt"),
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
    elif mutation == "metadata":
        snapshot = dict(result.metrics_snapshot)
        metadata = dict(snapshot["parser_metadata"])
        manifest = dict(metadata["artifact_manifest"])
        manifest["execution_id"] = "hand-made-fixture-execution"
        metadata["artifact_manifest"] = manifest
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


def test_primary_lineage_reuse_and_untrusted_market_label_are_blocked(
    db: Session, tmp_path: Path
) -> None:
    version, primary = _seed_primary(db, tmp_path)
    datadir = tmp_path / "declared-market"
    _write_market_data(datadir)
    digest = _market_data_digest(
        datadir, pair="BTC/USDT:USDT", timeframe="15m"
    )
    assert digest is not None
    service = StrategyValidationMatrixService(db)
    plan = service.declare(
        promotion_backtest_result_id=primary.id,
        strategy_version_id=version.id,
        windows=_specs(digest),
    )
    first = plan.windows[0]
    first.backtest_run_id = primary.backtest_run_id
    first.backtest_task_id = primary.backtest_task_id
    db.commit()
    plan = service.evaluate(plan.id)
    assert plan.status == "BLOCKED"
    assert "primary promotion lineage" in (plan.blocked_reason or "")

    version2, primary2 = _seed_primary(db, tmp_path, suffix="regime")
    swapped = _specs(digest)
    swapped[1] = ValidationWindowSpec(
        window_kind="WALK_FORWARD",
        market_state="bear",
        timerange=swapped[1].timerange,
        profile=swapped[1].profile,
        expected_market_data_digest=digest,
    )
    swapped[2] = ValidationWindowSpec(
        window_kind="WALK_FORWARD",
        market_state="bull",
        timerange=swapped[2].timerange,
        profile=swapped[2].profile,
        expected_market_data_digest=digest,
    )
    plan2 = service.declare(
        promotion_backtest_result_id=primary2.id,
        strategy_version_id=version2.id,
        windows=swapped,
    )
    _attach_real_results(db, plan2, tmp_path / "regime-run")
    assert service.evaluate(plan2.id).status == "BLOCKED"
    assert "computed market regime" in (plan2.blocked_reason or "")


def test_execution_id_is_globally_unique(db: Session, tmp_path: Path) -> None:
    version, primary = _seed_primary(db, tmp_path)
    plan = StrategyValidationMatrixService(db).declare(
        promotion_backtest_result_id=primary.id,
        strategy_version_id=version.id,
        windows=_specs("a" * 64),
    )
    plan.windows[0].execution_id = "same-execution"
    plan.windows[1].execution_id = "same-execution"
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


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
