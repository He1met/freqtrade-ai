from pathlib import Path
from typing import Optional

from app.adapters.freqtrade.cli_runner import FreqtradeCliRunner, FreqtradeCommand


class FreqtradeBacktestRunner:
    """Runs Freqtrade backtests through the safe CLI runner boundary."""

    def __init__(self, cli_runner: FreqtradeCliRunner) -> None:
        self._cli_runner = cli_runner

    def run_backtest(
        self,
        config_path: Path,
        strategy_name: str,
        result_path: Optional[Path] = None,
        timeout_seconds: Optional[int] = None,
    ) -> Path:
        if result_path is None:
            raise ValueError("result_path is required for Phase 1 backtest execution")

        options = {
            "--config": config_path,
            "--strategy": strategy_name,
            "--export": "trades",
            "--export-filename": result_path,
        }

        self._cli_runner.run(
            FreqtradeCommand(
                command="backtesting",
                options=options,
                timeout_seconds=timeout_seconds,
            )
        )

        return result_path
