#!/usr/bin/env python3
"""Run the Phase 3 local market-data and real backtesting baseline check."""

import argparse
import os
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_PATH = REPO_ROOT / "backend"
VENV_PYTHON = BACKEND_PATH / ".venv" / "bin" / "python"
if (
    os.environ.get("FREQTRADE_AI_PHASE3_REEXEC") != "1"
    and VENV_PYTHON.exists()
    and Path(sys.executable).absolute() != VENV_PYTHON
):
    os.environ["FREQTRADE_AI_PHASE3_REEXEC"] = "1"
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv])

if str(BACKEND_PATH) not in sys.path:
    sys.path.insert(0, str(BACKEND_PATH))

from app.spikes.real_freqtrade_backtest import (  # noqa: E402
    SpikeConfig,
    exit_code_for_status,
    run_spike,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Check local Freqtrade market data and run one real local backtesting "
            "baseline when data exists. This command never downloads candles or trades."
        )
    )
    parser.add_argument(
        "--tmp-dir",
        type=Path,
        default=Path("/tmp/freqtrade-ai-phase3-local-baseline"),
        help="Temporary workspace for generated strategy, config, and result files.",
    )
    parser.add_argument(
        "--report-path",
        type=Path,
        default=Path("reports/spikes/phase3_local_data_baseline_latest.md"),
        help="Markdown report path. The default reports directory is ignored by git.",
    )
    parser.add_argument(
        "--market-data-dir",
        type=Path,
        default=Path("user_data/data"),
        help="Existing local Freqtrade market data directory. The command never downloads data.",
    )
    parser.add_argument(
        "--freqtrade-bin",
        default=os.environ.get("FREQTRADE_BIN"),
        help="Optional explicit freqtrade binary path. Defaults to PATH/common local venvs.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=300,
        help="Timeout for each real freqtrade diagnostic/backtesting process.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = run_spike(
        SpikeConfig(
            tmp_dir=args.tmp_dir,
            report_path=args.report_path,
            market_data_dir=args.market_data_dir,
            freqtrade_binary=args.freqtrade_bin,
            timeout_seconds=args.timeout_seconds,
            report_title="Phase 3 Local Data Availability and Backtest Baseline Report",
            profile_name="phase3_local_data_baseline",
            bot_name="freqtrade_ai_phase3_baseline",
            strategy_prompt="Generate one Phase 3 local backtesting baseline strategy.",
        )
    )
    print(f"[{report.status}] report={report.report_path}")
    for blocker in report.blockers:
        print(f"[BLOCKED] {blocker}")
    for failure in report.failures:
        print(f"[FAIL] {failure}")
    if report.result_path is not None:
        print(f"[INFO] result_json={report.result_path}")
    if report.config_path is not None:
        print(f"[INFO] config={report.config_path}")
    if report.strategy_file is not None:
        print(f"[INFO] strategy={report.strategy_file}")
    if report.metrics:
        print(f"[PASS] metrics={report.metrics}")
    return exit_code_for_status(report.status)


if __name__ == "__main__":
    raise SystemExit(main())
