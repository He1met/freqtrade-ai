#!/usr/bin/env python3
"""Offline Phase 3 backtesting-system smoke check.

The smoke path uses generated fixture market data, BacktestProfile v2, a fake
Freqtrade backtesting runner, result parsing, matrix aggregation,
reproducibility checks, and an optional frontend build. It does not call real
Freqtrade, exchanges, K-line downloads, dry-run, live trading, Hyperopt, or
production databases.
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
from typing import Callable, Optional


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_PATH = REPO_ROOT / "backend"
VENV_PYTHON = BACKEND_PATH / ".venv" / "bin" / "python"
if (
    os.environ.get("FREQTRADE_AI_PHASE3_SMOKE_REEXEC") != "1"
    and VENV_PYTHON.exists()
    and Path(sys.executable).absolute() != VENV_PYTHON
):
    os.environ["FREQTRADE_AI_PHASE3_SMOKE_REEXEC"] = "1"
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv])

if str(BACKEND_PATH) not in sys.path:
    sys.path.insert(0, str(BACKEND_PATH))

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:////tmp/freqtrade-ai-phase3-smoke-import.sqlite")

from app.adapters.freqtrade.backtest_runner import (  # noqa: E402
    FreqtradeBacktestArtifactManifest,
)
from app.adapters.freqtrade.config_builder import FreqtradeConfigBuilder  # noqa: E402
from app.adapters.freqtrade.market_data_catalog import MarketDataCatalog  # noqa: E402
from app.adapters.freqtrade.result_parser import FreqtradeResultParser  # noqa: E402
from app.schemas.backtest import BacktestResultCreate  # noqa: E402
from app.schemas.backtest_profile import BacktestProfileV2  # noqa: E402
from app.services.backtest_matrix import BacktestMatrixExecutionService  # noqa: E402
from app.services.backtest_reproducibility import BacktestReproducibilityService  # noqa: E402


@dataclass
class Phase3SmokeContext:
    tmp_dir: Path
    market_data_dir: Path
    catalog: MarketDataCatalog
    success_profile: Optional[BacktestProfileV2] = None
    missing_profile: Optional[BacktestProfileV2] = None
    parsed_result: Optional[BacktestResultCreate] = None
    summary_path: Optional[Path] = None


class FakeBacktestRunner:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def run_backtest_with_artifact_manifest(
        self,
        config_path: Path,
        strategy_name: str,
        result_path: Path,
        manifest_path: Path,
        timeout_seconds: Optional[int] = None,
        datadir: Optional[Path] = None,
        strategy_path: Optional[Path] = None,
        userdir: Optional[Path] = None,
    ) -> FreqtradeBacktestArtifactManifest:
        self.calls.append(
            {
                "config_path": str(config_path),
                "strategy_name": strategy_name,
                "result_path": str(result_path),
                "manifest_path": str(manifest_path),
                "datadir": str(datadir) if datadir is not None else None,
                "timeout_seconds": timeout_seconds,
            }
        )
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            json.dumps(
                {
                    "strategy": {
                        strategy_name: {
                            "profit_total_abs": 42.5,
                            "profit_total_pct": 8.5,
                            "max_drawdown_pct": 2.5,
                            "wins": 18,
                            "losses": 7,
                            "draws": 0,
                            "total_trades": 25,
                            "backtest_start": "20240101",
                            "backtest_end": "20240201",
                            "sharpe": 1.25,
                            "sortino": 1.5,
                        }
                    }
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        manifest = FreqtradeBacktestArtifactManifest(
            manifest_version=1,
            status="SUCCESS",
            config_path=config_path,
            strategy_name=strategy_name,
            result_path=result_path,
            manifest_path=manifest_path,
            command_args=[
                "freqtrade",
                "backtesting",
                "--config",
                str(config_path),
                "--strategy",
                strategy_name,
            ],
            return_code=0,
            stdout="fixture backtesting completed",
            stderr="",
            datadir=datadir,
            strategy_path=strategy_path,
            userdir=userdir,
        )
        manifest.write()
        return manifest


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


def create_context(tmp_dir: Path) -> Phase3SmokeContext:
    market_data_dir = tmp_dir / "user_data" / "data"
    return Phase3SmokeContext(
        tmp_dir=tmp_dir,
        market_data_dir=market_data_dir,
        catalog=MarketDataCatalog(market_data_dir=market_data_dir),
    )


def create_fixture_market_data(context: Phase3SmokeContext) -> None:
    okx_dir = context.market_data_dir / "okx"
    okx_dir.mkdir(parents=True)
    market_file = okx_dir / "BTC_USDT_USDT-15m-futures-20240101-20240201.feather"
    market_file.write_bytes(b"phase3 fixture candles\n")
    log(f"  fixture_market_data={market_file}")


def inspect_repo_market_data_readiness() -> None:
    report = MarketDataCatalog(market_data_dir=REPO_ROOT / "user_data" / "data").inspect()
    if report.status == "available":
        log(f"  repo_market_data_status=available entries={len(report.available_entries)}")
        return
    reason = "; ".join(report.blockers) or f"market data status is {report.status}"
    log(f"[BLOCKED] optional real backtest readiness: {reason}")


def validate_catalog_and_profiles(context: Phase3SmokeContext) -> None:
    report = context.catalog.inspect()
    if report.status != "available":
        raise RuntimeError(f"Expected fixture catalog to be available, got {report.status}: {report.blockers}")
    if len(report.available_entries) != 1:
        raise RuntimeError(f"Expected one fixture data entry, got {len(report.available_entries)}")

    context.success_profile = BacktestProfileV2.model_validate(
        {
            "profile_name": "phase3_smoke_btc",
            "pair": "BTC/USDT:USDT",
            "timeframe": "15m",
            "timerange": "20240101-20240201",
            "strategy": {
                "name": "Phase3SmokeStrategy",
                "path": str(context.tmp_dir / "strategies"),
            },
            "data_source": {
                "kind": "local",
                "exchange": "okx",
                "datadir": str(context.market_data_dir),
                "data_format": "feather",
            },
            "tags": ["phase-3-smoke", "local-only"],
        }
    )
    context.missing_profile = BacktestProfileV2.model_validate(
        {
            "profile_name": "phase3_smoke_eth_missing",
            "pair": "ETH/USDT:USDT",
            "timeframe": "15m",
            "timerange": "20240101-20240201",
            "strategy": {"name": "Phase3SmokeStrategy"},
            "data_source": {
                "kind": "local",
                "exchange": "okx",
                "datadir": str(context.market_data_dir),
                "data_format": "feather",
            },
            "tags": ["phase-3-smoke", "blocked-fixture"],
        }
    )
    log(
        "  catalog_status=available "
        f"pair={report.available_entries[0].pair} timeframe={report.available_entries[0].timeframe}"
    )


def execute_fixture_matrix(context: Phase3SmokeContext) -> None:
    if context.success_profile is None or context.missing_profile is None:
        raise RuntimeError("profiles must be validated before matrix execution")

    runner = FakeBacktestRunner()
    service = BacktestMatrixExecutionService(
        runner=runner,
        config_builder=FreqtradeConfigBuilder(default_output_dir=context.tmp_dir / "configs"),
        market_data_catalog=context.catalog,
    )
    summary = service.execute_matrix(
        [context.success_profile, context.missing_profile],
        output_dir=context.tmp_dir / "matrix",
        timeout_seconds=30,
    )
    if summary.status != "BLOCKED":
        raise RuntimeError(f"Expected matrix status BLOCKED because one fixture lacks data, got {summary.status}")
    if summary.total_tasks != 2 or summary.succeeded != 1 or summary.blocked != 1 or summary.failed != 0:
        raise RuntimeError(f"Unexpected matrix summary: {summary.to_dict()}")
    if len(runner.calls) != 1:
        raise RuntimeError(f"Expected one fake runner call, got {len(runner.calls)}")
    success_task = next(task for task in summary.tasks if task.status == "SUCCESS")
    blocked_task = next(task for task in summary.tasks if task.status == "BLOCKED")
    if not success_task.manifest_path.exists() or not success_task.result_path.exists():
        raise RuntimeError("Successful fixture task did not write manifest and result JSON")
    if "no available local market data" not in (blocked_task.blocked_reason or ""):
        raise RuntimeError(f"Blocked task reason is not clear: {blocked_task.blocked_reason}")
    context.summary_path = summary.summary_path
    log(
        "  matrix_status=BLOCKED succeeded=1 blocked=1 "
        f"summary_path={summary.summary_path}"
    )


def parse_fixture_metrics(context: Phase3SmokeContext) -> None:
    if context.summary_path is None:
        raise RuntimeError("matrix must complete before parser check")
    summary_payload = json.loads(context.summary_path.read_text(encoding="utf-8"))
    success_task = next(task for task in summary_payload["tasks"] if task["status"] == "SUCCESS")
    parsed = FreqtradeResultParser().parse_backtest_result(
        Path(success_task["result_path"]),
        strategy_name="Phase3SmokeStrategy",
    )
    if parsed.profit_pct != 0.085 or parsed.max_drawdown_pct != 0.025:
        raise RuntimeError(f"Unexpected parsed metrics: {parsed.model_dump()}")
    risk_available = parsed.metrics_snapshot["parser_metadata"]["risk_metrics_available"]
    if risk_available != ["sharpe", "sortino"]:
        raise RuntimeError(f"Unexpected risk metrics availability: {risk_available}")
    context.parsed_result = parsed
    log(
        "  parsed_metrics="
        f"profit_pct={parsed.profit_pct} drawdown={parsed.max_drawdown_pct} "
        f"trades={parsed.total_trades}"
    )


def verify_reproducibility(context: Phase3SmokeContext) -> None:
    if context.success_profile is None or context.parsed_result is None:
        raise RuntimeError("profile and parsed result are required before reproducibility check")
    report = context.catalog.inspect()
    service = BacktestReproducibilityService()
    fingerprint = service.build_fingerprint(
        context.success_profile,
        strategy_version="phase3-smoke-v1",
        catalog_report=report,
    )
    comparison = service.compare_results(
        context.success_profile,
        strategy_version="phase3-smoke-v1",
        catalog_report=report,
        baseline_result=context.parsed_result,
        candidate_result=context.parsed_result,
    )
    missing = service.compare_results(
        context.success_profile,
        strategy_version="phase3-smoke-v1",
        catalog_report=report,
        baseline_result=None,
        candidate_result=context.parsed_result,
    )
    if comparison.status != "STABLE":
        raise RuntimeError(f"Expected stable reproducibility comparison, got {comparison.to_dict()}")
    if missing.status != "MISSING_BASELINE" or not missing.warnings:
        raise RuntimeError(f"Missing baseline was not reported clearly: {missing.to_dict()}")
    log(
        "  reproducibility="
        f"fingerprint={fingerprint.fingerprint_hash[:12]} stable={comparison.status} "
        f"missing_baseline={missing.status}"
    )


def run_frontend_build() -> None:
    frontend_dir = REPO_ROOT / "frontend"
    if not frontend_dir.exists():
        raise RuntimeError("frontend directory does not exist")
    subprocess.run(["npm", "run", "build"], cwd=frontend_dir, check=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the offline Phase 3 backtesting smoke check.")
    parser.add_argument("--offline", action="store_true", help="Required; confirms offline fixture mode.")
    parser.add_argument(
        "--tmp-dir",
        type=Path,
        default=Path("/tmp/freqtrade-ai-phase3-smoke"),
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
    log(f"[INFO] tmp_dir={tmp_dir}")
    log("[INFO] mode=offline-fixture; no real Freqtrade, exchange, download, dry-run, live trading, Hyperopt, or production DB")

    context = create_context(tmp_dir)
    run_step("create fixture local market data", lambda: create_fixture_market_data(context))
    run_step("inspect optional real market-data readiness", inspect_repo_market_data_readiness)
    run_step("validate MarketDataCatalog and BacktestProfile v2", lambda: validate_catalog_and_profiles(context))
    run_step("execute fixture backtest matrix and write manifests", lambda: execute_fixture_matrix(context))
    run_step("parse fixture Freqtrade result metrics", lambda: parse_fixture_metrics(context))
    run_step("verify baseline reproducibility checks", lambda: verify_reproducibility(context))
    if args.skip_frontend_build:
        log("[SKIP] frontend build")
    else:
        run_step("build frontend", run_frontend_build)

    log("[PASS] Phase 3 smoke completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
