#!/usr/bin/env python3
"""Offline Phase 5 dry-run / FreqUI smoke check.

The smoke path uses DryRunProfile fixtures, generated dry-run config, fake
Freqtrade trade executors, dry-run artifact manifests, read-only status
snapshots, FreqUI link metadata, and an optional frontend build. It does not
start real dry-run, connect to exchanges, download market data, place orders,
or persist secrets.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_PATH = REPO_ROOT / "backend"
VENV_PYTHON = BACKEND_PATH / ".venv" / "bin" / "python"
if (
    os.environ.get("FREQTRADE_AI_PHASE5_SMOKE_REEXEC") != "1"
    and VENV_PYTHON.exists()
    and Path(sys.executable).absolute() != VENV_PYTHON
):
    os.environ["FREQTRADE_AI_PHASE5_SMOKE_REEXEC"] = "1"
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv])

if str(BACKEND_PATH) not in sys.path:
    sys.path.insert(0, str(BACKEND_PATH))

from app.adapters.freqtrade.cli_runner import FreqtradeCliRunner  # noqa: E402
from app.adapters.freqtrade.config_builder import (  # noqa: E402
    DryRunEnvPreflight,
    FreqtradeConfigBuilder,
)
from app.adapters.freqtrade.dry_run_runner import (  # noqa: E402
    FreqtradeDryRunArtifactManifest,
    FreqtradeDryRunRunner,
)
from app.schemas.dry_run_profile import DryRunProfile  # noqa: E402
from app.services.dry_run_status import DryRunStatusSnapshotService  # noqa: E402
from app.services.freq_ui_link import FreqUILinkMetadataService  # noqa: E402


@dataclass
class Phase5SmokeContext:
    tmp_dir: Path
    profile: Optional[DryRunProfile] = None
    config_path: Optional[Path] = None
    env_preflight: Optional[DryRunEnvPreflight] = None
    success_manifest: Optional[FreqtradeDryRunArtifactManifest] = None
    failed_manifest: Optional[FreqtradeDryRunArtifactManifest] = None
    blocked_manifest: Optional[FreqtradeDryRunArtifactManifest] = None


def log(message: str) -> None:
    print(message, flush=True)


def run_step(name: str, action: Callable[[], None]) -> None:
    log(f"[RUN] {name}")
    try:
        action()
    except Exception as exc:
        log(f"[FAIL] {name}: {exc}")
        traceback.print_exc()
        raise
    log(f"[PASS] {name}")


def prepare_tmp_dir(tmp_dir: Path) -> None:
    if tmp_dir in {Path("/"), REPO_ROOT, BACKEND_PATH, REPO_ROOT.parent}:
        raise RuntimeError(f"Refusing unsafe smoke tmp-dir: {tmp_dir}")
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True)


def create_context(tmp_dir: Path) -> Phase5SmokeContext:
    return Phase5SmokeContext(tmp_dir=tmp_dir)


def create_profile_config_and_fixtures(context: Phase5SmokeContext) -> None:
    user_data = context.tmp_dir / "user_data"
    strategy_dir = user_data / "strategies" / "generated"
    strategy_dir.mkdir(parents=True)
    strategy_file = strategy_dir / "Phase5SmokeStrategy.py"
    strategy_file.write_text(
        "\n".join(
            [
                "class Phase5SmokeStrategy:",
                "    minimal_roi = {}",
                "    stoploss = -0.1",
                "",
            ]
        ),
        encoding="utf-8",
    )

    profile = DryRunProfile.model_validate(
        {
            "name": "phase5-local-dry-run",
            "description": "Local-only fixture dry-run profile for Phase 5 smoke.",
            "strategy": {
                "version_id": 162,
                "name": "Phase5SmokeStrategy",
                "file_path": "user_data/strategies/generated/Phase5SmokeStrategy.py",
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
                "strategy_version_id": 162,
                "strategy": "Phase5SmokeStrategy",
                "pair": "BTC/USDT:USDT",
                "timeframe": "15m",
                "exchange": "okx",
                "stake_currency": "USDT",
                "stake_amount": 100,
                "max_open_trades": 1,
                "dry_run": True,
                "freq_ui_enabled": True,
            },
            "tags": ["phase-5", "smoke", "local-only"],
        }
    )

    builder = FreqtradeConfigBuilder(default_output_dir=context.tmp_dir / "configs")
    build_result = builder.build_dry_run_config(
        profile=profile,
        environ={
            "FREQTRADE_DRY_RUN_API_KEY": "fixture-key-that-must-not-render",
            "FREQTRADE_DRY_RUN_API_SECRET": "fixture-secret-that-must-not-render",
        },
    )
    config_text = build_result.config_path.read_text(encoding="utf-8")
    if "fixture-key-that-must-not-render" in config_text or "fixture-secret-that-must-not-render" in config_text:
        raise RuntimeError("Generated dry-run config leaked fixture secret values")
    if build_result.config.get("dry_run") is not True:
        raise RuntimeError("Generated dry-run config did not keep dry_run=true")

    context.profile = profile
    context.config_path = build_result.config_path
    context.env_preflight = build_result.env_preflight
    log(f"  profile={profile.name} config_path={build_result.config_path}")


def _require_profile_and_config(context: Phase5SmokeContext) -> tuple[DryRunProfile, Path, DryRunEnvPreflight]:
    if context.profile is None or context.config_path is None or context.env_preflight is None:
        raise RuntimeError("profile, config, and ENV preflight must exist")
    return context.profile, context.config_path, context.env_preflight


def _success_status_snapshot(context: Phase5SmokeContext) -> dict[str, object]:
    return {
        "status": "success",
        "profile_name": "phase5-local-dry-run",
        "strategy_version_id": 162,
        "strategy_name": "Phase5SmokeStrategy",
        "exchange": "okx",
        "pair": "BTC/USDT:USDT",
        "timeframe": "15m",
        "dry_run": True,
        "balance_summary": {
            "currency": "USDT",
            "total": 1000,
            "free": 900,
            "used": 100,
            "realized_profit": 1.5,
            "unrealized_profit": 0.25,
        },
        "open_trades": [
            {"pair": "BTC/USDT:USDT", "stake_amount": 100, "profit_abs": 0.25},
            {"pair": "ETH/USDT:USDT", "is_open": False, "profit_abs": 99},
        ],
        "recent_events": [
            {
                "timestamp": "2026-07-04T16:30:00Z",
                "event_type": "fixture_status",
                "severity": "info",
                "message": "offline fixture dry-run snapshot parsed",
                "source": "phase5-smoke",
            }
        ],
        "last_updated": "2026-07-04T16:31:00Z",
        "artifact_manifest_path": str(context.tmp_dir / "dry-run" / "success-manifest.json"),
    }


def run_success_manifest(context: Phase5SmokeContext) -> None:
    profile, config_path, env_preflight = _require_profile_and_config(context)
    calls: list[list[str]] = []

    def fake_executor(
        args: Sequence[str],
        cwd: Optional[Path],
        timeout_seconds: Optional[int],
    ) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return subprocess.CompletedProcess(
            args=list(args),
            returncode=0,
            stdout="fixture dry-run completed without connecting to an exchange",
            stderr="",
        )

    manifest = FreqtradeDryRunRunner(
        FreqtradeCliRunner(executor=fake_executor)
    ).run_dry_run_with_artifact_manifest(
        profile=profile,
        config_path=config_path,
        manifest_path=context.tmp_dir / "dry-run" / "success-manifest.json",
        timeout_seconds=30,
        env_preflight=env_preflight,
        status_snapshots=[_success_status_snapshot(context)],
    )
    if manifest.status != "SUCCESS" or manifest.return_code != 0:
        raise RuntimeError(f"Expected SUCCESS manifest, got {manifest.to_dict()}")
    if len(calls) != 1 or calls[0][0:2] != ["freqtrade", "trade"]:
        raise RuntimeError(f"Fake dry-run executor was not called correctly: {calls}")
    if "--dry-run" not in calls[0]:
        raise RuntimeError(f"Dry-run command did not include --dry-run: {calls[0]}")
    banned_tokens = {"download-data", "live", "hyperopt"}
    if any(token in banned_tokens for token in calls[0][1:]):
        raise RuntimeError(f"Unsafe command token detected: {calls[0]}")

    context.success_manifest = manifest
    log(f"  success_manifest={manifest.manifest_path}")


def run_failed_and_blocked_manifests(context: Phase5SmokeContext) -> None:
    profile, config_path, env_preflight = _require_profile_and_config(context)

    def failed_executor(
        args: Sequence[str],
        cwd: Optional[Path],
        timeout_seconds: Optional[int],
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=list(args),
            returncode=2,
            stdout="partial fixture output api_key=fixture-api-key-that-must-not-render",
            stderr="fixture dry-run failure passphrase: fixture-passphrase-that-must-not-render",
        )

    failed_manifest = FreqtradeDryRunRunner(
        FreqtradeCliRunner(executor=failed_executor)
    ).run_dry_run_with_artifact_manifest(
        profile=profile,
        config_path=config_path,
        manifest_path=context.tmp_dir / "dry-run" / "failed-manifest.json",
        env_preflight=env_preflight,
        status_snapshots=[
            {
                **_success_status_snapshot(context),
                "status": "failed",
                "failed_reason": "fixture dry-run process failed",
            }
        ],
    )
    failed_text = failed_manifest.manifest_path.read_text(encoding="utf-8")
    if failed_manifest.status != "FAILED" or failed_manifest.return_code != 2:
        raise RuntimeError(f"Expected FAILED manifest, got {failed_manifest.to_dict()}")
    if (
        "fixture-api-key-that-must-not-render" in failed_text
        or "fixture-passphrase-that-must-not-render" in failed_text
    ):
        raise RuntimeError("FAILED manifest leaked secret-shaped fixture values")

    blocked_calls: list[list[str]] = []

    def blocked_executor(
        args: Sequence[str],
        cwd: Optional[Path],
        timeout_seconds: Optional[int],
    ) -> subprocess.CompletedProcess[str]:
        blocked_calls.append(list(args))
        raise AssertionError("dry-run executor must not run when ENV preflight is blocked")

    optional_readiness = DryRunEnvPreflight(
        status="BLOCKED",
        required_env_present=(),
        required_env_missing=("FREQTRADE_DRY_RUN_API_KEY", "FREQTRADE_DRY_RUN_API_SECRET"),
        optional_env_present=(),
        optional_env_missing=("FREQTRADE_DRY_RUN_API_PASSPHRASE",),
        blocked_reason=(
            "required ENV variables are missing or empty: "
            "FREQTRADE_DRY_RUN_API_KEY, FREQTRADE_DRY_RUN_API_SECRET"
        ),
    )
    blocked_manifest = FreqtradeDryRunRunner(
        FreqtradeCliRunner(executor=blocked_executor)
    ).run_dry_run_with_artifact_manifest(
        profile=profile,
        config_path=config_path,
        manifest_path=context.tmp_dir / "dry-run" / "blocked-manifest.json",
        env_preflight=optional_readiness,
        status_snapshots=[
            {
                "status": "blocked",
                "dry_run": True,
                "blocked_reason": optional_readiness.blocked_reason,
                "last_updated": "2026-07-04T16:32:00Z",
            }
        ],
    )
    if blocked_manifest.status != "BLOCKED" or blocked_manifest.return_code is not None:
        raise RuntimeError(f"Expected BLOCKED manifest, got {blocked_manifest.to_dict()}")
    if blocked_calls:
        raise RuntimeError("BLOCKED path must not call the fake dry-run executor")

    context.failed_manifest = failed_manifest
    context.blocked_manifest = blocked_manifest
    log("  manifest_statuses=SUCCESS,FAILED,BLOCKED")
    log("  optional_readiness=BLOCKED")


def parse_status_snapshots(context: Phase5SmokeContext) -> None:
    if context.success_manifest is None or context.failed_manifest is None or context.blocked_manifest is None:
        raise RuntimeError("all manifests must exist before status parsing")

    service = DryRunStatusSnapshotService()
    success = service.snapshot_from_artifact_manifest(context.success_manifest.manifest_path)
    failed = service.snapshot_from_artifact_manifest(context.failed_manifest.manifest_path)
    blocked = service.snapshot_from_artifact_manifest(context.blocked_manifest.manifest_path)
    if success.status != "SUCCESS" or success.dry_run is not True:
        raise RuntimeError(f"Unexpected SUCCESS status snapshot: {success.model_dump(mode='json')}")
    if failed.status != "FAILED" or not failed.failed_reason:
        raise RuntimeError(f"Unexpected FAILED status snapshot: {failed.model_dump(mode='json')}")
    if blocked.status != "BLOCKED" or not blocked.blocked_reason:
        raise RuntimeError(f"Unexpected BLOCKED status snapshot: {blocked.model_dump(mode='json')}")

    fixture_path = context.tmp_dir / "status" / "fixture-status.json"
    fixture_path.parent.mkdir(parents=True)
    fixture_path.write_text(
        json.dumps(_success_status_snapshot(context), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    fixture = service.snapshot_from_fixture_json(fixture_path)
    if fixture.status != "SUCCESS" or fixture.open_trades_summary.total_open_trades != 1:
        raise RuntimeError(f"Unexpected fixture status snapshot: {fixture.model_dump(mode='json')}")
    log("  snapshot_statuses=SUCCESS,FAILED,BLOCKED")


def validate_freq_ui_link(context: Phase5SmokeContext) -> None:
    if context.profile is None:
        raise RuntimeError("profile is required before FreqUI link validation")
    service = FreqUILinkMetadataService()
    enabled = service.metadata_from_config(context.profile.freq_ui.model_dump(mode="json"))
    disabled = service.metadata_from_config(None)
    if not enabled.enabled or str(enabled.base_url) != "http://127.0.0.1:8080/":
        raise RuntimeError(f"Unexpected enabled FreqUI link metadata: {enabled.model_dump(mode='json')}")
    if enabled.access_mode != "read-only-link":
        raise RuntimeError("FreqUI link access mode must stay read-only-link")
    if disabled.enabled or not disabled.blocked_reason:
        raise RuntimeError(f"Unexpected disabled FreqUI link metadata: {disabled.model_dump(mode='json')}")
    log(f"  frequi_access_mode={enabled.access_mode}")


def write_summary(context: Phase5SmokeContext) -> None:
    if context.success_manifest is None or context.failed_manifest is None or context.blocked_manifest is None:
        raise RuntimeError("all manifests must exist before summary")
    summary_path = context.tmp_dir / "phase5-smoke-summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "status": "PASS",
                "manifest_statuses": [
                    context.success_manifest.status,
                    context.failed_manifest.status,
                    context.blocked_manifest.status,
                ],
                "optional_readiness": "BLOCKED",
                "safety": {
                    "real_freqtrade": False,
                    "exchange_connection": False,
                    "download": False,
                    "real_dry_run": False,
                    "live_trading": False,
                    "real_orders": False,
                    "secrets_persisted": False,
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    log(f"  summary_path={summary_path}")


def run_frontend_build() -> None:
    frontend_dir = REPO_ROOT / "frontend"
    if not frontend_dir.exists():
        raise RuntimeError("frontend directory does not exist")
    subprocess.run(["npm", "run", "build"], cwd=frontend_dir, check=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the offline Phase 5 dry-run / FreqUI smoke check.")
    parser.add_argument("--offline", action="store_true", help="Required; confirms offline fixture mode.")
    parser.add_argument(
        "--tmp-dir",
        type=Path,
        default=Path("/tmp/freqtrade-ai-phase5-smoke"),
        help="Temporary workspace for generated fixture data and manifests.",
    )
    parser.add_argument(
        "--skip-frontend-build",
        action="store_true",
        help="Skip npm frontend build; use only for backend-only diagnostics or unit tests.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.offline:
        log("[FAIL] --offline is required; this smoke command only supports fixture mode.")
        return 2

    tmp_dir = args.tmp_dir.expanduser().resolve()
    prepare_tmp_dir(tmp_dir)
    os.chdir(tmp_dir)
    log(f"[INFO] tmp_dir={tmp_dir}")
    log(
        "[INFO] mode=offline-fixture; no real Freqtrade, exchange connection, "
        "market-data download, real dry-run, live trading, real orders, or secrets persistence"
    )

    context = create_context(tmp_dir)
    run_step("create fixture DryRunProfile and ENV-only config", lambda: create_profile_config_and_fixtures(context))
    run_step("write SUCCESS dry-run artifact manifest with fake runner", lambda: run_success_manifest(context))
    run_step("write FAILED and BLOCKED dry-run artifact manifests", lambda: run_failed_and_blocked_manifests(context))
    run_step("parse read-only dry-run status snapshots", lambda: parse_status_snapshots(context))
    run_step("validate FreqUI read-only link metadata", lambda: validate_freq_ui_link(context))
    run_step("write Phase 5 smoke summary", lambda: write_summary(context))
    if args.skip_frontend_build:
        log("[SKIP] frontend build")
    else:
        run_step("build frontend", run_frontend_build)

    log("[PASS] Phase 5 smoke completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
