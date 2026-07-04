import subprocess
from pathlib import Path

from app.adapters.freqtrade.cli_runner import FreqtradeCliRunner
from app.adapters.freqtrade.config_builder import DryRunEnvPreflight
from app.adapters.freqtrade.dry_run_runner import (
    FreqtradeDryRunArtifactManifest,
    FreqtradeDryRunRunner,
)
from app.schemas.dry_run_profile import DryRunProfile


def valid_profile_payload() -> dict:
    return {
        "name": "phase5-local-dry-run",
        "description": "Local dry-run profile for one candidate strategy.",
        "strategy": {
            "version_id": 123,
            "name": "MvpRsiStrategy",
            "file_path": "user_data/strategies/generated/MvpRsiStrategy.py",
        },
        "pair": "BTC/USDT:USDT",
        "timeframe": "15m",
        "stake": {
            "currency": "USDT",
            "amount": 100,
            "tradable_balance_ratio": 0.99,
            "max_open_trades": 1,
        },
        "exchange": {"name": "okx", "trading_mode": "futures"},
        "freq_ui": {
            "enabled": True,
            "base_url": "http://127.0.0.1:8080",
            "environment_label": "local-dry-run",
        },
        "command_options": {
            "user_data_dir": "user_data",
            "strategy_path": "user_data/strategies/generated",
            "log_level": "INFO",
        },
        "locked_variables": {
            "profile_name": "phase5-local-dry-run",
            "strategy_version_id": 123,
            "strategy": "MvpRsiStrategy",
            "pair": "BTC/USDT:USDT",
            "timeframe": "15m",
            "exchange": "okx",
            "stake_currency": "USDT",
            "stake_amount": 100,
            "max_open_trades": 1,
            "dry_run": True,
            "freq_ui_enabled": True,
        },
        "tags": ["phase-5", "local-only"],
    }


def test_dry_run_runner_builds_controlled_trade_plan_from_profile() -> None:
    profile = DryRunProfile.model_validate(valid_profile_payload())
    runner = FreqtradeDryRunRunner(FreqtradeCliRunner(binary="freqtrade"))

    plan = runner.build_dry_run_plan(
        profile=profile,
        config_path=Path("tmp/freqtrade_configs/dry-run.json"),
        timeout_seconds=45,
    )

    assert plan.profile_name == "phase5-local-dry-run"
    assert plan.strategy_version_id == 123
    assert plan.pair == "BTC/USDT:USDT"
    assert plan.timeframe == "15m"
    assert plan.userdir == Path("user_data")
    assert plan.strategy_path == Path("user_data/strategies/generated")
    assert plan.command_args == [
        "freqtrade",
        "trade",
        "--config",
        "tmp/freqtrade_configs/dry-run.json",
        "--dry-run",
        "--loglevel",
        "INFO",
        "--strategy",
        "MvpRsiStrategy",
        "--strategy-path",
        "user_data/strategies/generated",
        "--userdir",
        "user_data",
    ]


def test_dry_run_runner_uses_fake_executor_and_captures_success_output() -> None:
    calls = []

    def fake_executor(args, cwd, timeout_seconds):
        calls.append((list(args), cwd, timeout_seconds))
        return subprocess.CompletedProcess(
            args=list(args),
            returncode=0,
            stdout="dry-run started",
            stderr="",
        )

    profile = DryRunProfile.model_validate(valid_profile_payload())
    runner = FreqtradeDryRunRunner(FreqtradeCliRunner(executor=fake_executor))

    execution = runner.run_dry_run_with_output(
        profile=profile,
        config_path=Path("tmp/freqtrade_configs/dry-run.json"),
        timeout_seconds=45,
    )

    assert execution.command_result.return_code == 0
    assert execution.command_result.stdout == "dry-run started"
    assert execution.command_result.stderr == ""
    assert execution.plan.command_args == calls[0][0]
    assert calls[0][1] is None
    assert calls[0][2] == 45


def test_dry_run_runner_captures_failed_fake_executor_output_without_raising() -> None:
    def fake_executor(args, cwd, timeout_seconds):
        return subprocess.CompletedProcess(
            args=list(args),
            returncode=2,
            stdout="dry-run stdout",
            stderr="dry-run stderr",
        )

    profile = DryRunProfile.model_validate(valid_profile_payload())
    runner = FreqtradeDryRunRunner(FreqtradeCliRunner(executor=fake_executor))

    execution = runner.run_dry_run_with_output(
        profile=profile,
        config_path=Path("tmp/freqtrade_configs/dry-run.json"),
    )

    assert execution.command_result.return_code == 2
    assert execution.command_result.stdout == "dry-run stdout"
    assert execution.command_result.stderr == "dry-run stderr"


