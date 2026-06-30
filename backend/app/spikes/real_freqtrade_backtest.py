from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import shutil
from typing import Optional

from app.adapters.freqtrade.backtest_runner import FreqtradeBacktestRunner
from app.adapters.freqtrade.cli_runner import FreqtradeCliRunner
from app.adapters.freqtrade.config_builder import FreqtradeConfigBuilder
from app.adapters.freqtrade.market_data_index import FreqtradeMarketDataIndex, MarketDataFile
from app.adapters.freqtrade.result_parser import FreqtradeResultParser
from app.adapters.freqtrade.strategy_file_manager import StrategyFileManager
from app.core.config import REPO_ROOT
from app.core.paths import resolve_repo_path
from app.services.strategy_generation import FakeStrategyBlueprintProvider
from app.services.strategy_renderer import StrategyCodeRenderer


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
    metrics: dict[str, object] = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


def find_freqtrade_binary(explicit_binary: Optional[str] = None) -> Optional[Path]:
    if explicit_binary:
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


def prepare_strategy(strategy_dir: Path) -> tuple[str, Path]:
    blueprint = FakeStrategyBlueprintProvider().generate(
        "Generate one Phase 2 real Freqtrade backtest spike strategy.",
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


def run_spike(config: SpikeConfig) -> SpikeReport:
    report = SpikeReport()
    tmp_dir = config.tmp_dir.expanduser().resolve()
    tmp_dir.mkdir(parents=True, exist_ok=True)
    report.report_path = resolve_repo_path(config.report_path)

    binary = find_freqtrade_binary(config.freqtrade_binary)
    report.freqtrade_binary = binary
    if binary is None:
        report.blockers.append("freqtrade command was not found")

    market_data_dir = resolve_repo_path(config.market_data_dir)
    market_data_file = select_market_data_file(market_data_dir)
    report.market_data_file = market_data_file
    if market_data_file is None:
        report.blockers.append(f"no local market data files found under {market_data_dir}")

    strategy_dir = tmp_dir / "strategies"
    config_dir = tmp_dir / "freqtrade_configs"
    result_path = tmp_dir / "backtest-result.json"
    try:
        strategy_name, strategy_file = prepare_strategy(strategy_dir)
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

    datadir = market_data_dir / market_data_file.exchange
    snapshot = {
        "profile_name": "phase2_real_freqtrade_spike",
        "pair": market_data_file.pair,
        "timeframe": market_data_file.timeframe,
        "strategy": report.strategy_name,
        "strategy_path": str(strategy_dir),
        "user_data_dir": str(tmp_dir / "user_data"),
        "datadir": str(datadir),
        "exchange": {"name": market_data_file.exchange},
        "bot_name": "freqtrade_ai_phase2_spike",
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
        userdir=tmp_dir / "user_data",
    )
    report.command_args = execution.command_args
    report.return_code = execution.command_result.return_code
    report.stdout = execution.command_result.stdout
    report.stderr = execution.command_result.stderr
    report.result_path = execution.result_path

    if execution.command_result.return_code != 0:
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

    lines = [
        "# Phase 2 Real Freqtrade Backtest Spike Report",
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
