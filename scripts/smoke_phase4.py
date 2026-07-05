#!/usr/bin/env python3
"""Offline Phase 4 Hyperopt smoke check.

The smoke path uses a HyperoptProfile fixture, the safe Freqtrade hyperopt
adapter with a fake executor, artifact manifests, result parsing, optimized
StrategyVersion derivation, before/after comparison, and an optional frontend
build. It does not call real Freqtrade, exchanges, K-line downloads, dry-run,
live trading, or production databases.
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
    os.environ.get("FREQTRADE_AI_PHASE4_SMOKE_REEXEC") != "1"
    and VENV_PYTHON.exists()
    and Path(sys.executable).absolute() != VENV_PYTHON
):
    os.environ["FREQTRADE_AI_PHASE4_SMOKE_REEXEC"] = "1"
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv])

if str(BACKEND_PATH) not in sys.path:
    sys.path.insert(0, str(BACKEND_PATH))

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:////tmp/freqtrade-ai-phase4-smoke-import.sqlite")

from sqlalchemy.orm import Session  # noqa: E402

from app.adapters.freqtrade.cli_runner import FreqtradeCliRunner  # noqa: E402
from app.adapters.freqtrade.hyperopt_runner import (  # noqa: E402
    FreqtradeHyperoptArtifactManifest,
    FreqtradeHyperoptRunner,
)
from app.adapters.freqtrade.result_parser import (  # noqa: E402
    FreqtradeResultParser,
    HyperoptResultParsed,
)
from app.adapters.freqtrade.strategy_file_manager import StrategyFileManager  # noqa: E402
from app.db.session import create_database_engine, create_session_factory  # noqa: E402
from app.models import Base  # noqa: E402
from app.models.strategy import StrategyVersion  # noqa: E402
from app.repositories import StrategyRepository  # noqa: E402
from app.schemas import StrategyCreate, StrategyVersionCreate  # noqa: E402
from app.schemas.backtest import BacktestResultCreate  # noqa: E402
from app.schemas.hyperopt_profile import HyperoptProfile  # noqa: E402
from app.services.hyperopt_performance_comparison import (  # noqa: E402
    HyperoptPerformanceComparisonService,
)
from app.services.hyperopt_strategy_version import HyperoptStrategyVersionService  # noqa: E402


@dataclass
class Phase4SmokeContext:
    tmp_dir: Path
    db_session: Session
    parent_version_id: Optional[int] = None
    profile: Optional[HyperoptProfile] = None
    config_path: Optional[Path] = None
    strategy_path: Optional[Path] = None
    datadir: Optional[Path] = None
    success_manifest: Optional[FreqtradeHyperoptArtifactManifest] = None
    failed_manifest: Optional[FreqtradeHyperoptArtifactManifest] = None
    blocked_manifest: Optional[FreqtradeHyperoptArtifactManifest] = None
    parsed_hyperopt_result: Optional[HyperoptResultParsed] = None
    optimized_version_id: Optional[int] = None


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


def create_context(tmp_dir: Path) -> Phase4SmokeContext:
    engine = create_database_engine(f"sqlite+pysqlite:///{tmp_dir / 'phase4-smoke.sqlite'}")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    return Phase4SmokeContext(tmp_dir=tmp_dir, db_session=session_factory())


def create_parent_strategy_and_profile(context: Phase4SmokeContext) -> None:
    strategy_dir = context.tmp_dir / "user_data" / "strategies" / "generated"
    strategy_dir.mkdir(parents=True)
    strategy_file = strategy_dir / "Phase4SmokeStrategy.py"
    generated_code = "\n".join(
        [
            "class Phase4SmokeStrategy:",
            "    minimal_roi = {}",
            "    stoploss = -0.1",
            "",
        ]
    )
    strategy_file.write_text(generated_code, encoding="utf-8")

    repository = StrategyRepository(context.db_session)
    strategy = repository.create(
        StrategyCreate(
            name="Phase 4 Smoke Strategy",
            slug="phase4-smoke-strategy",
            description="Fixture strategy used only by the Phase 4 offline smoke.",
            tags=["phase-4", "smoke", "local-only"],
        )
    )
    parent = repository.create_version(
        StrategyVersionCreate(
            strategy_id=strategy.id,
            version_number=1,
            blueprint={
                "schema_version": "2",
                "class_name": "Phase4SmokeStrategy",
                "entry_rules": [{"indicator": "rsi", "operator": "<", "value": 35}],
            },
            generated_code=generated_code,
            file_path=str(strategy_file),
            validation_status="passed",
        )
    )
    if parent is None:
        raise RuntimeError("Parent StrategyVersion was not created")

    context.parent_version_id = parent.id
    context.config_path = context.tmp_dir / "configs" / "hyperopt.json"
    context.config_path.parent.mkdir(parents=True)
    context.config_path.write_text(
        json.dumps(
            {
                "mode": "offline-fixture",
                "dry_run": False,
                "exchange": {"name": "fixture"},
                "pair_whitelist": ["BTC/USDT:USDT"],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    market_data_file = (
        context.tmp_dir
        / "user_data"
        / "data"
        / "okx"
        / "futures"
        / "BTC_USDT_USDT-15m-futures.feather"
    )
    market_data_file.parent.mkdir(parents=True)
    market_data_file.write_bytes(b"phase4 fixture candles\n")
    context.strategy_path = strategy_file.parent
    context.datadir = context.tmp_dir / "user_data" / "data" / "okx"

    context.profile = HyperoptProfile.model_validate(
        {
            "name": "phase4-smoke-hyperopt-15m",
            "description": "Local-only fixture Hyperopt profile for Phase 4 smoke.",
            "strategy": {
                "version_id": parent.id,
                "name": "Phase4SmokeStrategy",
                "file_path": str(strategy_file),
            },
            "backtest_profile_id": 1,
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
            "epochs": 20,
            "hyperopt_loss": "SharpeHyperOptLoss",
            "random_state": 42,
            "max_open_trades": 1,
            "stake_currency": "USDT",
            "locked_variables": {
                "pair": "BTC/USDT:USDT",
                "timeframe": "15m",
                "timerange": "20240101-20240201",
                "local_data_source": "okx/futures/BTC_USDT_USDT-15m-futures.feather",
                "strategy_version_id": parent.id,
                "spaces": ["buy", "sell", "roi"],
                "epochs": 20,
                "hyperopt_loss": "SharpeHyperOptLoss",
            },
            "tags": ["phase-4", "smoke", "local-only"],
        }
    )
    log(f"  parent_version_id={parent.id} strategy_file={strategy_file}")


def _require_profile_paths(
    context: Phase4SmokeContext,
) -> tuple[HyperoptProfile, Path, Path, Path]:
    if (
        context.profile is None
        or context.config_path is None
        or context.strategy_path is None
        or context.datadir is None
    ):
        raise RuntimeError("profile and fixture paths must exist before hyperopt execution")
    return context.profile, context.config_path, context.strategy_path, context.datadir


def run_success_hyperopt_manifest(context: Phase4SmokeContext) -> None:
    profile, config_path, strategy_path, datadir = _require_profile_paths(context)
    result_path = context.tmp_dir / "hyperopt" / "success-result.json"
    manifest_path = context.tmp_dir / "hyperopt" / "success-manifest.json"
    best_params_path = context.tmp_dir / "hyperopt" / "success-best-params.json"
    calls: list[list[str]] = []

    def fake_executor(
        args: Sequence[str],
        cwd: Optional[Path],
        timeout_seconds: Optional[int],
    ) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        result_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "best_result": {
                "epoch": 7,
                "loss": -1.25,
                "score": 0.91,
                "is_best": True,
                "strategy_name": "Phase4SmokeStrategy",
                "spaces": ["buy", "sell", "roi"],
                "params": {
                    "buy": {"rsi_value": 31},
                    "sell": {"sell_rsi": 74},
                    "roi": {"0": 0.05, "60": 0.02},
                },
                "metrics": {
                    "profit_total_abs": 130.0,
                    "profit_total_pct": 13.0,
                    "max_drawdown_pct": 4.0,
                    "wins": 24,
                    "losses": 10,
                    "draws": 1,
                    "total_trades": 35,
                    "sharpe": 1.45,
                    "sortino": 1.85,
                    "calmar": 1.30,
                },
            }
        }
        result_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        best_params_path.write_text(
            json.dumps(payload["best_result"]["params"], indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            args=list(args),
            returncode=0,
            stdout="fixture hyperopt complete",
            stderr="",
        )

    runner = FreqtradeHyperoptRunner(FreqtradeCliRunner(executor=fake_executor))
    manifest = runner.run_hyperopt_with_artifact_manifest(
        profile=profile,
        config_path=config_path,
        result_path=result_path,
        manifest_path=manifest_path,
        timeout_seconds=30,
        datadir=datadir,
        strategy_path=strategy_path,
        userdir=context.tmp_dir / "user_data",
        best_params_path=best_params_path,
    )
    if manifest.status != "SUCCESS" or manifest.return_code != 0:
        raise RuntimeError(f"Expected SUCCESS manifest, got {manifest.to_dict()}")
    if len(calls) != 1 or calls[0][0:2] != ["freqtrade", "hyperopt"]:
        raise RuntimeError(f"Fake hyperopt executor was not called correctly: {calls}")
    banned_tokens = {"download-data", "--dry-run", "trade"}
    if any(token in banned_tokens for token in calls[0][1:]):
        raise RuntimeError(f"Unsafe command token detected: {calls[0]}")
    context.success_manifest = manifest
    log(f"  success_manifest={manifest.manifest_path} result_path={manifest.result_path}")


def run_failed_and_blocked_manifests(context: Phase4SmokeContext) -> None:
    profile, config_path, strategy_path, datadir = _require_profile_paths(context)

    def failed_executor(
        args: Sequence[str],
        cwd: Optional[Path],
        timeout_seconds: Optional[int],
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=list(args),
            returncode=2,
            stdout="partial fixture output api_key=fixture-api-key-that-must-not-render",
            stderr="fixture hyperopt failure passphrase: fixture-passphrase-that-must-not-render",
        )

    failed_manifest = FreqtradeHyperoptRunner(
        FreqtradeCliRunner(executor=failed_executor)
    ).run_hyperopt_with_artifact_manifest(
        profile=profile,
        config_path=config_path,
        result_path=context.tmp_dir / "hyperopt" / "failed-result.json",
        manifest_path=context.tmp_dir / "hyperopt" / "failed-manifest.json",
        datadir=datadir,
        strategy_path=strategy_path,
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
        return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")

    empty_datadir = context.tmp_dir / "user_data" / "data" / "empty-okx"
    empty_datadir.mkdir(parents=True)
    blocked_manifest = FreqtradeHyperoptRunner(
        FreqtradeCliRunner(executor=blocked_executor)
    ).run_hyperopt_with_artifact_manifest(
        profile=profile,
        config_path=config_path,
        result_path=context.tmp_dir / "hyperopt" / "blocked-result.json",
        manifest_path=context.tmp_dir / "hyperopt" / "blocked-manifest.json",
        datadir=empty_datadir,
        strategy_path=strategy_path,
    )
    if blocked_manifest.status != "BLOCKED" or blocked_manifest.return_code is not None:
        raise RuntimeError(f"Expected BLOCKED manifest, got {blocked_manifest.to_dict()}")
    if blocked_calls:
        raise RuntimeError("BLOCKED path must not call the fake hyperopt executor")
    if not blocked_manifest.blocked_reason or "no supported local market data files" not in blocked_manifest.blocked_reason:
        raise RuntimeError(f"Blocked reason is not clear: {blocked_manifest.blocked_reason}")

    context.failed_manifest = failed_manifest
    context.blocked_manifest = blocked_manifest
    log("  manifest_statuses=SUCCESS,FAILED,BLOCKED")


def parse_hyperopt_result(context: Phase4SmokeContext) -> None:
    if context.success_manifest is None:
        raise RuntimeError("SUCCESS manifest is required before parsing")
    parsed = FreqtradeResultParser().parse_hyperopt_result(
        context.success_manifest.result_path,
        strategy_name=context.success_manifest.strategy_name,
    )
    if parsed.best_epoch != 7 or parsed.best_params["buy"]["rsi_value"] != 31:
        raise RuntimeError(f"Unexpected parsed Hyperopt result: {parsed}")
    if parsed.metrics_snapshot["normalized_metrics"]["profit_pct"] != 0.13:
        raise RuntimeError(f"Unexpected normalized metrics: {parsed.metrics_snapshot}")
    context.parsed_hyperopt_result = parsed
    log(
        "  parsed_hyperopt="
        f"best_epoch={parsed.best_epoch} loss={parsed.loss} spaces={','.join(parsed.spaces)}"
    )


def create_optimized_strategy_version(context: Phase4SmokeContext) -> None:
    if context.parent_version_id is None or context.parsed_hyperopt_result is None:
        raise RuntimeError("parent version and parsed Hyperopt result are required")
    manifest_path = (
        str(context.success_manifest.manifest_path)
        if context.success_manifest is not None
        else None
    )
    optimized_strategy_dir = context.tmp_dir / "optimized_strategies"
    optimized_strategy_dir.mkdir()
    result = HyperoptStrategyVersionService(
        context.db_session,
        file_manager=StrategyFileManager(
            output_dir=optimized_strategy_dir,
            approved_roots=[optimized_strategy_dir],
        ),
    ).create_optimized_version(
        parent_version_id=context.parent_version_id,
        hyperopt_run_id="phase4-smoke-run-7",
        hyperopt_result=context.parsed_hyperopt_result,
        artifact_manifest_path=manifest_path,
    )
    child = result.optimized_version
    parent = context.db_session.get(StrategyVersion, context.parent_version_id)
    if parent is None:
        raise RuntimeError("Parent StrategyVersion disappeared")
    if child.parent_version_id != parent.id or child.version_number != parent.version_number + 1:
        raise RuntimeError(f"Optimized child lineage is invalid: {child.diff_snapshot}")
    if not Path(child.file_path).exists() or "HYPEROPT_DERIVATION" not in child.generated_code:
        raise RuntimeError("Optimized StrategyVersion did not write expected metadata")
    context.optimized_version_id = child.id
    log(f"  optimized_version_id={child.id} parent_version_id={parent.id}")


def compare_before_after(context: Phase4SmokeContext) -> None:
    if context.parsed_hyperopt_result is None:
        raise RuntimeError("parsed Hyperopt result is required before comparison")
    normalized = context.parsed_hyperopt_result.metrics_snapshot["normalized_metrics"]
    before = BacktestResultCreate(
        result_path="fixture/before-backtest.json",
        metrics_snapshot={
            "normalized_metrics": {
                "sharpe": 1.20,
                "sortino": 1.60,
                "calmar": 1.10,
            }
        },
        profit_total=100.0,
        profit_pct=0.10,
        max_drawdown_pct=0.05,
        win_rate=0.55,
        total_trades=30,
        timerange="20240101-20240201",
    )
    after = BacktestResultCreate(
        result_path=context.parsed_hyperopt_result.result_path,
        metrics_snapshot={"normalized_metrics": normalized},
        profit_total=normalized["profit_total"],
        profit_pct=normalized["profit_pct"],
        max_drawdown_pct=normalized["max_drawdown_pct"],
        win_rate=normalized["win_rate"],
        total_trades=normalized["total_trades"],
        timerange="20240101-20240201",
    )
    comparison = HyperoptPerformanceComparisonService().compare(
        before_result=before,
        after_result=after,
    )
    if comparison.status != "IMPROVED":
        raise RuntimeError(f"Expected IMPROVED before/after comparison, got {comparison.to_dict()}")
    summary_path = context.tmp_dir / "phase4-smoke-summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "status": "PASS",
                "manifest_statuses": [
                    context.success_manifest.status if context.success_manifest else None,
                    context.failed_manifest.status if context.failed_manifest else None,
                    context.blocked_manifest.status if context.blocked_manifest else None,
                ],
                "comparison": comparison.to_dict(),
                "optimized_version_id": context.optimized_version_id,
                "safety": {
                    "real_freqtrade": False,
                    "exchange_connection": False,
                    "download": False,
                    "dry_run": False,
                    "live_trading": False,
                    "production_database": False,
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    log(f"  before_after_status={comparison.status} summary_path={summary_path}")


def run_frontend_build() -> None:
    frontend_dir = REPO_ROOT / "frontend"
    if not frontend_dir.exists():
        raise RuntimeError("frontend directory does not exist")
    subprocess.run(["npm", "run", "build"], cwd=frontend_dir, check=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the offline Phase 4 Hyperopt smoke check.")
    parser.add_argument("--offline", action="store_true", help="Required; confirms offline fixture mode.")
    parser.add_argument(
        "--tmp-dir",
        type=Path,
        default=Path("/tmp/freqtrade-ai-phase4-smoke"),
        help="Temporary workspace for generated fixture data, manifests, and SQLite.",
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
    log(f"[INFO] tmp_dir={tmp_dir}")
    log(
        "[INFO] mode=offline-fixture; no real Freqtrade, exchange, download, dry-run, "
        "live trading, real Hyperopt, or production DB"
    )

    context = create_context(tmp_dir)
    run_step("create fixture StrategyVersion and HyperoptProfile", lambda: create_parent_strategy_and_profile(context))
    run_step("write SUCCESS hyperopt artifact manifest with fake runner", lambda: run_success_hyperopt_manifest(context))
    run_step("write FAILED and BLOCKED hyperopt artifact manifests", lambda: run_failed_and_blocked_manifests(context))
    run_step("parse fixture Freqtrade Hyperopt result", lambda: parse_hyperopt_result(context))
    run_step("derive optimized StrategyVersion from best params", lambda: create_optimized_strategy_version(context))
    run_step("compare before and after fixture performance", lambda: compare_before_after(context))
    if args.skip_frontend_build:
        log("[SKIP] frontend build")
    else:
        run_step("build frontend", run_frontend_build)

    log("[PASS] Phase 4 smoke completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
