from pathlib import Path

from app.spikes.phase5_dry_run_preflight import (
    DryRunPreflightConfig,
    find_secret_shaped_config_values,
    run_preflight,
)


REQUIRED_ENV = (
    "FREQTRADE_DRY_RUN_EXCHANGE",
    "FREQTRADE_DRY_RUN_PAIR",
    "FREQTRADE_DRY_RUN_TIMEFRAME",
    "FREQTRADE_DRY_RUN_API_KEY",
    "FREQTRADE_DRY_RUN_API_SECRET",
)


def test_preflight_blocks_when_freqtrade_command_is_missing(tmp_path: Path) -> None:
    user_data = make_user_data(tmp_path)

    report = run_preflight(
        DryRunPreflightConfig(
            tmp_dir=tmp_path / "work",
            report_path=tmp_path / "report.md",
            user_data_dir=user_data,
            freqtrade_binary=str(tmp_path / "missing-freqtrade"),
            required_env_vars=REQUIRED_ENV,
            secret_scan_paths=(),
        ),
        environ=complete_env(),
    )

    assert report.status == "BLOCKED"
    assert "freqtrade command was not found" in report.blockers
    assert "Status: BLOCKED" in report.report_path.read_text(encoding="utf-8")


def test_preflight_blocks_when_required_env_is_missing(tmp_path: Path) -> None:
    user_data = make_user_data(tmp_path)
    freqtrade_bin = make_executable(tmp_path / "freqtrade")

    report = run_preflight(
        DryRunPreflightConfig(
            tmp_dir=tmp_path / "work",
            report_path=tmp_path / "report.md",
            user_data_dir=user_data,
            freqtrade_binary=str(freqtrade_bin),
            required_env_vars=REQUIRED_ENV,
            secret_scan_paths=(),
        ),
        environ={
            "FREQTRADE_DRY_RUN_EXCHANGE": "okx",
            "FREQTRADE_DRY_RUN_PAIR": "BTC/USDT:USDT",
            "FREQTRADE_DRY_RUN_API_KEY": "secret-key-that-must-not-render",
        },
    )

    rendered = report.report_path.read_text(encoding="utf-8")
    assert report.status == "BLOCKED"
    assert "FREQTRADE_DRY_RUN_TIMEFRAME" in report.required_env_missing
    assert "FREQTRADE_DRY_RUN_API_SECRET" in report.required_env_missing
    assert "secret-key-that-must-not-render" not in rendered


def test_preflight_fails_without_rendering_secret_values(tmp_path: Path) -> None:
    user_data = make_user_data(tmp_path)
    freqtrade_bin = make_executable(tmp_path / "freqtrade")
    unsafe_config = tmp_path / "dry-run-config.json"
    unsafe_config.write_text('{"api_secret": "real-secret-value"}', encoding="utf-8")

    report = run_preflight(
        DryRunPreflightConfig(
            tmp_dir=tmp_path / "work",
            report_path=tmp_path / "report.md",
            user_data_dir=user_data,
            freqtrade_binary=str(freqtrade_bin),
            required_env_vars=REQUIRED_ENV,
            secret_scan_paths=(unsafe_config,),
        ),
        environ=complete_env(),
    )

    rendered = report.report_path.read_text(encoding="utf-8")
    assert report.status == "FAILED"
    assert report.secret_findings[0].key == "api_secret"
    assert "real-secret-value" not in rendered
    assert "api_secret" in rendered


def test_secret_scan_allows_placeholder_values(tmp_path: Path) -> None:
    safe_config = tmp_path / ".env.example"
    safe_config.write_text(
        "FREQTRADE_DRY_RUN_API_KEY=change_me\n"
        "FREQTRADE_DRY_RUN_API_SECRET=${FREQTRADE_DRY_RUN_API_SECRET}\n",
        encoding="utf-8",
    )

    assert find_secret_shaped_config_values([safe_config]) == []


def make_user_data(tmp_path: Path) -> Path:
    user_data = tmp_path / "user_data"
    data_file = user_data / "data" / "okx" / "BTC_USDT_USDT-15m-futures.feather"
    data_file.parent.mkdir(parents=True)
    data_file.write_bytes(b"fixture")
    (user_data / "strategies").mkdir(parents=True)
    return user_data


def make_executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def complete_env() -> dict[str, str]:
    return {
        "FREQTRADE_DRY_RUN_EXCHANGE": "okx",
        "FREQTRADE_DRY_RUN_PAIR": "BTC/USDT:USDT",
        "FREQTRADE_DRY_RUN_TIMEFRAME": "15m",
        "FREQTRADE_DRY_RUN_API_KEY": "secret-key-that-must-not-render",
        "FREQTRADE_DRY_RUN_API_SECRET": "secret-value-that-must-not-render",
    }
