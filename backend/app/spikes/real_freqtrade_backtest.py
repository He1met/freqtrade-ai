from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import shutil
from typing import Optional

from app.adapters.freqtrade.backtest_runner import FreqtradeBacktestRunner
from app.adapters.freqtrade.cli_runner import (
    FreqtradeCliRunner,
    FreqtradeCommand,
    FreqtradeCommandResult,
)
from app.adapters.freqtrade.config_builder import FreqtradeConfigBuilder
from app.adapters.freqtrade.market_data_index import FreqtradeMarketDataIndex, MarketDataFile
from app.adapters.freqtrade.result_parser import FreqtradeResultParser
from app.adapters.freqtrade.strategy_file_manager import StrategyFileManager
from app.core.config import REPO_ROOT
from app.core.paths import resolve_repo_path
from app.services.strategy_generation import FakeStrategyBlueprintProvider
from app.services.strategy_renderer import StrategyCodeRenderer


# This module is a Phase 2 spike, not the production backtesting workflow. It
# proves whether the current adapter boundaries can drive a real local
# Freqtrade CLI run when the user's machine already has market data available.
REQUIRED_METRIC_LABELS = {
    "profit_total": "total profit",
    "max_drawdown_pct": "max drawdown",
    "total_trades": "trade count",
    "win_rate": "win rate",
}


@dataclass(frozen=True)
class SpikeConfig:
    tmp_dir: Path = Path("/tmp/freqtrade-ai-real-backtest")
    report_path: Path = Path("reports/spikes/phase2_real_freqtrade_backtest_latest.md")
    market_data_dir: Path = Path("user_data/data")
    freqtrade_binary: Optional[str] = None
    timeout_seconds: int = 300
    report_title: str = "Phase 2 Real Freqtrade Backtest Spike Report"
    profile_name: str = "phase2_real_freqtrade_spike"
    bot_name: str = "freqtrade_ai_phase2_spike"
    strategy_prompt: str = "Generate one Phase 2 real Freqtrade backtest spike strategy."
    check_timerange: bool = True


@dataclass
class SpikeReport:
    status: str = "PENDING"
    freqtrade_binary: Optional[Path] = None
    market_data_file: Optional[MarketDataFile] = None
    strategy_file: Optional[Path] = None
    strategy_name: Optional[str] = None
    config_path: Optional[Path] = None
    result_path: Optional[Path] = None
    report_path: Optional[Path] = None
    command_args: list[str] = field(default_factory=list)
    return_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    list_data_args: list[str] = field(default_factory=list)
    list_data_return_code: Optional[int] = None
    list_data_stdout: str = ""
    list_data_stderr: str = ""
    metrics: dict[str, object] = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    report_title: str = "Phase 2 Real Freqtrade Backtest Spike Report"


def find_freqtrade_binary(explicit_binary: Optional[str] = None) -> Optional[Path]:
    if explicit_binary:
        # An explicit path is treated as a contract. If it is wrong, report a
        # blocker instead of silently falling back to another freqtrade binary.
        candidate = Path(explicit_binary).expanduser()
        if candidate.exists() and candidate.is_file():
            return candidate.resolve()
        return None

    candidates = []
    discovered = shutil.which("freqtrade")
    if discovered:
        candidates.append(Path(discovered))

    candidates.extend(
        [
            REPO_ROOT / "backend" / ".venv" / "bin" / "freqtrade",
            Path.home() / "freqtrade_venv" / "bin" / "freqtrade",
        ]
    )

    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate.resolve()
    return None


def select_market_data_file(market_data_dir: Path) -> Optional[MarketDataFile]:
    files = FreqtradeMarketDataIndex(market_data_dir=market_data_dir).list_files()
    if not files:
        return None
    return sorted(
        files,
        key=lambda item: (
            item.exchange,
            item.pair,
            item.timeframe,
            item.relative_path.as_posix(),
        ),
    )[0]


def prepare_strategy(strategy_dir: Path, prompt: str) -> tuple[str, Path]:
    blueprint = FakeStrategyBlueprintProvider().generate(
        prompt,
        requested_count=1,
    )[0]
    code = StrategyCodeRenderer().render(blueprint)
    path = StrategyFileManager(output_dir=strategy_dir).write_strategy_file(
        blueprint.class_name,
        code,
        file_stem=blueprint.slug,
    )
    return blueprint.class_name, path


def parse_required_metrics(result_path: Path, strategy_name: str) -> dict[str, object]:
    parsed = FreqtradeResultParser().parse_backtest_result(result_path, strategy_name=strategy_name)
    metrics = {
        "profit_total": parsed.profit_total,
        "max_drawdown_pct": parsed.max_drawdown_pct,
        "total_trades": parsed.total_trades,
        "win_rate": parsed.win_rate,
    }
    missing = [
        label
        for key, label in REQUIRED_METRIC_LABELS.items()
        if metrics.get(key) is None
    ]
    if missing:
        raise RuntimeError(f"Result JSON is missing required metrics: {', '.join(missing)}")
    return metrics


