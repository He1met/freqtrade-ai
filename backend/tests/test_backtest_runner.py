import json
import subprocess
from pathlib import Path

import pytest

from app.adapters.freqtrade.backtest_runner import FreqtradeBacktestRunner
from app.adapters.freqtrade.cli_runner import FreqtradeCliRunner


def test_backtest_runner_invokes_safe_cli_with_export_file() -> None:
    calls = []

    def fake_executor(args, cwd, timeout_seconds):
        calls.append((args, cwd, timeout_seconds))
        return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")

    runner = FreqtradeBacktestRunner(FreqtradeCliRunner(executor=fake_executor))
    result_path = runner.run_backtest(
        Path("tmp/freqtrade_configs/backtest.json"),
        "MvpRsiStrategy",
        result_path=Path("reports/backtests/result.json"),
        timeout_seconds=60,
    )

    assert result_path == Path("reports/backtests/result.json")
    assert calls == [
        (
            [
                "freqtrade",
                "backtesting",
                "--config",
                "tmp/freqtrade_configs/backtest.json",
                "--export",
                "trades",
                "--export-filename",
                "reports/backtests/result.json",
                "--strategy",
                "MvpRsiStrategy",
            ],
            None,
            60,
        )
    ]


def test_backtest_runner_requires_result_path() -> None:
    def fake_executor(args, cwd, timeout_seconds):
        return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")

    runner = FreqtradeBacktestRunner(FreqtradeCliRunner(executor=fake_executor))

    with pytest.raises(ValueError, match="result_path is required"):
        runner.run_backtest(Path("tmp/config.json"), "MvpRsiStrategy")


def test_backtest_runner_writes_success_artifact_manifest(tmp_path) -> None:
    calls = []

    def fake_executor(args, cwd, timeout_seconds):
        calls.append((args, cwd, timeout_seconds))
        result_path = Path(args[args.index("--export-filename") + 1])
        result_path.parent.mkdir(parents=True)
        result_path.write_text('{"strategy": {}}', encoding="utf-8")
        return subprocess.CompletedProcess(
            args=list(args),
            returncode=0,
            stdout="backtesting complete",
            stderr="",
        )

    datadir = tmp_path / "user_data" / "data" / "okx"
    datadir.mkdir(parents=True)
    (datadir / "BTC_USDT_USDT-15m-futures.feather").write_bytes(b"candles")
    result_path = tmp_path / "reports" / "backtest-result.json"
    manifest_path = tmp_path / "reports" / "manifest.json"

    runner = FreqtradeBacktestRunner(FreqtradeCliRunner(executor=fake_executor))
    manifest = runner.run_backtest_with_artifact_manifest(
        tmp_path / "config.json",
        "MvpRsiStrategy",
        result_path=result_path,
        manifest_path=manifest_path,
        timeout_seconds=60,
        datadir=datadir,
        strategy_path=tmp_path / "strategies",
        userdir=tmp_path / "user_data",
    )

    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest.status == "SUCCESS"
    assert manifest.return_code == 0
    assert manifest.blocked_reason is None
    assert manifest.failed_reason is None
    assert stored["status"] == "SUCCESS"
    assert stored["result_path"] == str(result_path)
    assert stored["stdout"] == "backtesting complete"
    assert stored["datadir"] == str(datadir)
    assert calls[0][2] == 60


def test_backtest_runner_writes_failed_manifest_with_stdout_stderr(tmp_path) -> None:
    def fake_executor(args, cwd, timeout_seconds):
        return subprocess.CompletedProcess(
            args=list(args),
            returncode=2,
            stdout="partial output",
            stderr="freqtrade failure",
        )

    datadir = tmp_path / "data" / "okx"
    datadir.mkdir(parents=True)
    (datadir / "BTC_USDT_USDT-15m-futures.feather").write_bytes(b"candles")
    manifest_path = tmp_path / "manifest.json"

    runner = FreqtradeBacktestRunner(FreqtradeCliRunner(executor=fake_executor))
    manifest = runner.run_backtest_with_artifact_manifest(
        tmp_path / "config.json",
        "MvpRsiStrategy",
        result_path=tmp_path / "result.json",
        manifest_path=manifest_path,
        datadir=datadir,
    )

    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest.status == "FAILED"
    assert manifest.return_code == 2
    assert manifest.failed_reason == "Freqtrade backtesting exited with code 2"
    assert stored["stdout"] == "partial output"
    assert stored["stderr"] == "freqtrade failure"


def test_backtest_runner_writes_blocked_manifest_without_running_cli(tmp_path) -> None:
    calls = []

    def fake_executor(args, cwd, timeout_seconds):
        calls.append(args)
        return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")

    missing_datadir = tmp_path / "missing-data" / "okx"
    missing_datadir.mkdir(parents=True)
    (missing_datadir / ".gitkeep").write_text("", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"

    runner = FreqtradeBacktestRunner(FreqtradeCliRunner(executor=fake_executor))
    manifest = runner.run_backtest_with_artifact_manifest(
        tmp_path / "config.json",
        "MvpRsiStrategy",
        result_path=tmp_path / "result.json",
        manifest_path=manifest_path,
        datadir=missing_datadir,
    )

    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest.status == "BLOCKED"
    assert manifest.return_code is None
    assert "no supported local market data files found" in manifest.blocked_reason
    assert stored["status"] == "BLOCKED"
    assert stored["blocked_reason"] == manifest.blocked_reason
    assert calls == []
