import json
import subprocess
from pathlib import Path

from app.adapters.freqtrade.cli_runner import FreqtradeCliRunner
from app.adapters.freqtrade.hyperopt_runner import FreqtradeHyperoptRunner
from app.schemas.hyperopt_profile import HyperoptProfile


def valid_profile_payload(tmp_path: Path) -> dict:
    strategy_file = tmp_path / "user_data" / "strategies" / "generated" / "MvpRsiStrategy.py"
    return {
        "name": "phase4-local-hyperopt-15m",
        "description": "Local-only Hyperopt profile for one candidate strategy.",
        "strategy": {
            "version_id": 123,
            "name": "MvpRsiStrategy",
            "file_path": str(strategy_file),
        },
        "backtest_profile_id": 45,
        "pair": "BTC/USDT:USDT",
        "timeframe": "15m",
        "timerange": "20240101-20240201",
        "local_data_source": {
            "kind": "local",
            "root": "user_data/data",
            "exchange": "okx",
            "relative_path": "okx/futures/BTC_USDT_USDT-15m-futures.feather",
            "data_format": "feather",
        },
        "spaces": ["buy", "sell", "roi"],
        "epochs": 100,
        "hyperopt_loss": "SharpeHyperOptLoss",
        "random_state": 42,
        "max_open_trades": 1,
        "stake_currency": "USDT",
        "locked_variables": {
            "pair": "BTC/USDT:USDT",
            "timeframe": "15m",
            "timerange": "20240101-20240201",
            "local_data_source": "okx/futures/BTC_USDT_USDT-15m-futures.feather",
            "strategy_version_id": 123,
            "spaces": ["buy", "sell", "roi"],
            "epochs": 100,
            "hyperopt_loss": "SharpeHyperOptLoss",
        },
        "tags": ["phase-4", "local-only"],
    }


def write_preflight_files(tmp_path: Path, profile: HyperoptProfile) -> tuple[Path, Path, Path]:
    config_path = tmp_path / "tmp" / "freqtrade_configs" / "hyperopt.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("{}", encoding="utf-8")

    strategy_file_path = Path(profile.strategy.file_path)
    strategy_file_path.parent.mkdir(parents=True)
    strategy_file_path.write_text("class MvpRsiStrategy: pass\n", encoding="utf-8")

    datadir = tmp_path / "user_data" / "data" / "okx"
    market_data_file = datadir / "futures" / "BTC_USDT_USDT-15m-futures.feather"
    market_data_file.parent.mkdir(parents=True)
    market_data_file.write_bytes(b"candles")
    return config_path, strategy_file_path.parent, datadir


def test_hyperopt_runner_writes_success_manifest(tmp_path) -> None:
    profile = HyperoptProfile.model_validate(valid_profile_payload(tmp_path))
    config_path, strategy_path, datadir = write_preflight_files(tmp_path, profile)
    calls = []

    def fake_executor(args, cwd, timeout_seconds):
        calls.append((args, cwd, timeout_seconds))
        result_path = Path(args[args.index("--export-filename") + 1])
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text('{"best_result": {"params": {"buy_rsi": 31}}}', encoding="utf-8")
        return subprocess.CompletedProcess(
            args=list(args),
            returncode=0,
            stdout="hyperopt complete",
            stderr="",
        )

    result_path = tmp_path / "reports" / "hyperopt-result.json"
    best_params_path = tmp_path / "reports" / "best-params.json"
    best_params_path.parent.mkdir(parents=True)
    best_params_path.write_text('{"buy": {"rsi": 31}}', encoding="utf-8")
    manifest_path = tmp_path / "reports" / "hyperopt-manifest.json"

    runner = FreqtradeHyperoptRunner(FreqtradeCliRunner(executor=fake_executor))
    manifest = runner.run_hyperopt_with_artifact_manifest(
        profile=profile,
        config_path=config_path,
        result_path=result_path,
        manifest_path=manifest_path,
        timeout_seconds=120,
        datadir=datadir,
        strategy_path=strategy_path,
        userdir=tmp_path / "user_data",
        best_params_path=best_params_path,
    )

    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest.status == "SUCCESS"
    assert manifest.return_code == 0
    assert manifest.blocked_reason is None
    assert manifest.failed_reason is None
    assert stored["status"] == "SUCCESS"
    assert stored["profile_name"] == "phase4-local-hyperopt-15m"
    assert stored["strategy_version_id"] == 123
    assert stored["spaces"] == ["buy", "sell", "roi"]
    assert stored["stdout"] == "hyperopt complete"
    assert stored["best_params_path"] == str(best_params_path)
    assert calls == [
        (
            [
                "freqtrade",
                "hyperopt",
                "--config",
                str(config_path),
                "--datadir",
                str(datadir),
                "--epochs",
                "100",
                "--export",
                "trades",
                "--export-filename",
                str(result_path),
                "--hyperopt-loss",
                "SharpeHyperOptLoss",
                "--print-json",
                "--random-state",
                "42",
                "--spaces",
                "buy",
                "sell",
                "roi",
                "--strategy",
                "MvpRsiStrategy",
                "--strategy-path",
                str(strategy_path),
                "--timeframe",
                "15m",
                "--timerange",
                "20240101-20240201",
                "--userdir",
                str(tmp_path / "user_data"),
            ],
            None,
            120,
        )
    ]


