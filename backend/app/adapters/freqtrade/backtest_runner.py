from pathlib import Path

from app.adapters.freqtrade.cli_runner import FreqtradeCliRunner


class FreqtradeBacktestRunner:
    """Runs future Freqtrade backtests through the CLI runner boundary."""

    def __init__(self, cli_runner: FreqtradeCliRunner) -> None:
        self._cli_runner = cli_runner

    def run_backtest(self, config_path: Path, strategy_name: str) -> Path:
        raise NotImplementedError("Phase 0 does not execute Freqtrade backtests.")
