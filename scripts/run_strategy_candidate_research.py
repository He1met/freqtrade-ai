#!/usr/bin/env python3
"""Reproduce the offline ten-candidate Freqtrade research matrix.

This script is intentionally execution-only: it reads local OHLCV files and
writes ignored backtest artifacts plus a tracked JSON summary.  It does not
open a database, read credentials, start a runtime, or submit exchange orders.
"""

import argparse
import ast
import csv
from datetime import datetime
import hashlib
import json
import re
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.core.strategy_research_contract import (  # noqa: E402
    MAX_VALIDATION_DRAWDOWN,
    MIN_FEE_PER_SIDE as FEE_PER_SIDE,
    MIN_SLIPPAGE_PER_SIDE as SLIPPAGE_PER_SIDE,
    MIN_STRATEGY_SCORE,
    MIN_VALIDATION_TRADES,
    official_research_policy,
)
from app.core.strategy_research_matrix import (  # noqa: E402
    ALLOWED_RESEARCH_PAIRS,
    ALLOWED_RESEARCH_TIMEFRAMES,
    ResearchTarget,
)
from app.services.strategy_blueprint_equivalence import (  # noqa: E402
    prove_blueprint_code_equivalence,
)

STARTING_BALANCE = 1000.0

WINDOWS = (
    ("primary_bear", "PRIMARY", "bear", "20230701-20231001"),
    ("wf_bull", "WALK_FORWARD", "bull", "20231001-20240301"),
    ("wf_range", "WALK_FORWARD", "range", "20240301-20240629"),
    ("oos", "OOS", None, "20250101-20251001"),
    ("wf_bear", "WALK_FORWARD", "bear", "20251001-20260201"),
)

_FAILURE_CONTEXT: dict[str, Any] = {}


def _validate_run_id(run_id: str) -> str:
    formats = {
        8: "%Y%m%d",
        10: "%Y%m%d%H",
        12: "%Y%m%d%H%M",
        14: "%Y%m%d%H%M%S",
    }
    date_format = formats.get(len(run_id))
    if date_format is None or not run_id.isdigit():
        raise RuntimeError("run-id must be YYYYMMDD with optional HH, MM, and SS")
    try:
        datetime.strptime(run_id, date_format)
    except ValueError as exc:
        raise RuntimeError("run-id is not a valid calendar timestamp") from exc
    return run_id


@dataclass(frozen=True)
class Candidate:
    class_name: str
    path: Path
    sha256: str
    timeframe: str
    canonical_blueprint_evidence: Optional[dict[str, Any]] = None


def _safe_error(reason: str) -> str:
    return re.sub(
        r"(?i)\b(api[_-]?key|secret|password|passphrase|token)(\s*[:=]\s*)\S+",
        r"\1\2[REDACTED]",
        reason,
    )[:2000]


