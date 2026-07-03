#!/usr/bin/env python3
"""Generate local runtime MVP data for the frontend.

The script renders local strategy files, runs one real local Freqtrade
backtesting smoke check, and writes the UI payload consumed by /api endpoints.
It does not download market data, start dry-run trading, or run live trading.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_PATH = REPO_ROOT / "backend"
VENV_PYTHON = BACKEND_PATH / ".venv" / "bin" / "python"
if (
    os.environ.get("FREQTRADE_AI_RUNTIME_SEED_REEXEC") != "1"
    and VENV_PYTHON.exists()
    and Path(sys.executable).absolute() != VENV_PYTHON
):
    os.environ["FREQTRADE_AI_RUNTIME_SEED_REEXEC"] = "1"
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv])

if str(BACKEND_PATH) not in sys.path:
    sys.path.insert(0, str(BACKEND_PATH))

from app.adapters.freqtrade.result_parser import FreqtradeResultParser  # noqa: E402
from app.adapters.freqtrade.strategy_file_manager import StrategyFileManager  # noqa: E402
from app.schemas.strategy_blueprint import StrategyBlueprint  # noqa: E402
from app.services.strategy_renderer import StrategyCodeRenderer  # noqa: E402
from app.spikes.real_freqtrade_backtest import (  # noqa: E402
    SpikeConfig,
    exit_code_for_status,
    run_spike,
    tail,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create local runtime UI data from generated strategies and a real Freqtrade backtest."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("tmp/runtime/mvp-data.json"),
        help="Output JSON consumed by backend /api endpoints.",
    )
    parser.add_argument(
        "--tmp-dir",
        type=Path,
        default=Path("/tmp/freqtrade-ai-ui-runtime"),
        help="Temporary workspace for generated strategies, config, and backtest results.",
    )
    parser.add_argument(
        "--report-path",
        type=Path,
        default=Path("/tmp/freqtrade-ai-ui-runtime-report.md"),
        help="Markdown report for the real Freqtrade smoke run.",
    )
    parser.add_argument(
        "--market-data-dir",
        type=Path,
        default=Path("user_data/data"),
        help="Existing local Freqtrade market data directory. The script never downloads data.",
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
        help="Timeout for the real Freqtrade backtesting process.",
    )
    return parser.parse_args()


def runtime_blueprints() -> list[StrategyBlueprint]:
    return [
        StrategyBlueprint(
            name="Runtime EMA Pullback",
            slug="runtime-ema-pullback",
            class_name="RuntimeEmaPullbackStrategy",
            description="本地生成的 EMA 回撤候选策略，等待后续真实回测。",
            timeframe="5m",
            indicators=[
                {"name": "rsi", "kind": "rsi", "period": 14},
                {"name": "ema_fast", "kind": "ema", "period": 12},
                {"name": "ema_slow", "kind": "ema", "period": 36},
            ],
            entry_rules=[{"indicator": "rsi", "operator": "<", "value": 36}],
            exit_rules=[{"indicator": "rsi", "operator": ">", "value": 68}],
            tags=["本地生成", "候选"],
        ),
        StrategyBlueprint(
            name="Runtime SMA Mean Revert",
            slug="runtime-sma-mean-revert",
            class_name="RuntimeSmaMeanRevertStrategy",
            description="本地生成的 SMA 均值回归候选策略，等待后续真实回测。",
            timeframe="5m",
            indicators=[
                {"name": "rsi", "kind": "rsi", "period": 10},
                {"name": "sma_mid", "kind": "sma", "period": 30},
            ],
            entry_rules=[{"indicator": "rsi", "operator": "<=", "value": 32}],
            exit_rules=[{"indicator": "rsi", "operator": ">=", "value": 64}],
            tags=["本地生成", "候选"],
        ),
    ]


def prepare_tmp_dir(tmp_dir: Path) -> None:
    tmp_dir = tmp_dir.expanduser().resolve()
    unsafe = {Path("/"), REPO_ROOT, BACKEND_PATH, REPO_ROOT.parent}
    if tmp_dir in unsafe:
        raise RuntimeError(f"Refusing unsafe tmp-dir: {tmp_dir}")
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True)


def render_candidate_strategies(strategy_dir: Path) -> list[dict[str, str]]:
    renderer = StrategyCodeRenderer()
    manager = StrategyFileManager(output_dir=strategy_dir)
    rendered: list[dict[str, str]] = []
    for blueprint in runtime_blueprints():
        path = manager.write_strategy_file(
            blueprint.class_name,
            renderer.render(blueprint),
            file_stem=blueprint.slug,
        )
        rendered.append(
            {
                "id": blueprint.slug,
                "name": blueprint.class_name,
                "display_name": blueprint.name,
                "description": blueprint.description or "",
                "timeframe": blueprint.timeframe,
                "file_path": str(path),
            }
        )
    return rendered


def ratio_to_percent(value: float | None) -> float | None:
    if value is None:
        return None
    return value * 100 if abs(value) <= 1 else value


def clamp(value: float, minimum: float = 0, maximum: float = 100) -> float:
    return max(minimum, min(maximum, value))


def round_optional(value: float | None, digits: int = 6) -> float | None:
    return None if value is None else round(value, digits)


def metric_payload(parsed: Any) -> dict[str, Any]:
    return {
        "profitTotal": round_optional(parsed.profit_total),
        "profitPct": round_optional(ratio_to_percent(parsed.profit_pct)),
        "maxDrawdownPct": round_optional(ratio_to_percent(parsed.max_drawdown_pct)),
        "winRate": round_optional(parsed.win_rate),
        "totalTrades": parsed.total_trades,
        "timerange": parsed.timerange,
        "sharpe": round_optional(parsed.metrics_snapshot.get("normalized_metrics", {}).get("sharpe")),
        "sortino": round_optional(parsed.metrics_snapshot.get("normalized_metrics", {}).get("sortino")),
        "calmar": round_optional(parsed.metrics_snapshot.get("normalized_metrics", {}).get("calmar")),
    }


def write_artifact_manifest(path: Path, manifest: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return path


def build_artifact_manifest(report: Any, manifest_path: Path) -> dict[str, Any]:
    return {
        "manifestVersion": 1,
        "status": report.status,
        "configPath": str(report.config_path) if report.config_path else None,
        "strategyName": report.strategy_name,
        "resultPath": str(report.result_path) if report.result_path else None,
        "manifestPath": str(manifest_path),
        "commandArgs": report.command_args,
        "returnCode": report.return_code,
        "stdout": tail(report.stdout, max_lines=20),
        "stderr": tail(report.stderr, max_lines=20),
        "datadir": (
            str(report.market_data_file.relative_path.parent)
            if report.market_data_file is not None
            else None
        ),
        "strategyPath": str(report.strategy_file.parent) if report.strategy_file else None,
        "userdir": str(report.config_path.parent.parent / "user_data") if report.config_path else None,
        "blockedReason": "; ".join(report.blockers) if report.blockers else None,
        "failedReason": "; ".join(report.failures) if report.failures else None,
    }


def score_from_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    profit_pct = metrics["profitPct"] or 0
    drawdown_pct = metrics["maxDrawdownPct"] or 0
    win_rate = metrics["winRate"] or 0
    trades = metrics["totalTrades"] or 0

    profit_score = clamp(50 + profit_pct * 5)
    risk_score = clamp(100 - drawdown_pct * 8)
    stability_score = clamp(45 + win_rate * 45 + min(trades, 50) * 0.2)
    quality_score = 88.0
    breakdown = [
        ("profit_score", profit_score, 0.35),
        ("risk_score", risk_score, 0.25),
        ("stability_score", stability_score, 0.15),
        ("quality_score", quality_score, 0.25),
    ]
    score_breakdown = [
        {
            "name": name,
            "score": round(score, 3),
            "weight": weight,
            "contribution": round(score * weight, 3),
        }
        for name, score, weight in breakdown
    ]
    total_score = sum(item["contribution"] for item in score_breakdown)

    return {
        "totalScore": round(total_score, 3),
        "rawTotalScore": round(total_score, 3),
        "profitScore": round(profit_score, 3),
        "riskScore": round(risk_score, 3),
        "stabilityScore": round(stability_score, 3),
        "qualityScore": round(quality_score, 3),
        "scoreBreakdown": score_breakdown,
    }


def build_payload(
    report: Any,
    parsed: Any,
    artifact_manifest: dict[str, Any],
    candidate_strategies: list[dict[str, str]],
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    metrics = metric_payload(parsed)
    market_data = report.market_data_file
    pair = market_data.pair if market_data is not None else "unknown"
    timeframe = market_data.timeframe if market_data is not None else "unknown"
    strategy_id = "mvp-rsi-strategy-real"
    version_id = f"{strategy_id}-v1"
    backtest_run_id = "real-backtest-run-001"
    backtest_task_id = "real-backtest-task-001"
    score = score_from_metrics(metrics)

    strategies = [
        {
            "id": strategy_id,
            "name": report.strategy_name,
            "status": "active",
            "timeframe": timeframe,
            "source": "generated_local",
            "description": "本地生成并完成真实 Freqtrade backtesting smoke 的策略。",
            "tags": ["本地生成", "真实回测", pair],
            "currentVersion": {
                "id": version_id,
                "versionNumber": 1,
                "filePath": str(report.strategy_file),
                "validationStatus": "passed",
                "validationErrors": [],
            },
        }
    ]
    for index, candidate in enumerate(candidate_strategies, start=2):
        candidate_id = candidate["id"]
        strategies.append(
            {
                "id": candidate_id,
                "name": candidate["name"],
                "status": "candidate",
                "timeframe": candidate["timeframe"],
                "source": "generated_local",
                "description": candidate["description"],
                "tags": ["本地生成", "待回测"],
                "currentVersion": {
                    "id": f"{candidate_id}-v1",
                    "versionNumber": 1,
                    "filePath": candidate["file_path"],
                    "validationStatus": "passed",
                    "validationErrors": [],
                },
            }
        )

    generation_runs = [
        {
            "id": "runtime-generation-001",
            "status": "succeeded",
            "provider": "fake",
            "model": "offline-fixture",
            "requestedCount": len(strategies),
            "generatedCount": len(strategies),
            "acceptedCount": len(strategies),
            "failedCount": 0,
            "errorMessage": None,
        }
    ]

    backtest_runs = [
        {
            "id": backtest_run_id,
            "strategyName": report.strategy_name,
            "status": "succeeded",
            "profileName": "local-real-freqtrade-smoke",
            "requestedTaskCount": 1,
            "completedTaskCount": 1,
            "profitPct": metrics["profitPct"],
            "maxDrawdownPct": metrics["maxDrawdownPct"],
            "artifactManifest": artifact_manifest,
            "metrics": metrics,
            "blockedReason": None,
            "failedReason": None,
        }
    ]

    backtest_tasks = [
        {
            "id": backtest_task_id,
            "runId": backtest_run_id,
            "strategyName": report.strategy_name,
            "pair": pair,
            "timeframe": timeframe,
            "status": "succeeded",
            "configPath": str(report.config_path),
            "resultPath": str(report.result_path),
            "profitPct": metrics["profitPct"],
            "errorMessage": None,
            "artifactManifest": artifact_manifest,
            "metrics": metrics,
            "blockedReason": None,
            "failedReason": None,
        }
    ]

    ranking = [
        {
            "rank": 1,
            "strategyId": strategy_id,
            "strategyName": report.strategy_name,
            "versionNumber": 1,
            "filePath": str(report.strategy_file),
            "scoringVersion": "runtime-smoke-v1",
            **score,
            "elimination": {"eliminated": False, "reasons": []},
            "warnings": [],
        }
    ]

    version_lineage = [
        {
            "id": item["currentVersion"]["id"],
            "strategyId": item["id"],
            "parentVersionId": None,
            "versionNumber": 1,
            "changeSummary": "本地运行时生成的初始版本。",
            "diffSnapshot": {
                "created_by": "scripts/seed_runtime_mvp.py",
                "generated_at": now,
            },
            "hasParent": False,
            "createdAt": now,
        }
        for item in strategies
    ]

    return {
        "strategies": strategies,
        "generationRuns": generation_runs,
        "backtestRuns": backtest_runs,
        "backtestTasks": backtest_tasks,
        "ranking": ranking,
        "failureReasons": [],
        "versionLineage": version_lineage,
    }


def main() -> int:
    args = parse_args()
    tmp_dir = args.tmp_dir.expanduser().resolve()
    prepare_tmp_dir(tmp_dir)

    candidate_strategies = render_candidate_strategies(tmp_dir / "runtime_strategies")
    report = run_spike(
        SpikeConfig(
            tmp_dir=tmp_dir / "real_backtest",
            report_path=args.report_path,
            market_data_dir=args.market_data_dir,
            freqtrade_binary=args.freqtrade_bin,
            timeout_seconds=args.timeout_seconds,
            report_title="Runtime MVP Real Freqtrade Smoke Report",
            profile_name="runtime_mvp_real_freqtrade_smoke",
            bot_name="freqtrade_ai_runtime_mvp",
            strategy_prompt="Generate one runtime MVP strategy for real local Freqtrade smoke testing.",
        )
    )

    if report.status != "SUCCESS" or report.result_path is None or report.strategy_name is None:
        print(f"[{report.status}] report={report.report_path}")
        for blocker in report.blockers:
            print(f"[BLOCKED] {blocker}")
        for failure in report.failures:
            print(f"[FAIL] {failure}")
        return exit_code_for_status(report.status)

    parsed = FreqtradeResultParser().parse_backtest_result(
        report.result_path,
        strategy_name=report.strategy_name,
    )
    manifest_path = tmp_dir / "backtest-artifact.json"
    artifact_manifest = build_artifact_manifest(report, manifest_path)
    write_artifact_manifest(manifest_path, artifact_manifest)

    payload = build_payload(
        report=report,
        parsed=parsed,
        artifact_manifest=artifact_manifest,
        candidate_strategies=candidate_strategies,
    )
    output = args.output.expanduser()
    if not output.is_absolute():
        output = REPO_ROOT / output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")

    print(f"[PASS] runtime data={output}")
    print(f"[PASS] report={report.report_path}")
    print(f"[PASS] metrics={payload['backtestRuns'][0]['metrics']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
