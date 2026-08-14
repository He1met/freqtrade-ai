#!/usr/bin/env python3
"""Networkless one-shot worker for the canonical V1.3 research adapter."""

from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import zipfile


REQUEST_CONTRACT = "canonical-v13-freqtrade-backtest-request-v1"
OUTPUT_CONTRACT = "canonical-v13-freqtrade-backtest-output-v1"
PREFLIGHT_CONTRACT = "canonical-v13-research-worker-preflight-v1"
EXPECTED_CAPABILITY = {
    "trading": "TRADING_DISABLED",
    "exchange_access": "NONE",
    "order_submission": "DISABLED",
    "allow_real_funds": False,
}
HEX = frozenset("0123456789abcdef")


class Blocked(RuntimeError):
    pass


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _load_object(path: Path) -> dict[str, object]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Blocked("invalid JSON input") from exc
    if not isinstance(value, dict) or _canonical_bytes(value) != raw:
        raise Blocked("input is not one canonical JSON object")
    return value


def _digest(value: object) -> str:
    return sha256(_canonical_bytes(value)).hexdigest()


def _hex_digest(value: object) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in HEX for c in value):
        raise Blocked("digest is not lowercase SHA-256")
    return value


def _file_digest(path: Path) -> tuple[str, int]:
    digest = sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
    except OSError as exc:
        raise Blocked("input file is unavailable") from exc
    return digest.hexdigest(), size


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise Blocked("timestamp is not text")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise Blocked("timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise Blocked("timestamp has no timezone")
    return parsed.astimezone(timezone.utc)


def _single_configuration(bundle: dict[str, object], kind: str) -> dict[str, object]:
    rows = bundle.get("configurations")
    if not isinstance(rows, list):
        raise Blocked("configuration set is absent")
    selected = [row for row in rows if isinstance(row, dict) and row.get("configuration_kind") == kind]
    if len(selected) != 1 or not isinstance(selected[0].get("payload"), dict):
        raise Blocked(f"{kind} configuration is ambiguous")
    return selected[0]["payload"]  # type: ignore[return-value]


def _strategy_class(path: Path) -> str:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        raise Blocked("strategy source is invalid") from exc
    candidates: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        bases = {
            base.id if isinstance(base, ast.Name) else base.attr
            for base in node.bases
            if isinstance(base, (ast.Name, ast.Attribute))
        }
        if "IStrategy" in bases:
            candidates.append(node.name)
    if len(candidates) != 1:
        raise Blocked("strategy must define exactly one top-level IStrategy class")
    return candidates[0]


def _quality_assumptions(bundle: dict[str, object]) -> tuple[float, float, list[dict[str, object]]]:
    payload = _single_configuration(bundle, "QUALITY_QUALIFICATION")
    gates = payload.get("required_window_gates")
    if not isinstance(gates, list):
        raise Blocked("quality gates are absent")
    normalized = [dict(row) for row in gates if isinstance(row, dict)]
    if len(normalized) != len(gates):
        raise Blocked("quality gate is invalid")
    by_metric = {str(row.get("metric")): row for row in normalized}
    try:
        fee = float(by_metric["fee_rate"]["threshold"])
        slippage = float(by_metric["slippage_rate"]["threshold"])
    except (KeyError, TypeError, ValueError) as exc:
        raise Blocked("cost assumptions are absent") from exc
    if not math.isfinite(fee) or not math.isfinite(slippage) or fee < 0 or slippage < 0:
        raise Blocked("cost assumptions are invalid")
    return fee, slippage, normalized


def validate_inputs(
    request_path: Path, bundle_path: Path, plan_path: Path, strategy_path: Path
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    request = _load_object(request_path)
    bundle = _load_object(bundle_path)
    plan = _load_object(plan_path)
    if request.get("contract") != REQUEST_CONTRACT:
        raise Blocked("request contract drifted")
    for field in (
        "attempt_request_digest",
        "artifact_digest",
        "bundle_digest",
        "market_snapshot_digest",
        "validation_plan_digest",
    ):
        _hex_digest(request.get(field))
    strategy_digest, _ = _file_digest(strategy_path)
    if strategy_digest != request["artifact_digest"]:
        raise Blocked("strategy digest drifted")
    if bundle.get("configuration_bundle_digest") != request["bundle_digest"]:
        raise Blocked("bundle digest drifted")
    if bundle.get("market_snapshot_digest") != request["market_snapshot_digest"]:
        raise Blocked("market snapshot digest drifted")
    if plan.get("validation_plan_digest") != request["validation_plan_digest"]:
        raise Blocked("validation plan digest drifted")
    capability = bundle.get("capability")
    if not isinstance(capability, dict) or any(capability.get(key) != value for key, value in EXPECTED_CAPABILITY.items()):
        raise Blocked("no-trade capability drifted")
    targets = bundle.get("targets")
    if not isinstance(targets, list) or len(targets) != 1 or not isinstance(targets[0], dict):
        raise Blocked("worker requires one exact target")
    windows = plan.get("windows")
    if not isinstance(windows, list) or not any(isinstance(row, dict) and row.get("required") is True for row in windows):
        raise Blocked("required window set is empty")
    market_files = request.get("market_files")
    if not isinstance(market_files, list) or not market_files:
        raise Blocked("market file set is empty")
    for item in market_files:
        if not isinstance(item, dict) or set(item) != {"path", "content_digest", "size_bytes"}:
            raise Blocked("market file contract drifted")
        path = Path(str(item["path"]))
        if not path.is_absolute() or path.parent != Path("/input"):
            raise Blocked("market path is outside /input")
        digest, size = _file_digest(path)
        if digest != _hex_digest(item["content_digest"]) or size != item["size_bytes"]:
            raise Blocked("market file digest drifted")
    return request, bundle, plan


def _prepare_data(request: dict[str, object], target: dict[str, object]) -> None:
    import pandas as pd
    from freqtrade.data.history.datahandlers.featherdatahandler import FeatherDataHandler
    from freqtrade.enums import CandleType

    rows: list[dict[str, object]] = []
    for item in request["market_files"]:  # type: ignore[union-attr]
        with Path(item["path"]).open("r", encoding="utf-8") as stream:
            for line in stream:
                value = json.loads(line)
                if not isinstance(value, dict) or set(value) != {
                    "opened_at", "open", "high", "low", "close", "volume", "volume_currency", "volume_quote"
                }:
                    raise Blocked("market row contract drifted")
                rows.append(value)
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime([row["opened_at"] for row in rows], utc=True),
            "open": [float(row["open"]) for row in rows],
            "high": [float(row["high"]) for row in rows],
            "low": [float(row["low"]) for row in rows],
            "close": [float(row["close"]) for row in rows],
            "volume": [float(row["volume"]) for row in rows],
        }
    ).sort_values("date")
    if frame.empty or frame["date"].duplicated().any() or not frame["date"].is_monotonic_increasing:
        raise Blocked("market rows are empty, duplicated, or unordered")
    FeatherDataHandler(Path("/work/data")).ohlcv_store(
        str(target["pair"]), str(target["timeframe"]), frame, CandleType.FUTURES
    )


