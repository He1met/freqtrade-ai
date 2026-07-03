import json
import subprocess
from pathlib import Path
from zipfile import ZipFile

import pytest

from app.adapters.freqtrade.backtest_runner import FreqtradeBacktestRunner
from app.adapters.freqtrade.cli_runner import FreqtradeCliRunner


def test_backtest_runner_invokes_safe_cli_with_backtest_directory() -> None:
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
                "--backtest-directory",
                "reports/backtests",
                "--config",
                "tmp/freqtrade_configs/backtest.json",
                "--export",
                "trades",
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
        result_dir = Path(args[args.index("--backtest-directory") + 1])
        result_path = result_dir / "backtest-result.json"
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


def test_backtest_runner_materializes_zip_export_to_result_path(tmp_path) -> None:
    def fake_executor(args, cwd, timeout_seconds):
        result_dir = Path(args[args.index("--backtest-directory") + 1])
        result_dir.mkdir(parents=True)
        archive_path = result_dir / "backtest-result-2026-07-03_20-46-36.zip"
        with ZipFile(archive_path, "w") as archive:
            archive.writestr(
                "backtest-result-2026-07-03_20-46-36.json",
                '{"strategy": {"MvpRsiStrategy": {"total_trades": 1}}}',
            )
            archive.writestr("backtest-result-2026-07-03_20-46-36_config.json", "{}")
        (result_dir / ".last_result.json").write_text(
            json.dumps({"latest_backtest": archive_path.name}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            args=list(args),
            returncode=0,
            stdout="backtesting complete",
            stderr="",
        )

    result_path = tmp_path / "reports" / "backtest-result.json"
    runner = FreqtradeBacktestRunner(FreqtradeCliRunner(executor=fake_executor))
    execution = runner.run_backtest_with_output(
        tmp_path / "config.json",
        "MvpRsiStrategy",
        result_path=result_path,
    )

    assert execution.command_result.return_code == 0
    assert json.loads(result_path.read_text(encoding="utf-8")) == {
        "strategy": {"MvpRsiStrategy": {"total_trades": 1}}
    }


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
