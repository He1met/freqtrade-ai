from pathlib import Path
from dataclasses import dataclass
from typing import Optional

from app.adapters.freqtrade.cli_runner import (
    FreqtradeCliRunner,
    FreqtradeCommand,
    FreqtradeCommandResult,
)
from app.adapters.freqtrade.exceptions import FreqtradeCommandError


@dataclass(frozen=True)
class FreqtradeBacktestExecution:
    result_path: Path
    command_args: list[str]
    command_result: FreqtradeCommandResult


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
        execution = self.run_backtest_with_output(
            config_path,
            strategy_name,
            result_path=result_path,
            timeout_seconds=timeout_seconds,
        )
        if execution.command_result.return_code != 0:
            raise FreqtradeCommandError(
                f"Freqtrade backtesting exited with code {execution.command_result.return_code}"
            )
        return execution.result_path

    def run_backtest_with_output(
        self,
        config_path: Path,
        strategy_name: str,
        result_path: Optional[Path] = None,
        timeout_seconds: Optional[int] = None,
        datadir: Optional[Path] = None,
        strategy_path: Optional[Path] = None,
        userdir: Optional[Path] = None,
    ) -> FreqtradeBacktestExecution:
        if result_path is None:
            raise ValueError("result_path is required for Phase 1 backtest execution")

        options = {
            "--config": config_path,
            "--strategy": strategy_name,
            "--export": "trades",
            "--export-filename": result_path,
        }
        if datadir is not None:
            options["--datadir"] = datadir
        if strategy_path is not None:
            options["--strategy-path"] = strategy_path
        if userdir is not None:
            options["--userdir"] = userdir

        command = FreqtradeCommand(
            command="backtesting",
            options=options,
            timeout_seconds=timeout_seconds,
        )
        return FreqtradeBacktestExecution(
            result_path=result_path,
            command_args=self._cli_runner.build_args(command),
            command_result=self._cli_runner.run_unchecked(command),
        )