def test_dry_run_runner_writes_success_artifact_manifest_with_redaction(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    calls = []

    def fake_executor(args, cwd, timeout_seconds):
        calls.append((list(args), cwd, timeout_seconds))
        return subprocess.CompletedProcess(
            args=list(args),
            returncode=0,
            stdout="dry-run started api_secret=real-value",
            stderr="Bearer token-value",
        )

    config_path, userdir, strategy_path = write_manifest_prerequisites(tmp_path)
    manifest_path = tmp_path / "reports" / "dry-run-manifest.json"
    profile = DryRunProfile.model_validate(valid_profile_payload())
    runner = FreqtradeDryRunRunner(FreqtradeCliRunner(executor=fake_executor))

    manifest = runner.run_dry_run_with_artifact_manifest(
        profile=profile,
        config_path=config_path,
        manifest_path=manifest_path,
        timeout_seconds=45,
        env_preflight=DryRunEnvPreflight(
            status="READY",
            required_env_present=("FREQTRADE_DRY_RUN_API_KEY",),
            required_env_missing=(),
            optional_env_present=(),
            optional_env_missing=("FREQTRADE_DRY_RUN_API_PASSPHRASE",),
        ),
        status_snapshots=[
            {
                "status": "STARTED",
                "userdir": str(userdir),
                "strategy_path": str(strategy_path),
                "api_key": "real-value",
            }
        ],
    )

    stored = FreqtradeDryRunArtifactManifest.read(manifest_path)
    stored_text = manifest_path.read_text(encoding="utf-8")
    assert manifest.status == "SUCCESS"
    assert manifest.return_code == 0
    assert stored["status"] == "SUCCESS"
    assert stored["profile_snapshot"]["profile_hash"]
    assert stored["env_preflight"]["status"] == "READY"
    assert stored["status_snapshots"][0]["api_key"] == "[REDACTED]"
    assert "api_secret=[REDACTED]" in stored["stdout"]
    assert stored["stderr"] == "Bearer [REDACTED]"
    assert "real-value" not in stored_text
    assert "token-value" not in stored_text
    assert calls[0][0] == stored["command_args"]
    assert calls[0][2] == 45


def test_dry_run_runner_writes_failed_artifact_manifest(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    def fake_executor(args, cwd, timeout_seconds):
        return subprocess.CompletedProcess(
            args=list(args),
            returncode=2,
            stdout="dry-run stdout",
            stderr="passphrase=real-passphrase",
        )

    config_path, _, _ = write_manifest_prerequisites(tmp_path)
    manifest_path = tmp_path / "dry-run-failed-manifest.json"
    profile = DryRunProfile.model_validate(valid_profile_payload())
    runner = FreqtradeDryRunRunner(FreqtradeCliRunner(executor=fake_executor))

    manifest = runner.run_dry_run_with_artifact_manifest(
        profile=profile,
        config_path=config_path,
        manifest_path=manifest_path,
    )

    stored = FreqtradeDryRunArtifactManifest.read(manifest_path)
    assert manifest.status == "FAILED"
    assert manifest.return_code == 2
    assert manifest.failed_reason == "Freqtrade dry-run exited with code 2"
    assert stored["failed_reason"] == manifest.failed_reason
    assert "real-passphrase" not in manifest_path.read_text(encoding="utf-8")


def test_dry_run_runner_writes_blocked_manifest_without_running_cli(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    calls = []

    def fake_executor(args, cwd, timeout_seconds):
        calls.append(args)
        raise AssertionError("dry-run executor must not run when ENV preflight is blocked")

    config_path, _, _ = write_manifest_prerequisites(tmp_path)
    manifest_path = tmp_path / "dry-run-blocked-manifest.json"
    profile = DryRunProfile.model_validate(valid_profile_payload())
    runner = FreqtradeDryRunRunner(FreqtradeCliRunner(executor=fake_executor))

    manifest = runner.run_dry_run_with_artifact_manifest(
        profile=profile,
        config_path=config_path,
        manifest_path=manifest_path,
        env_preflight=DryRunEnvPreflight(
            status="BLOCKED",
            required_env_present=(),
            required_env_missing=("FREQTRADE_DRY_RUN_API_KEY",),
            optional_env_present=(),
            optional_env_missing=(),
            blocked_reason="required ENV variables are missing or empty: FREQTRADE_DRY_RUN_API_KEY",
        ),
    )

    stored = FreqtradeDryRunArtifactManifest.read(manifest_path)
    assert manifest.status == "BLOCKED"
    assert manifest.return_code is None
    assert "FREQTRADE_DRY_RUN_API_KEY" in manifest.blocked_reason
    assert stored["blocked_reason"] == manifest.blocked_reason
    assert calls == []


def test_dry_run_runner_writes_skipped_manifest_without_preflight_or_cli(tmp_path) -> None:
    calls = []

    def fake_executor(args, cwd, timeout_seconds):
        calls.append(args)
        raise AssertionError("dry-run executor must not run for skipped manifest")

    manifest_path = tmp_path / "dry-run-skipped-manifest.json"
    profile = DryRunProfile.model_validate(valid_profile_payload())
    runner = FreqtradeDryRunRunner(FreqtradeCliRunner(executor=fake_executor))

    manifest = runner.run_dry_run_with_artifact_manifest(
        profile=profile,
        config_path=tmp_path / "missing-config.json",
        manifest_path=manifest_path,
        skipped_reason="dry-run start was intentionally skipped by offline acceptance",
    )

    stored = FreqtradeDryRunArtifactManifest.read(manifest_path)
    assert manifest.status == "SKIPPED"
    assert manifest.return_code is None
    assert stored["skipped_reason"] == "dry-run start was intentionally skipped by offline acceptance"
    assert calls == []


def write_manifest_prerequisites(tmp_path) -> tuple[Path, Path, Path]:
    config_path = tmp_path / "tmp" / "freqtrade_configs" / "dry-run.json"
    userdir = tmp_path / "user_data"
    strategy_path = userdir / "strategies" / "generated"
    config_path.parent.mkdir(parents=True)
    strategy_path.mkdir(parents=True)
    config_path.write_text('{"dry_run": true}\n', encoding="utf-8")
    return config_path, userdir, strategy_path