def _failure_candidate_evidence(
    repo: Path, candidates: list[Candidate], results: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for candidate in candidates:
        evidence.append(
            {
                "candidate_name": candidate.class_name,
                "source_path": str(candidate.path.relative_to(repo)),
                "code_digest": candidate.sha256,
                "evidence_snapshot": results.get(candidate.class_name, {}),
            }
        )
    return evidence


def _record_unhandled_failure(error: BaseException) -> None:
    context = _FAILURE_CONTEXT
    args = context.get("args")
    repo = context.get("repo")
    output = context.get("output")
    if args is None or repo is None or output is None:
        return
    safe_reason = _safe_error(f"{type(error).__name__}: {error}")
    candidate_evidence = _failure_candidate_evidence(
        repo, context.get("candidates") or [], context.get("results") or {}
    )
    failure_report = {
        "schema_version": "freqtrade-ai-strategy-candidate-research-failure-v1",
        "run_id": args.run_id,
        "status": "FAILED",
        "failed_stage": context.get("stage", "UNKNOWN"),
        "failure_reason": safe_reason,
        "requested_count": 10,
        "generated_count": len(candidate_evidence),
        "persisted_count": 0,
        "candidates": candidate_evidence,
        "safety": {
            "execution_scope": "LOCAL_BACKTEST_ONLY",
            "allow_real_funds": False,
            "real_orders": False,
            "runtime_or_writer_touched": False,
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(failure_report, indent=2, sort_keys=True), encoding="utf-8")
    if not args.persist_database:
        return
    if not args.repository_commit:
        raise RuntimeError("--repository-commit is required with --persist-database")
    sys.path.insert(0, str(repo / "backend"))
    from app.db.session import session_scope
    from app.services.strategy_research import StrategyResearchPersistenceService

    with session_scope() as db:
        batch = StrategyResearchPersistenceService(db).record_failed_batch(
            run_id=args.run_id,
            repository_commit=args.repository_commit,
            stage=context.get("stage", "UNKNOWN"),
            failure_reason=safe_reason,
            candidate_evidence=candidate_evidence,
            report_path=str(output),
        )
        StrategyResearchPersistenceService(db).attach_persistence_receipt(output, batch)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _discover_candidates(root: Path) -> list[Candidate]:
    candidates: list[Candidate] = []
    files = sorted(root.glob("[0-9][0-9]_*.py"))
    if len(files) != 10:
        raise RuntimeError(f"expected exactly 10 candidate files, found {len(files)}")
    for path in files:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        classes = [node.name for node in tree.body if isinstance(node, ast.ClassDef)]
        strategy_classes = [name for name in classes if name.startswith("Candidate")]
        if len(strategy_classes) != 1:
            raise RuntimeError(f"{path} must define exactly one Candidate class")
        _reject_obvious_lookahead(tree, path)
        strategy_node = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == strategy_classes[0]
        )
        timeframe = None
        for node in strategy_node.body:
            if (
                isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "timeframe" for target in node.targets)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                timeframe = node.value.value
                break
        if timeframe not in ALLOWED_RESEARCH_TIMEFRAMES:
            raise RuntimeError(
                f"{path} must declare timeframe in {ALLOWED_RESEARCH_TIMEFRAMES}"
            )
        compile(source, str(path), "exec")
        source_bytes = path.read_bytes()
        code_digest = hashlib.sha256(source_bytes).hexdigest()
        blueprint_path = path.with_suffix(".blueprint.json")
        blueprint_evidence = None
        if blueprint_path.is_file():
            blueprint_payload = json.loads(blueprint_path.read_text(encoding="utf-8"))
            if not isinstance(blueprint_payload, dict):
                raise RuntimeError(f"{blueprint_path} must contain one Blueprint v2 object")
            equivalence = prove_blueprint_code_equivalence(
                blueprint_payload=blueprint_payload,
                source_bytes=source_bytes,
                expected_source_digest=code_digest,
                expected_class_name=strategy_classes[0],
                expected_timeframe=timeframe,
            )
            blueprint_evidence = {
                "contract_version": "formal-candidate-blueprint-evidence-v1",
                "blueprint": equivalence.blueprint.model_dump(mode="json"),
                "blueprint_digest": equivalence.blueprint_digest,
                "renderer_version": equivalence.renderer_version,
                "rendered_code_digest": equivalence.rendered_code_digest,
                "source_code_digest": code_digest,
                "exact_render_match": True,
            }
        candidates.append(
            Candidate(
                strategy_classes[0],
                path,
                code_digest,
                timeframe,
                canonical_blueprint_evidence=blueprint_evidence,
            )
        )
    return candidates


def _reject_obvious_lookahead(tree: ast.AST, path: Path) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "shift" or not node.args:
            continue
        value = node.args[0]
        if isinstance(value, ast.UnaryOp) and isinstance(value.op, ast.USub):
            raise RuntimeError(f"negative shift is forbidden: {path}:{node.lineno}")
        if isinstance(value, ast.Constant) and isinstance(value.value, (int, float)) and value.value < 0:
            raise RuntimeError(f"negative shift is forbidden: {path}:{node.lineno}")


def _config(
    strategy_path: Path,
    userdir: Path,
    datadir: Path,
    *,
    pair: str,
    timeframe: str,
) -> dict[str, Any]:
    return {
        "bot_name": "freqtrade_ai_candidate_research",
        "dry_run": True,
        "initial_state": "stopped",
        "max_open_trades": 1,
        "stake_currency": "USDT",
        "stake_amount": 100.0,
        "dry_run_wallet": STARTING_BALANCE,
        "tradable_balance_ratio": 0.99,
        "timeframe": timeframe,
        "trading_mode": "futures",
        "margin_mode": "isolated",
        "exchange": {
            "name": "okx",
            "pair_whitelist": [pair],
            "pair_blacklist": [],
        },
        "pairlists": [{"method": "StaticPairList"}],
        "strategy_path": str(strategy_path),
        "user_data_dir": str(userdir),
        "datadir": str(datadir),
        "entry_pricing": {"price_side": "same", "use_order_book": True, "order_book_top": 1},
        "exit_pricing": {"price_side": "same", "use_order_book": True, "order_book_top": 1},
        "unfilledtimeout": {
            "entry": 10,
            "exit": 10,
            "exit_timeout_count": 0,
            "unit": "minutes",
        },
    }


def _run_window(
    *,
    freqtrade: Path,
    config_path: Path,
    datadir: Path,
    strategy_path: Path,
    userdir: Path,
    output_dir: Path,
    timerange: str,
    class_names: list[str],
    timeframe: str,
) -> tuple[dict[str, Any], list[str]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    command = [
        str(freqtrade),
        "backtesting",
        "--no-color",
        "--config",
        str(config_path),
        "--datadir",
        str(datadir),
        "--strategy-path",
        str(strategy_path),
        "--userdir",
        str(userdir),
        "--timerange",
        timerange,
        "--timeframe",
        timeframe,
        "--fee",
        str(FEE_PER_SIDE),
        "--cache",
        "none",
        "--export",
        "trades",
        "--backtest-directory",
        str(output_dir),
        "--strategy-list",
        *class_names,
    ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    (output_dir / "stdout.txt").write_text(completed.stdout, encoding="utf-8")
    (output_dir / "stderr.txt").write_text(completed.stderr, encoding="utf-8")
    if completed.returncode != 0:
        raise RuntimeError(
            f"Freqtrade failed for {timerange} with exit {completed.returncode}; "
            f"see {output_dir / 'stderr.txt'}"
        )
    archives = sorted(output_dir.glob("*.zip"), key=lambda item: item.stat().st_mtime)
    if not archives:
        raise RuntimeError(f"Freqtrade did not produce a result archive in {output_dir}")
    archive = archives[-1]
    with zipfile.ZipFile(archive) as bundle:
        result_names = [name for name in bundle.namelist() if name.endswith(".json") and not name.endswith("_config.json")]
        if len(result_names) != 1:
            raise RuntimeError(f"unexpected result members in {archive}: {result_names}")
        payload = json.loads(bundle.read(result_names[0]))
    return payload, command


def _run_lookahead(
    *,
    freqtrade: Path,
    config_path: Path,
    datadir: Path,
    strategy_path: Path,
    userdir: Path,
    artifact_root: Path,
    class_names: list[str],
    timeframe: str,
) -> list[str]:
    artifact_root.mkdir(parents=True, exist_ok=True)
    export_path = artifact_root / "lookahead.csv"
    command = [
        str(freqtrade),
        "lookahead-analysis",
        "--no-color",
        "--config",
        str(config_path),
        "--datadir",
        str(datadir),
        "--strategy-path",
        str(strategy_path),
        "--userdir",
        str(userdir),
        "--timerange",
        "20230701-20260201",
        "--timeframe",
        timeframe,
        "--fee",
        str(FEE_PER_SIDE),
        "--minimum-trade-amount",
        "10",
        "--targeted-trade-amount",
        "20",
        "--lookahead-analysis-exportfilename",
        str(export_path),
        "--strategy-list",
        *class_names,
    ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    (artifact_root / "lookahead.stdout.txt").write_text(completed.stdout, encoding="utf-8")
    (artifact_root / "lookahead.stderr.txt").write_text(completed.stderr, encoding="utf-8")
    if completed.returncode != 0 or not export_path.is_file():
        raise RuntimeError(
            f"Freqtrade lookahead-analysis failed with exit {completed.returncode}; "
            f"see {artifact_root / 'lookahead.stderr.txt'}"
        )
    return command


def _stress_metrics(strategy: dict[str, Any]) -> dict[str, Any]:
    trades = sorted(strategy.get("trades") or [], key=lambda item: item["close_timestamp"])
    stressed_abs: list[float] = []
    stressed_ratios: list[float] = []
    for trade in trades:
        stake = float(trade.get("stake_amount") or 0.0)
        ratio = float(trade.get("profit_ratio") or 0.0) - 2.0 * SLIPPAGE_PER_SIDE
        stressed_ratios.append(ratio)
        stressed_abs.append(float(trade.get("profit_abs") or 0.0) - stake * 2.0 * SLIPPAGE_PER_SIDE)
    cumulative = 0.0
    peak = 0.0
    max_drawdown_abs = 0.0
    for profit_abs in stressed_abs:
        cumulative += profit_abs
        peak = max(peak, cumulative)
        max_drawdown_abs = max(max_drawdown_abs, peak - cumulative)
    total_profit_abs = sum(stressed_abs)
    total_trades = len(trades)
    wins = sum(value > 0 for value in stressed_ratios)
    win_rate = wins / total_trades if total_trades else 0.0
    profit_pct = total_profit_abs / STARTING_BALANCE
    max_drawdown_pct = max_drawdown_abs / (STARTING_BALANCE + peak) if STARTING_BALANCE + peak else 0.0
    return {
        "total_trades": total_trades,
        "profit_pct": round(profit_pct, 8),
        "profit_total_abs": round(total_profit_abs, 8),
        "max_drawdown_pct": round(max_drawdown_pct, 8),
        "win_rate": round(win_rate, 8),
        "fee_per_side": FEE_PER_SIDE,
        "slippage_per_side": SLIPPAGE_PER_SIDE,
        "net_of_fee_and_slippage": True,
    }


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))


def _project_score(metrics: dict[str, Any]) -> dict[str, Any]:
    total_trades = int(metrics["total_trades"])
    profit = _clamp(float(metrics["profit_pct"]) * 500.0 + 50.0)
    risk = _clamp(100.0 - float(metrics["max_drawdown_pct"]) * 500.0)
    stability = _clamp(float(metrics["win_rate"]) * 100.0)
    trade_activity = _clamp(total_trades / 30.0 * 100.0)
    quality = trade_activity * 0.35 + 65.0
    total = profit * 0.35 + risk * 0.25 + stability * 0.15 + quality * 0.25
    eliminated = total_trades < 3 or float(metrics["max_drawdown_pct"]) >= 0.35
    return {
        "scoring_version": "phase2-quality-v1",
        "total_score": round(0.0 if eliminated else total, 6),
        "components": {
            "profit_score": round(profit, 6),
            "risk_score": round(risk, 6),
            "stability_score": round(stability, 6),
            "quality_score": round(quality, 6),
        },
        "eliminated": eliminated,
        "assumptions": "static review and validation signals passed; no failure history",
    }


def _market_return(datadir: Path, target: ResearchTarget, timerange: str) -> float:
    import pandas as pd

    path = target.market_path(datadir)
    frame = pd.read_feather(path, columns=["date", "close"])
    start, end = timerange.split("-", maxsplit=1)
    dates = frame["date"].dt.strftime("%Y%m%d")
    selected = frame[(dates >= start) & (dates < end)]
    if len(selected) < 2:
        raise RuntimeError(f"market data does not cover {timerange}")
    return float(selected["close"].iloc[-1] / selected["close"].iloc[0] - 1.0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freqtrade", type=Path, required=True)
    parser.add_argument("--datadir", type=Path, required=True)
    parser.add_argument("--run-id", default="20260804")
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--persist-database",
        action="store_true",
        help="persist the completed report as research candidates; never deploys or activates",
    )
    parser.add_argument("--repository-commit")
    args = parser.parse_args()

    repo = Path(__file__).resolve().parents[1]
    strategy_path = repo / "research" / "strategy_candidates"
    userdir = repo / "user_data"
    requested_output = args.output or Path(
        f"reports/research/strategy-candidates-{args.run_id}.json"
    )
    output = requested_output if requested_output.is_absolute() else repo / requested_output
    _FAILURE_CONTEXT.update(
        {
            "args": args,
            "repo": repo,
            "strategy_path": strategy_path,
            "output": output,
            "stage": "PREFLIGHT",
            "results": {},
            "candidates": [],
        }
    )
    _validate_run_id(args.run_id)
    artifact_root = repo / "reports" / "backtests" / f"strategy-candidates-{args.run_id}"
    config_dir = repo / "tmp" / "freqtrade_configs"
    config_path = config_dir / f"strategy-candidates-{args.run_id}.json"
    datadir = args.datadir.resolve()
    freqtrade = args.freqtrade.resolve()
    if not freqtrade.is_file() or not datadir.is_dir():
        raise RuntimeError("Freqtrade binary or OKX data directory is missing")

    candidates = _discover_candidates(strategy_path)
    _FAILURE_CONTEXT["candidates"] = candidates
    config_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, dict[str, Any]] = {
        item.class_name: {
            "file": str(item.path.relative_to(repo)),
            "sha256": item.sha256,
            "declared_timeframe": item.timeframe,
            "static_check": "PASSED",
            "loadable": True,
            "targets": {},
            **(
                {"canonical_blueprint_v2": item.canonical_blueprint_evidence}
                if item.canonical_blueprint_evidence is not None
                else {}
            ),
        }
        for item in candidates
    }
    _FAILURE_CONTEXT.update({"stage": "MATRIX", "results": results})
    commands: list[list[str]] = []
    window_evidence: list[dict[str, Any]] = []
    market_data_evidence: list[dict[str, Any]] = []
    for timeframe in ALLOWED_RESEARCH_TIMEFRAMES:
        timeframe_candidates = [item for item in candidates if item.timeframe == timeframe]
        if not timeframe_candidates:
            raise RuntimeError(f"candidate set has no {timeframe} strategies")
        for pair in ALLOWED_RESEARCH_PAIRS:
            target = ResearchTarget(pair=pair, timeframe=timeframe)
            target_slug = pair.split("/", 1)[0].lower() + "-" + timeframe
            target_root = artifact_root / target_slug
            config_path = config_dir / f"strategy-candidates-{args.run_id}-{target_slug}.json"
            config = _config(
                strategy_path, userdir, datadir, pair=pair, timeframe=timeframe
            )
            config_path.write_text(
                json.dumps(config, indent=2, sort_keys=True), encoding="utf-8"
            )
            lookahead_config_path = config_dir / (
                f"strategy-candidates-{args.run_id}-{target_slug}-lookahead.json"
            )
            lookahead_config = json.loads(json.dumps(config))
            lookahead_config["entry_pricing"]["price_side"] = "other"
            lookahead_config["exit_pricing"]["price_side"] = "other"
            lookahead_config_path.write_text(
                json.dumps(lookahead_config, indent=2, sort_keys=True), encoding="utf-8"
            )
            market_path = target.market_path(datadir)
            if not market_path.is_file():
                raise RuntimeError(f"required research data is missing: {market_path}")
            market_data_evidence.append(
                {
                    "pair": pair,
                    "timeframe": timeframe,
                    "path": str(market_path),
                    "sha256": _sha256(market_path),
                }
            )
            _FAILURE_CONTEXT["stage"] = f"LOOKAHEAD_{target_slug.upper()}"
            lookahead_command = _run_lookahead(
                freqtrade=freqtrade,
                config_path=lookahead_config_path,
                datadir=datadir,
                strategy_path=strategy_path,
                userdir=userdir,
                artifact_root=target_root,
                class_names=[item.class_name for item in timeframe_candidates],
                timeframe=timeframe,
            )
            commands.append(lookahead_command)
            lookahead_path = target_root / "lookahead.csv"
            by_strategy: dict[str, dict[str, str]] = {}
            with lookahead_path.open(encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    by_strategy[row["strategy"]] = row
            for candidate in timeframe_candidates:
                row = by_strategy.get(candidate.class_name)
                results[candidate.class_name]["targets"][target.key] = {
                    "pair": pair,
                    "timeframe": timeframe,
                    "lookahead_analysis": {
                        "status": "PASSED" if row and row.get("has_bias") == "False" else "MISSING_OR_FAILED",
                        "has_bias": None if row is None else row.get("has_bias") == "True",
                        "total_signals": None if row is None else int(row["total_signals"]),
                        "biased_entry_signals": None if row is None else int(row["biased_entry_signals"]),
                        "biased_exit_signals": None if row is None else int(row["biased_exit_signals"]),
                    },
                    "windows": {},
                }
            for name, kind, expected_regime, timerange in WINDOWS:
                _FAILURE_CONTEXT["stage"] = f"WINDOW_{target_slug.upper()}_{name.upper()}"
                market_return = _market_return(datadir, target, timerange)
                actual_regime = (
                    "bull" if market_return >= 0.05
                    else "bear" if market_return <= -0.05 else "range"
                )
                payload, command = _run_window(
                    freqtrade=freqtrade,
                    config_path=config_path,
                    datadir=datadir,
                    strategy_path=strategy_path,
                    userdir=userdir,
                    output_dir=target_root / name,
                    timerange=timerange,
                    class_names=[item.class_name for item in timeframe_candidates],
                    timeframe=timeframe,
                )
                commands.append(command)
                if expected_regime is not None and actual_regime != expected_regime:
                    raise RuntimeError(
                        f"{target.key} {name} expected {expected_regime}, computed {actual_regime}"
                    )
                window_evidence.append(
                    {
                        "target": target.key,
                        "name": name,
                        "kind": kind,
                        "timerange": timerange,
                        "market_return": round(market_return, 8),
                        "market_regime": actual_regime,
                    }
                )
                strategy_payload = payload.get("strategy") or {}
                for candidate in timeframe_candidates:
                    item = strategy_payload.get(candidate.class_name)
                    target_result = results[candidate.class_name]["targets"][target.key]
                    if not isinstance(item, dict):
                        results[candidate.class_name]["loadable"] = False
                        target_result["windows"][name] = {"status": "MISSING"}
                        continue
                    target_result["windows"][name] = {
                        "status": "SUCCESS", **_stress_metrics(item)
                    }

    qualified: list[str] = []
    validation_names = ("wf_bull", "wf_range", "oos", "wf_bear")
    for candidate in candidates:
        item = results[candidate.class_name]
        qualified_targets: list[dict[str, Any]] = []
        for target_result in item["targets"].values():
            primary = target_result["windows"]["primary_bear"]
            target_result["primary_score"] = _project_score(primary)
            target_result["validation_passed"] = all(
                target_result["windows"][name].get("status") == "SUCCESS"
                and target_result["windows"][name]["total_trades"] >= MIN_VALIDATION_TRADES
                and target_result["windows"][name]["profit_pct"] > 0
                and target_result["windows"][name]["max_drawdown_pct"] <= MAX_VALIDATION_DRAWDOWN
                for name in validation_names
            )
            target_result["score_threshold_passed"] = (
                target_result["primary_score"]["total_score"] >= MIN_STRATEGY_SCORE
            )
            target_result["deployable_candidate"] = bool(
                item["loadable"]
                and target_result["lookahead_analysis"]["status"] == "PASSED"
                and target_result["validation_passed"]
                and target_result["score_threshold_passed"]
            )
            if target_result["deployable_candidate"]:
                qualified_targets.append(target_result)
        selected = max(
            qualified_targets,
            key=lambda value: value["primary_score"]["total_score"],
            default=max(
                item["targets"].values(),
                key=lambda value: value["primary_score"]["total_score"],
            ),
        )
        item["deployment_target"] = {
            "pair": selected["pair"],
            "timeframe": selected["timeframe"],
        }
        item["lookahead_analysis"] = selected["lookahead_analysis"]
        item["windows"] = selected["windows"]
        item["primary_score"] = selected["primary_score"]
        item["validation_passed"] = selected["validation_passed"]
        item["score_threshold_passed"] = selected["score_threshold_passed"]
        item["deployable_candidate"] = bool(qualified_targets)
        item["qualified_targets"] = [
            {"pair": value["pair"], "timeframe": value["timeframe"]}
            for value in qualified_targets
        ]
        if qualified_targets:
            qualified.append(candidate.class_name)

    report = {
        "schema_version": "freqtrade-ai-strategy-candidate-research-v1",
        "safety": {
            "execution_scope": "LOCAL_BACKTEST_ONLY",
            "allow_real_funds": False,
            "real_orders": False,
            "database_used": False,
            "candidate_database_persistence_requested": args.persist_database,
            "runtime_or_writer_touched": False,
        },
        "environment": {
            "freqtrade_version": subprocess.run(
                [str(freqtrade), "--version"], check=True, capture_output=True, text=True
            ).stdout.strip(),
            "pairs": list(ALLOWED_RESEARCH_PAIRS),
            "timeframes": list(ALLOWED_RESEARCH_TIMEFRAMES),
            "market_data": market_data_evidence,
            "fee_per_side": FEE_PER_SIDE,
            "slippage_per_side": SLIPPAGE_PER_SIDE,
            "starting_balance": STARTING_BALANCE,
        },
        "selection_policy": official_research_policy(),
        "windows": window_evidence,
        "candidates": results,
        "qualified_candidates": qualified,
        "commands": commands,
        "limitations": [
            "This standalone evidence is not a persisted StrategyValidationPlan.",
            "Slippage is applied as a deterministic two-sided post-backtest stress cost.",
            "Funding history is unavailable for these historical windows; Freqtrade reports zero funding fees.",
            "ETH/SOL deployment remains blocked until the canonical owner applies and verifies the separate risk-chain allowlist migration.",
        ],
    }
    _FAILURE_CONTEXT["stage"] = "REPORT_WRITE"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(output)
    print(json.dumps({"qualified_candidates": qualified}, indent=2))
    if args.persist_database:
        _FAILURE_CONTEXT["stage"] = "DATABASE_PERSISTENCE"
        if not args.repository_commit:
            raise RuntimeError("--repository-commit is required with --persist-database")
        sys.path.insert(0, str(repo / "backend"))
        from app.db.session import session_scope
        from app.services.strategy_research import StrategyResearchPersistenceService

        with session_scope() as db:
            service = StrategyResearchPersistenceService(db)
            batch = service.persist_report(
                output.resolve(),
                run_id=args.run_id,
                repository_commit=args.repository_commit,
            )
            service.attach_persistence_receipt(output.resolve(), batch)
            print(
                json.dumps(
                    {
                        "research_batch_id": batch.id,
                        "generated_count": batch.generated_count,
                        "persisted_count": batch.persisted_count,
                        "qualified_count": batch.qualified_count,
                        "rejected_count": batch.rejected_count,
                        "allow_real_funds": False,
                        "real_orders": False,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
    _FAILURE_CONTEXT.clear()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BaseException as exc:
        try:
            _record_unhandled_failure(exc)
        except BaseException as persistence_exc:
            print(
                f"failed to persist research failure evidence: {_safe_error(str(persistence_exc))}",
                file=sys.stderr,
            )
        raise