def prepare_freqtrade_userdir(userdir: Path) -> None:
    userdir.mkdir(parents=True, exist_ok=True)
    for relative_path in (
        "backtest_results",
        "data",
        "freqaimodels",
        "hyperopts",
        "logs",
        "notebooks",
        "plot",
        "strategies",
    ):
        (userdir / relative_path).mkdir(parents=True, exist_ok=True)


def infer_trading_mode(market_data_file: MarketDataFile) -> Optional[str]:
    if "futures" in market_data_file.relative_path.parts or ":" in market_data_file.pair:
        return "futures"
    return None


def prepare_market_data_workspace(
    source_root: Path,
    market_data_file: MarketDataFile,
    tmp_dir: Path,
) -> Path:
    workspace_root = tmp_dir / "market_data"
    target_path = workspace_root / market_data_file.relative_path
    target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(market_data_file.path, target_path)

    source_exchange_dir = source_root / market_data_file.exchange
    target_exchange_dir = workspace_root / market_data_file.exchange
    for cache_file in source_exchange_dir.glob("futures/leverage_tiers_*.json"):
        cache_target = target_exchange_dir / cache_file.relative_to(source_exchange_dir)
        cache_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cache_file, cache_target)

    return workspace_root


def run_timerange_check(
    binary: Path,
    market_data_dir: Path,
    market_data_file: MarketDataFile,
    timeout_seconds: int,
) -> tuple[list[str], FreqtradeCommandResult]:
    runner = FreqtradeCliRunner(binary=str(binary))
    command = FreqtradeCommand(
        command="list-data",
        options={
            "--datadir": market_data_dir / market_data_file.exchange,
            "--exchange": market_data_file.exchange,
            "--pairs": [market_data_file.pair],
            "--show-timerange": True,
        },
        timeout_seconds=timeout_seconds,
    )
    trading_mode = infer_trading_mode(market_data_file)
    if trading_mode is not None:
        command_options = dict(command.options or {})
        command_options["--trading-mode"] = trading_mode
        command = FreqtradeCommand(
            command=command.command,
            options=command_options,
            timeout_seconds=command.timeout_seconds,
        )
    return runner.build_args(command), runner.run_unchecked(command)


def run_spike(config: SpikeConfig) -> SpikeReport:
    report = SpikeReport()
    report.report_title = config.report_title
    tmp_dir = config.tmp_dir.expanduser().resolve()
    tmp_dir.mkdir(parents=True, exist_ok=True)
    report.report_path = resolve_repo_path(config.report_path)

    binary = find_freqtrade_binary(config.freqtrade_binary)
    report.freqtrade_binary = binary
    if binary is None:
        report.blockers.append("freqtrade command was not found")

    # Local market data is a hard prerequisite. The spike must not download
    # candles or connect to an exchange just to make the command pass.
    market_data_dir = resolve_repo_path(config.market_data_dir)
    market_data_file = select_market_data_file(market_data_dir)
    report.market_data_file = market_data_file
    if market_data_file is None:
        report.blockers.append(f"no local market data files found under {market_data_dir}")

    strategy_dir = tmp_dir / "strategies"
    config_dir = tmp_dir / "freqtrade_configs"
    result_path = tmp_dir / "backtest-result.json"
    try:
        strategy_name, strategy_file = prepare_strategy(strategy_dir, config.strategy_prompt)
        report.strategy_name = strategy_name
        report.strategy_file = strategy_file
    except Exception as exc:
        report.failures.append(f"strategy file generation failed: {exc}")

    if report.blockers or report.failures:
        report.status = "BLOCKED" if report.blockers else "FAILED"
        write_report(report)
        return report

    assert binary is not None
    assert market_data_file is not None
    assert report.strategy_name is not None

    userdir = tmp_dir / "user_data"
    prepare_freqtrade_userdir(userdir)
    runtime_market_data_dir = prepare_market_data_workspace(
        market_data_dir,
        market_data_file,
        tmp_dir,
    )

    if config.check_timerange:
        try:
            report.list_data_args, list_data_result = run_timerange_check(
                binary,
                runtime_market_data_dir,
                market_data_file,
                config.timeout_seconds,
            )
            report.list_data_return_code = list_data_result.return_code
            report.list_data_stdout = list_data_result.stdout
            report.list_data_stderr = list_data_result.stderr
        except Exception as exc:
            report.status = "FAILED"
            report.failures.append(f"freqtrade list-data timerange check failed: {exc}")
            write_report(report)
            return report

        if report.list_data_return_code != 0:
            report.status = "FAILED"
            report.failures.append(
                f"freqtrade list-data timerange check exited with code {report.list_data_return_code}"
            )
            write_report(report)
            return report

    datadir = runtime_market_data_dir / market_data_file.exchange
    trading_mode = infer_trading_mode(market_data_file)
    exchange_snapshot: dict[str, str] = {"name": market_data_file.exchange}
    if trading_mode is not None:
        exchange_snapshot["trading_mode"] = trading_mode
        if trading_mode == "futures":
            exchange_snapshot["margin_mode"] = "isolated"

    snapshot = {
        # The config snapshot intentionally contains only backtesting inputs
        # that can be derived from local files and generated strategy metadata.
        "profile_name": config.profile_name,
        "pair": market_data_file.pair,
        "timeframe": market_data_file.timeframe,
        "strategy": report.strategy_name,
        "strategy_path": str(strategy_dir),
        "user_data_dir": str(userdir),
        "datadir": str(datadir),
        "exchange": exchange_snapshot,
        "bot_name": config.bot_name,
    }
    try:
        report.config_path = FreqtradeConfigBuilder(
            default_output_dir=config_dir,
        ).build_backtest_config(snapshot)
    except Exception as exc:
        report.status = "FAILED"
        report.failures.append(f"temporary Freqtrade config generation failed: {exc}")
        write_report(report)
        return report

    execution = FreqtradeBacktestRunner(
        FreqtradeCliRunner(binary=str(binary)),
    ).run_backtest_with_output(
        report.config_path,
        report.strategy_name,
        result_path=result_path,
        timeout_seconds=config.timeout_seconds,
        datadir=datadir,
        strategy_path=strategy_dir,
        userdir=userdir,
    )
    report.command_args = execution.command_args
    report.return_code = execution.command_result.return_code
    report.stdout = execution.command_result.stdout
    report.stderr = execution.command_result.stderr
    report.result_path = execution.result_path

    if execution.command_result.return_code != 0:
        # Non-zero exits are FAILED, not BLOCKED, because all local
        # prerequisites were present and Freqtrade actually ran.
        report.status = "FAILED"
        report.failures.append(
            f"freqtrade backtesting exited with code {execution.command_result.return_code}"
        )
        write_report(report)
        return report

    if not execution.result_path.exists():
        report.status = "FAILED"
        report.failures.append(f"result JSON was not generated: {execution.result_path}")
        write_report(report)
        return report

    try:
        report.metrics = parse_required_metrics(execution.result_path, report.strategy_name)
    except Exception as exc:
        report.status = "FAILED"
        report.failures.append(f"result JSON parsing failed: {exc}")
        write_report(report)
        return report

    report.status = "SUCCESS"
    write_report(report)
    return report