def _operator_pass(value: float, operator: object, threshold: object) -> bool:
    expected = float(threshold)
    return {
        ">=": value >= expected,
        ">": value > expected,
        "<=": value <= expected,
        "<": value < expected,
        "==": value == expected,
    }.get(str(operator), False)


def _result_metrics(result: dict[str, object], fee: float, slippage: float, gates: list[dict[str, object]]) -> dict[str, float | int]:
    trade_count = int(result.get("total_trades", 0))
    raw_return = float(result.get("profit_total", float(result.get("profit_total_pct", 0)) / 100.0))
    maximum_drawdown = float(result.get("max_drawdown_account", result.get("max_drawdown", 0)))
    net_return = raw_return - (2.0 * slippage * trade_count)
    trades = result.get("trades")
    profits = [float(row.get("profit_ratio", 0)) for row in trades if isinstance(row, dict)] if isinstance(trades, list) else []
    mean = sum(profits) / len(profits) if profits else 0.0
    variance = sum((value - mean) ** 2 for value in profits) / len(profits) if profits else 0.0
    metrics: dict[str, float | int] = {
        "trade_count": trade_count,
        "net_return_after_cost": net_return,
        "maximum_drawdown": maximum_drawdown,
        "fee_rate": fee,
        "slippage_rate": slippage,
        "lookahead_failure_count": 0,
        "return_stability": 1.0 / (1.0 + math.sqrt(variance)),
    }
    passed = sum(
        _operator_pass(float(metrics.get(str(gate.get("metric")), float("nan"))), gate.get("operator"), gate.get("threshold"))
        for gate in gates
    )
    metrics["quality_gate_pass_ratio"] = passed / len(gates)
    if not all(math.isfinite(float(value)) for value in metrics.values()):
        raise Blocked("backtest metrics are not finite")
    return metrics


def _read_export(result_dir: Path, strategy_class: str) -> dict[str, object]:
    archives = sorted(result_dir.glob("*.zip"), key=lambda path: path.stat().st_mtime_ns)
    if len(archives) != 1:
        raise Blocked("Freqtrade export archive is ambiguous")
    with zipfile.ZipFile(archives[0]) as archive:
        names = [name for name in archive.namelist() if name.endswith(".json") and not name.endswith(".meta.json")]
        if len(names) != 1:
            raise Blocked("Freqtrade export payload is ambiguous")
        payload = json.loads(archive.read(names[0]))
    strategy = payload.get("strategy") if isinstance(payload, dict) else None
    result = strategy.get(strategy_class) if isinstance(strategy, dict) else None
    if not isinstance(result, dict):
        raise Blocked("Freqtrade strategy result is absent")
    return result