def test_hyperopt_runner_writes_failed_manifest_for_non_zero_return(tmp_path) -> None:
    profile = HyperoptProfile.model_validate(valid_profile_payload(tmp_path))
    config_path, strategy_path, datadir = write_preflight_files(tmp_path, profile)

    def fake_executor(args, cwd, timeout_seconds):
        return subprocess.CompletedProcess(
            args=list(args),
            returncode=2,
            stdout="partial output api_key=real-value",
            stderr="hyperopt failure passphrase: real-passphrase",
        )

    manifest_path = tmp_path / "hyperopt-manifest.json"
    runner = FreqtradeHyperoptRunner(FreqtradeCliRunner(executor=fake_executor))
    manifest = runner.run_hyperopt_with_artifact_manifest(
        profile=profile,
        config_path=config_path,
        result_path=tmp_path / "hyperopt-result.json",
        manifest_path=manifest_path,
        datadir=datadir,
        strategy_path=strategy_path,
    )

    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest.status == "FAILED"
    assert manifest.return_code == 2
    assert manifest.failed_reason == "Freqtrade hyperopt exited with code 2"
    assert stored["stdout"] == "partial output api_key=[REDACTED]"
    assert stored["stderr"] == "hyperopt failure passphrase: [REDACTED]"
    assert "real-value" not in manifest_path.read_text(encoding="utf-8")
    assert "real-passphrase" not in manifest_path.read_text(encoding="utf-8")


def test_hyperopt_runner_writes_failed_manifest_when_result_file_missing(tmp_path) -> None:
    profile = HyperoptProfile.model_validate(valid_profile_payload(tmp_path))
    config_path, strategy_path, datadir = write_preflight_files(tmp_path, profile)

    def fake_executor(args, cwd, timeout_seconds):
        return subprocess.CompletedProcess(
            args=list(args),
            returncode=0,
            stdout="hyperopt complete without export",
            stderr="",
        )

    manifest_path = tmp_path / "hyperopt-manifest.json"
    result_path = tmp_path / "missing-result.json"
    runner = FreqtradeHyperoptRunner(FreqtradeCliRunner(executor=fake_executor))
    manifest = runner.run_hyperopt_with_artifact_manifest(
        profile=profile,
        config_path=config_path,
        result_path=result_path,
        manifest_path=manifest_path,
        datadir=datadir,
        strategy_path=strategy_path,
    )

    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest.status == "FAILED"
    assert manifest.return_code == 0
    assert manifest.failed_reason == f"Freqtrade hyperopt result JSON was not generated: {result_path}"
    assert stored["status"] == "FAILED"


def test_hyperopt_runner_writes_blocked_manifest_without_running_cli(tmp_path) -> None:
    profile = HyperoptProfile.model_validate(valid_profile_payload(tmp_path))
    config_path = tmp_path / "tmp" / "freqtrade_configs" / "hyperopt.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("{}", encoding="utf-8")
    strategy_path = Path(profile.strategy.file_path).parent
    strategy_path.mkdir(parents=True)
    Path(profile.strategy.file_path).write_text("class MvpRsiStrategy: pass\n", encoding="utf-8")
    datadir = tmp_path / "user_data" / "data" / "okx"
    datadir.mkdir(parents=True)
    (datadir / ".gitkeep").write_text("", encoding="utf-8")
    calls = []

    def fake_executor(args, cwd, timeout_seconds):
        calls.append(args)
        return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")

    manifest_path = tmp_path / "hyperopt-manifest.json"
    runner = FreqtradeHyperoptRunner(FreqtradeCliRunner(executor=fake_executor))
    manifest = runner.run_hyperopt_with_artifact_manifest(
        profile=profile,
        config_path=config_path,
        result_path=tmp_path / "hyperopt-result.json",
        manifest_path=manifest_path,
        datadir=datadir,
        strategy_path=strategy_path,
    )

    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest.status == "BLOCKED"
    assert manifest.return_code is None
    assert "no supported local market data files found" in manifest.blocked_reason
    assert stored["blocked_reason"] == manifest.blocked_reason
    assert calls == []


def test_hyperopt_runner_marks_missing_binary_as_blocked(tmp_path) -> None:
    profile = HyperoptProfile.model_validate(valid_profile_payload(tmp_path))
    config_path, strategy_path, datadir = write_preflight_files(tmp_path, profile)

    def missing_binary_executor(args, cwd, timeout_seconds):
        raise FileNotFoundError("freqtrade")

    manifest_path = tmp_path / "hyperopt-manifest.json"
    runner = FreqtradeHyperoptRunner(FreqtradeCliRunner(executor=missing_binary_executor))
    manifest = runner.run_hyperopt_with_artifact_manifest(
        profile=profile,
        config_path=config_path,
        result_path=tmp_path / "hyperopt-result.json",
        manifest_path=manifest_path,
        datadir=datadir,
        strategy_path=strategy_path,
    )

    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest.status == "BLOCKED"
    assert manifest.blocked_reason == "freqtrade binary is not available"
    assert stored["stderr"] == "freqtrade"
