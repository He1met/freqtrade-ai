import hashlib
import json
import subprocess
from pathlib import Path

from sqlalchemy.orm import Session

from app.adapters.freqtrade.cli_runner import FreqtradeCliRunner
from app.adapters.freqtrade.config_builder import FreqtradeConfigBuilder
from app.adapters.freqtrade.dry_run_runner import (
    FreqtradeDryRunArtifactManifest,
    FreqtradeDryRunRunner,
)
from app.core.config import Settings
from app.db.session import create_database_engine, create_session_factory
from app.models import Base
from app.repositories import StrategyRepository
from app.schemas import StrategyCreate, StrategyVersionCreate
from app.schemas.dry_run_control import DryRunControlStartRequest, DryRunControlStopRequest
from app.services.dry_run_control import DryRunControlService


def session_factory(tmp_path: Path):
    engine = create_database_engine(f"sqlite+pysqlite:///{tmp_path / 'control.sqlite'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def seed_strategy_version(db: Session, tmp_path: Path) -> int:
    class_name = "PhaseEightControlledDryRun"
    code = f"class {class_name}:\n    pass\n"
    strategy_file = tmp_path / "user_data" / "strategies" / "generated" / f"{class_name}.py"
    strategy_file.parent.mkdir(parents=True, exist_ok=True)
    strategy_file.write_text(code, encoding="utf-8")
    repository = StrategyRepository(db)
    strategy = repository.create(StrategyCreate(name=class_name, slug="phase-eight-controlled-dry-run"))
    version = repository.create_version(
        StrategyVersionCreate(
            strategy_id=strategy.id,
            blueprint={"class_name": class_name},
            generated_code=code,
            code_hash=hashlib.sha256(code.encode("utf-8")).hexdigest(),
            file_path=str(strategy_file),
            validation_status="passed",
        )
    )
    assert version is not None
    return version.id


def write_market_data(tmp_path: Path) -> Path:
    data_dir = tmp_path / "user_data" / "data"
    exchange_dir = data_dir / "okx"
    exchange_dir.mkdir(parents=True)
    exchange_dir.joinpath("BTC_USDT_USDT-15m-20240101-20240201.feather").write_bytes(b"local candles")
    return data_dir


def control_payload(strategy_version_id: int, market_data_dir: Path, **overrides) -> DryRunControlStartRequest:
    payload = {
        "strategy_version_id": strategy_version_id,
        "pair": "BTC/USDT:USDT",
        "timeframe": "15m",
        "exchange": "okx",
        "market_data_dir": str(market_data_dir),
        "timeout_seconds": 5,
    }
    payload.update(overrides)
    return DryRunControlStartRequest.model_validate(payload)


def settings(tmp_path: Path, *, allow_controlled: bool) -> Settings:
    return Settings(
        database_url=f"sqlite+pysqlite:///{tmp_path / 'control.sqlite'}",
        tmp_freqtrade_config_dir=tmp_path / "tmp" / "freqtrade_configs",
        market_data_dir=tmp_path / "user_data" / "data",
        allow_controlled_dry_run_process=allow_controlled,
    )


def service(
    db: Session,
    tmp_path: Path,
    *,
    allow_controlled: bool,
    environ: dict[str, str],
    executor,
) -> DryRunControlService:
    return DryRunControlService(
        db,
        environ=environ,
        settings=settings(tmp_path, allow_controlled=allow_controlled),
        config_builder=FreqtradeConfigBuilder(default_output_dir=tmp_path / "tmp" / "freqtrade_configs"),
        runner=FreqtradeDryRunRunner(FreqtradeCliRunner(executor=executor)),
        report_dir=tmp_path / "reports" / "runtime",
    )


def test_control_start_blocks_without_manual_approval_and_never_runs_cli(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    calls = []

    def fake_executor(args, cwd, timeout_seconds):
        calls.append(args)
        raise AssertionError("controlled dry-run must not run without manual approval")

    factory = session_factory(tmp_path)
    market_data_dir = write_market_data(tmp_path)
    with factory() as db:
        version_id = seed_strategy_version(db, tmp_path)
        report = service(
            db,
            tmp_path,
            allow_controlled=True,
            environ={
                "FREQTRADE_DRY_RUN_API_KEY": "secret-key-value",
                "FREQTRADE_DRY_RUN_API_SECRET": "secret-value",
            },
            executor=fake_executor,
        ).start(control_payload(version_id, market_data_dir))

    assert report is not None
    assert report.status == "BLOCKED"
    assert any("manual approval is required" in reason for reason in report.blocked_reasons)
    assert report.status_snapshot.status == "BLOCKED"
    assert calls == []
    manifest_text = Path(report.manifest_path).read_text(encoding="utf-8")
    assert "secret-key-value" not in manifest_text
    assert "secret-value" not in manifest_text


def test_control_start_blocks_when_backend_process_gate_is_disabled(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    calls = []

    def fake_executor(args, cwd, timeout_seconds):
        calls.append(args)
        raise AssertionError("controlled dry-run must not run while backend gate is disabled")

    factory = session_factory(tmp_path)
    market_data_dir = write_market_data(tmp_path)
    with factory() as db:
        version_id = seed_strategy_version(db, tmp_path)
        report = service(
            db,
            tmp_path,
            allow_controlled=False,
            environ={
                "FREQTRADE_DRY_RUN_API_KEY": "present",
                "FREQTRADE_DRY_RUN_API_SECRET": "present",
            },
            executor=fake_executor,
        ).start(control_payload(version_id, market_data_dir, manual_approval=True))

    assert report is not None
    assert report.status == "BLOCKED"
    assert any("disabled by backend safety setting" in reason for reason in report.blocked_reasons)
    assert calls == []


def test_control_start_runs_fake_executor_only_after_readiness_manual_and_backend_gate_pass(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    calls = []

    def fake_executor(args, cwd, timeout_seconds):
        calls.append((list(args), cwd, timeout_seconds))
        return subprocess.CompletedProcess(
            args=list(args),
            returncode=0,
            stdout="dry-run completed api_secret=secret-value",
            stderr="Bearer bearer-token-value",
        )

    factory = session_factory(tmp_path)
    market_data_dir = write_market_data(tmp_path)
    with factory() as db:
        version_id = seed_strategy_version(db, tmp_path)
        report = service(
            db,
            tmp_path,
            allow_controlled=True,
            environ={
                "FREQTRADE_DRY_RUN_API_KEY": "present",
                "FREQTRADE_DRY_RUN_API_SECRET": "present",
            },
            executor=fake_executor,
        ).start(control_payload(version_id, market_data_dir, manual_approval=True))

    assert report is not None
    assert report.status == "SUCCESS"
    assert report.status_snapshot.status == "STOPPED"
    assert report.status_snapshot.dry_run is True
    assert report.safety["starts_live_trading"] is False
    assert report.safety["places_real_orders"] is False
    assert calls and calls[0][2] == 5
    assert "--dry-run" in calls[0][0]
    stored = FreqtradeDryRunArtifactManifest.read(Path(report.manifest_path))
    stored_text = Path(report.manifest_path).read_text(encoding="utf-8")
    config = json.loads(Path(report.config_path).read_text(encoding="utf-8"))
    assert stored["status"] == "SUCCESS"
    assert stored["stdout"] == "dry-run completed api_secret=[REDACTED]"
    assert stored["stderr"] == "Bearer [REDACTED]"
    assert "secret-value" not in stored_text
    assert "bearer-token-value" not in stored_text
    assert config["dry_run"] is True
    assert config["initial_state"] == "stopped"


def test_control_start_blocks_missing_readiness_preconditions_without_cli(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    calls = []

    def fake_executor(args, cwd, timeout_seconds):
        calls.append(args)
        raise AssertionError("controlled dry-run must not run when readiness is blocked")

    factory = session_factory(tmp_path)
    with factory() as db:
        version_id = seed_strategy_version(db, tmp_path)
        report = service(
            db,
            tmp_path,
            allow_controlled=True,
            environ={
                "FREQTRADE_DRY_RUN_API_KEY": "present",
                "FREQTRADE_DRY_RUN_API_SECRET": "present",
            },
            executor=fake_executor,
        ).start(control_payload(version_id, tmp_path / "missing-data", manual_approval=True))

    assert report is not None
    assert report.status == "BLOCKED"
    assert any("market data directory does not exist" in reason for reason in report.blocked_reasons)
    assert calls == []


def test_control_stop_records_stopped_snapshot_without_process_kill(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    def fake_executor(args, cwd, timeout_seconds):
        return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")

    factory = session_factory(tmp_path)
    market_data_dir = write_market_data(tmp_path)
    with factory() as db:
        version_id = seed_strategy_version(db, tmp_path)
        control = service(
            db,
            tmp_path,
            allow_controlled=True,
            environ={
                "FREQTRADE_DRY_RUN_API_KEY": "present",
                "FREQTRADE_DRY_RUN_API_SECRET": "present",
            },
            executor=fake_executor,
        )
        start_report = control.start(control_payload(version_id, market_data_dir, manual_approval=True))
        stop_report = control.stop(DryRunControlStopRequest(reason="operator stop after bounded smoke"))

    assert start_report is not None
    assert stop_report.status == "STOPPED"
    assert stop_report.status_snapshot.status == "STOPPED"
    assert stop_report.safety["kills_external_process"] is False
    assert Path(stop_report.status_snapshot_path).exists()