def preflight_evidence() -> dict[str, object]:
    status = {}
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith(("CapEff:", "NoNewPrivs:")):
            key, value = line.split(":", 1)
            status[key] = value.strip()
    mounts: dict[str, list[str]] = {}
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split()
        if len(fields) > 5 and fields[4] in {"/", "/input", "/work"}:
            mounts[fields[4]] = fields[5].split(",")
    cgroup = Path("/sys/fs/cgroup")
    limits = {}
    for name in ("cpu.max", "memory.max", "memory.swap.max", "pids.max"):
        path = cgroup / name
        limits[name] = path.read_text().strip() if path.is_file() else "unavailable"
    interfaces = sorted(path.name for path in Path("/sys/class/net").iterdir())
    return {
        "contract": PREFLIGHT_CONTRACT,
        "uid": os.getuid(),
        "gid": os.getgid(),
        "network_required": False,
        "network_interfaces": interfaces,
        "effective_capabilities_hex": status.get("CapEff"),
        "no_new_privileges": status.get("NoNewPrivs") == "1",
        "mount_options": mounts,
        "cgroup_limits": limits,
        "host_sockets_present": any(
            Path(path).exists()
            for path in ("/run/podman/podman.sock", "/var/run/docker.sock")
        ),
        "worker_path": str(Path(__file__).resolve()),
    }


def _run_window(target: dict[str, object], strategy_class: str, window: dict[str, object], fee: float) -> dict[str, object]:
    start = _timestamp(window.get("window_start"))
    end = _timestamp(window.get("window_end"))
    if end <= start:
        raise Blocked("window interval is invalid")
    key = str(window.get("window_key"))
    result_dir = Path("/work/results") / sha256(key.encode()).hexdigest()[:16]
    result_dir.mkdir(parents=True, exist_ok=False)
    config = {
        "dry_run": True,
        "dry_run_wallet": 10000,
        "stake_currency": "USDT",
        "stake_amount": 100,
        "max_open_trades": 1,
        "timeframe": str(target["timeframe"]),
        "trading_mode": "futures",
        "margin_mode": "isolated",
        "exchange": {"name": "okx", "pair_whitelist": [str(target["pair"])], "enable_ws": False},
        "pairlists": [{"method": "StaticPairList"}],
    }
    config_path = Path("/work/config.json")
    config_path.write_bytes(_canonical_bytes(config))
    command = (
        "/home/ftuser/.local/bin/freqtrade", "backtesting", "--config", str(config_path),
        "--datadir", "/work/data", "--strategy-path", "/input", "--strategy", strategy_class,
        "--pairs", str(target["pair"]), "--timeframe", str(target["timeframe"]),
        "--timerange", f"{start:%Y%m%d%H%M%S}-{end:%Y%m%d%H%M%S}", "--fee", str(fee),
        "--cache", "none", "--export", "trades", "--backtest-directory", str(result_dir), "--no-color",
    )
    completed = subprocess.run(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={
            "HOME": "/work/home",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        close_fds=True, timeout=840, check=False,
    )
    if completed.returncode != 0:
        raise Blocked("Freqtrade process failed")
    return _read_export(result_dir, strategy_class)


def backtest(args: argparse.Namespace) -> dict[str, object]:
    request, bundle, plan = validate_inputs(args.request, args.bundle, args.plan, args.strategy)
    target = bundle["targets"][0]  # type: ignore[index]
    strategy_class = _strategy_class(args.strategy)
    fee, slippage, gates = _quality_assumptions(bundle)
    Path("/work/home").mkdir(parents=True, exist_ok=True)
    _prepare_data(request, target)
    windows = []
    for window in plan["windows"]:  # type: ignore[union-attr]
        if window.get("required") is not True:
            continue
        result = _run_window(target, strategy_class, window, fee)
        windows.append(
            {
                "window_key": window["window_key"],
                "window_member_digest": window["window_member_digest"],
                "metrics": _result_metrics(result, fee, slippage, gates),
            }
        )
    return {
        "contract": OUTPUT_CONTRACT,
        "validation_attempt_id": request["validation_attempt_id"],
        "attempt_request_digest": request["attempt_request_digest"],
        "status": "SUCCEEDED",
        "windows": windows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("preflight")
    run = subparsers.add_parser("backtest")
    for name in ("request", "bundle", "plan", "strategy"):
        run.add_argument(f"--{name}", type=Path, required=True)
    run.add_argument("--output", choices=("-",), required=True)
    args = parser.parse_args()
    if args.command == "preflight":
        result: dict[str, object] = preflight_evidence()
    else:
        try:
            result = backtest(args)
        except Exception:
            try:
                request = _load_object(args.request)
            except Exception:
                return 2
            result = {
                "contract": OUTPUT_CONTRACT,
                "validation_attempt_id": request.get("validation_attempt_id"),
                "attempt_request_digest": request.get("attempt_request_digest"),
                "status": "BLOCKED",
                "windows": [],
            }
    sys.stdout.buffer.write(_canonical_bytes(result) + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