def write_report(report: SpikeReport) -> Path:
    if report.report_path is None:
        raise ValueError("report_path is required")
    report.report_path.parent.mkdir(parents=True, exist_ok=True)
    report.report_path.write_text(render_report(report), encoding="utf-8")
    return report.report_path


def render_report(report: SpikeReport) -> str:
    def value(text: object) -> str:
        return str(text) if text is not None else "not available"

    blocker_lines = [f"- {item}" for item in report.blockers] or ["- none"]
    failure_lines = [f"- {item}" for item in report.failures] or ["- none"]

    market_data_lines = ["- none"]
    if report.market_data_file is not None:
        market_data_lines = [
            f"- Exchange: {report.market_data_file.exchange}",
            f"- Pair: {report.market_data_file.pair}",
            f"- Timeframe: {report.market_data_file.timeframe}",
            f"- Format: {report.market_data_file.data_format}",
            f"- File size bytes: {report.market_data_file.file_size_bytes}",
        ]

    lines = [
        f"# {report.report_title}",
        "",
        f"- Status: {report.status}",
        f"- Freqtrade command: {value(report.freqtrade_binary)}",
        "- Local market data: "
        f"{value(report.market_data_file.relative_path if report.market_data_file else None)}",
        f"- Strategy file: {value(report.strategy_file)}",
        f"- Temporary config: {value(report.config_path)}",
        f"- Result JSON: {value(report.result_path)}",
        f"- Return code: {value(report.return_code)}",
        "",
        "## Local Data Availability",
        "",
        *market_data_lines,
        "",
        "## Timerange Check",
        "",
        f"- Return code: {value(report.list_data_return_code)}",
        "",
        "```text",
        " ".join(report.list_data_args) if report.list_data_args else "not executed",
        "```",
        "",
        "### Timerange Stdout Tail",
        "",
        "```text",
        tail(report.list_data_stdout),
        "```",
        "",
        "### Timerange Stderr Tail",
        "",
        "```text",
        tail(report.list_data_stderr),
        "```",
        "",
        "## Safety Boundary",
        "",
        "- No dry-run or live trading is executed.",
        "- No exchange data download is executed.",
        "- No real exchange credentials are read or written.",
        "- No Freqtrade source code is modified.",
        "",
        "## Command",
        "",
        "```text",
        " ".join(report.command_args) if report.command_args else "not executed",
        "```",
        "",
        "## Metrics",
        "",
        "```json",
        json.dumps(report.metrics, indent=2, sort_keys=True),
        "```",
        "",
        "## Blockers",
        "",
        *blocker_lines,
        "",
        "## Failures",
        "",
        *failure_lines,
        "",
        "## Stdout Tail",
        "",
        "```text",
        tail(report.stdout),
        "```",
        "",
        "## Stderr Tail",
        "",
        "```text",
        tail(report.stderr),
        "```",
        "",
    ]
    return "\n".join(lines)


def tail(text: str, max_lines: int = 80) -> str:
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:]) if lines else ""


def exit_code_for_status(status: str) -> int:
    if status == "SUCCESS":
        return 0
    if status == "BLOCKED":
        return 2
    return 1
