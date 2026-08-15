#!/usr/bin/env python3
"""Networkless one-shot worker for the canonical V1.3 research adapter."""

from __future__ import annotations

import argparse
import ast
import csv
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import zipfile


REQUEST_CONTRACT = "canonical-v13-freqtrade-backtest-request-v1"
OUTPUT_CONTRACT = "canonical-v13-freqtrade-backtest-output-v1"
LOOKAHEAD_REQUEST_CONTRACT = "canonical-v13-freqtrade-lookahead-request-v1"
LOOKAHEAD_OUTPUT_CONTRACT = "canonical-v13-freqtrade-lookahead-output-v3"
PREFLIGHT_CONTRACT = "canonical-v13-research-worker-preflight-v1"
EXPECTED_CAPABILITY = {
    "trading": "TRADING_DISABLED",
    "exchange_access": "NONE",
    "order_submission": "DISABLED",
    "allow_real_funds": False,
}
FREQTRADE_SUBPROCESS_ENV = {
    "HOME": "/work/home",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONPATH": "/freqtrade:/home/ftuser/.local/lib/python3.14/site-packages",
}
HEX = frozenset("0123456789abcdef")


class Blocked(RuntimeError):
    pass


class LookaheadBlocked(Blocked):
    def __init__(
        self,
        reason_code: str,
        *,
        observed_trade_count: int | None = None,
        required_trade_count: int | None = None,
    ) -> None:
        self.reason_code = reason_code
        self.observed_trade_count = observed_trade_count
        self.required_trade_count = required_trade_count
        super().__init__(reason_code)


_INSUFFICIENT_TRADES = re.compile(
    r"found (?P<observed>\d+) trades which is less than minimum_trade_amount "
    r"(?P<required>\d+)\."
)
_MAXIMUM_LOOKAHEAD_LOG_BYTES = 1_000_000
_LOOKAHEAD_FAILURE_DETAILS = {
    "LOOKAHEAD_EXPORT_MISSING": "Freqtrade lookahead export is unavailable",
    "LOOKAHEAD_INSUFFICIENT_TRADES": "Freqtrade observed fewer trades than required",
    "LOOKAHEAD_LOG_LIMIT_EXCEEDED": "Freqtrade lookahead log exceeded the safe limit",
    "LOOKAHEAD_PROCESS_FAILED": "Freqtrade lookahead did not complete successfully",
    "LOOKAHEAD_RESULT_AMBIGUOUS": "Freqtrade lookahead result does not uniquely match the strategy",
    "LOOKAHEAD_WORKER_BLOCKED": "lookahead worker rejected the frozen input",
    "LOOKAHEAD_WORKER_INTERNAL_ERROR": "lookahead worker failed closed",
}


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


def _freqtrade_timerange(start: datetime, end: datetime) -> str:
    """Preserve exact intraday UTC bounds using Freqtrade's Unix syntax."""

    if end <= start:
        raise Blocked("window interval is invalid")
    return f"{int(start.timestamp())}-{int(end.timestamp())}"


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


def validate_lookahead_inputs(
    request_path: Path, bundle_path: Path, strategy_path: Path
) -> tuple[dict[str, object], dict[str, object]]:
    request = _load_object(request_path)
    bundle = _load_object(bundle_path)
    if request.get("contract") != LOOKAHEAD_REQUEST_CONTRACT:
        raise Blocked("lookahead request contract drifted")
    for field in (
        "request_digest",
        "artifact_digest",
        "bundle_digest",
        "market_snapshot_digest",
    ):
        _hex_digest(request.get(field))
    expected_request = {
        key: value for key, value in request.items() if key != "request_digest"
    }
    if _digest(expected_request) != request["request_digest"]:
        raise Blocked("lookahead request digest drifted")
    strategy_digest, _ = _file_digest(strategy_path)
    if strategy_digest != request["artifact_digest"]:
        raise Blocked("strategy digest drifted")
    if bundle.get("configuration_bundle_digest") != request["bundle_digest"]:
        raise Blocked("bundle digest drifted")
    if bundle.get("market_snapshot_digest") != request["market_snapshot_digest"]:
        raise Blocked("market snapshot digest drifted")
    capability = bundle.get("capability")
    if not isinstance(capability, dict) or any(
        capability.get(key) != value for key, value in EXPECTED_CAPABILITY.items()
    ):
        raise Blocked("no-trade capability drifted")
    targets = bundle.get("targets")
    if not isinstance(targets, list) or len(targets) != 1 or not isinstance(targets[0], dict):
        raise Blocked("worker requires one exact target")
    target = targets[0]
    if target.get("research_target_id") != request.get("research_target_id"):
        raise Blocked("lookahead target lineage drifted")
    windows = request.get("windows")
    if not isinstance(windows, list) or not windows:
        raise Blocked("lookahead required window set is empty")
    seen_window_keys: set[str] = set()
    for window in windows:
        if not isinstance(window, dict) or set(window) != {
            "window_key",
            "window_member_digest",
            "window_start",
            "window_end",
            "minimum_closed_candles",
        }:
            raise Blocked("lookahead window contract drifted")
        key = str(window["window_key"])
        if not key or key in seen_window_keys:
            raise Blocked("lookahead window key is invalid")
        seen_window_keys.add(key)
        _hex_digest(window["window_member_digest"])
        start = _timestamp(window["window_start"])
        end = _timestamp(window["window_end"])
        minimum_closed_candles = window["minimum_closed_candles"]
        if (
            end <= start
            or isinstance(minimum_closed_candles, bool)
            or not isinstance(minimum_closed_candles, int)
            or minimum_closed_candles <= 0
        ):
            raise Blocked("lookahead window interval is invalid")
    market_files = request.get("market_files")
    if not isinstance(market_files, list) or not market_files:
        raise Blocked("market file set is empty")
    for item in market_files:
        if not isinstance(item, dict) or set(item) != {
            "path",
            "content_digest",
            "size_bytes",
        }:
            raise Blocked("market file contract drifted")
        path = Path(str(item["path"]))
        if not path.is_absolute() or path.parent != Path("/input"):
            raise Blocked("market path is outside /input")
        digest, size = _file_digest(path)
        if digest != _hex_digest(item["content_digest"]) or size != item["size_bytes"]:
            raise Blocked("market file digest drifted")
    metadata = request.get("exchange_metadata")
    if not isinstance(metadata, dict) or set(metadata) != {
        "path", "content_digest", "size_bytes", "receipt_digest"
    }:
        raise Blocked("offline exchange metadata contract drifted")
    metadata_path = Path(str(metadata["path"]))
    if metadata_path != Path("/input/exchange-metadata.json"):
        raise Blocked("offline exchange metadata path drifted")
    digest, size = _file_digest(metadata_path)
    if (
        digest != _hex_digest(metadata["content_digest"])
        or size != metadata["size_bytes"]
    ):
        raise Blocked("offline exchange metadata digest drifted")
    _hex_digest(metadata["receipt_digest"])
    return request, bundle


def _install_offline_exchange_patch(metadata_path: Path) -> None:
    metadata = _load_object(metadata_path)
    if (
        metadata.get("contract")
        != "canonical-v13-okx-offline-exchange-metadata-v1"
        or metadata.get("freqtrade_version") != "2026.6"
        or metadata.get("ccxt_version") != "4.5.61"
        or metadata.get("credential_access") != "NONE"
        or metadata.get("network_access") != "PUBLIC_MARKET_DATA_ONLY"
    ):
        raise Blocked("offline exchange metadata identity drifted")
    markets = metadata.get("markets")
    tiers = metadata.get("leverage_tiers")
    if (
        not isinstance(markets, dict)
        or set(markets) != {"BTC/USDT:USDT"}
        or not isinstance(tiers, dict)
        or set(tiers) != set(markets)
        or not isinstance(tiers["BTC/USDT:USDT"], list)
        or not tiers["BTC/USDT:USDT"]
    ):
        raise Blocked("offline exchange metadata set is invalid")

    from freqtrade.resolvers.exchange_resolver import ExchangeResolver

    original = ExchangeResolver.load_exchange

    def load_exchange(config, *, exchange_config=None, validate=True, load_leverage_tiers=False):
        exchange = original(
            config,
            exchange_config=exchange_config,
            validate=False,
            load_leverage_tiers=False,
        )
        market_rows = [dict(row) for row in markets.values() if isinstance(row, dict)]
        if len(market_rows) != 1:
            raise Blocked("offline market row is invalid")
        exchange._api.set_markets(market_rows)
        exchange._api_async.set_markets(market_rows)
        exchange._markets = exchange._api.markets
        exchange._last_markets_refresh = int(datetime.now(timezone.utc).timestamp() * 1000)
        exchange._leverage_tiers = {
            pair: [exchange.parse_leverage_tier(row) for row in rows]
            for pair, rows in tiers.items()
        }
        if set(exchange._markets) != set(markets) or set(exchange._leverage_tiers) != set(markets):
            raise Blocked("offline exchange hydration is incomplete")
        return exchange

    ExchangeResolver.load_exchange = staticmethod(load_exchange)


def _run_freqtrade_offline(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] != "--metadata":
        return 2
    _install_offline_exchange_patch(Path(argv[1]))
    Path("/work/user_data").mkdir(parents=True, exist_ok=True)
    from freqtrade.main import main as freqtrade_main

    return int(freqtrade_main(argv[2:]) or 0)


def _lookahead_output(
    request: dict[str, object],
    *,
    status: str,
    has_bias: bool | None,
    observed_signal_count: int,
    window_results: list[dict[str, object]],
    failure_stage: str | None,
    failure_code: str | None,
    tool_return_code: int | None,
    stdout_digest: str,
    stderr_digest: str,
    redacted_detail: str | None,
    blocked_observed_trade_count: int | None = None,
    blocked_required_trade_count: int | None = None,
) -> dict[str, object]:
    evidence = {
        "contract": LOOKAHEAD_OUTPUT_CONTRACT,
        "request_digest": request.get("request_digest"),
        "strategy_version_id": request.get("strategy_version_id"),
        "research_target_id": request.get("research_target_id"),
        "status": status,
        "has_bias": has_bias,
        "observed_signal_count": observed_signal_count,
        "blocked_observed_trade_count": blocked_observed_trade_count,
        "blocked_required_trade_count": blocked_required_trade_count,
        "window_results": window_results,
        "failure_stage": failure_stage,
        "failure_code": failure_code,
        "tool_return_code": tool_return_code,
        "stdout_digest": stdout_digest,
        "stderr_digest": stderr_digest,
        "redacted_detail": redacted_detail,
    }
    return {**evidence, "evidence_digest": _digest(evidence)}


def _prepare_data(request: dict[str, object], target: dict[str, object]):
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
    data_root = Path("/work/data")
    (data_root / "futures").mkdir(parents=True, exist_ok=True)
    FeatherDataHandler(data_root).ohlcv_store(
        str(target["pair"]), str(target["timeframe"]), frame, CandleType.FUTURES
    )
    return frame


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
        "--timerange", _freqtrade_timerange(start, end), "--fee", str(fee),
        "--cache", "none", "--export", "trades", "--backtest-directory", str(result_dir), "--no-color",
    )
    completed = subprocess.run(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=FREQTRADE_SUBPROCESS_ENV,
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


def lookahead(args: argparse.Namespace) -> dict[str, object]:
    request, bundle = validate_lookahead_inputs(
        args.request, args.bundle, args.strategy
    )
    target = bundle["targets"][0]  # type: ignore[index]
    strategy_class = _strategy_class(args.strategy)
    fee, _slippage, _gates = _quality_assumptions(bundle)
    Path("/work/home").mkdir(parents=True, exist_ok=True)
    Path("/work/user_data").mkdir(parents=True, exist_ok=True)
    frame = _prepare_data(request, target)
    config = {
        "dry_run": True,
        "dry_run_wallet": 10000,
        "stake_currency": "USDT",
        "stake_amount": 100,
        "max_open_trades": 1,
        "timeframe": str(target["timeframe"]),
        "trading_mode": "futures",
        "margin_mode": "isolated",
        "entry_pricing": {"price_side": "other", "use_order_book": False},
        "exit_pricing": {"price_side": "other", "use_order_book": False},
        "exchange": {
            "name": "okx",
            "pair_whitelist": [str(target["pair"])],
            "enable_ws": False,
        },
        "pairlists": [{"method": "StaticPairList"}],
    }
    config_path = Path("/work/lookahead-config.json")
    config_path.write_bytes(_canonical_bytes(config))
    window_results: list[dict[str, object]] = []
    captured_stdout = bytearray()
    captured_stderr = bytearray()
    for index, window in enumerate(request["windows"]):  # type: ignore[union-attr]
        start = _timestamp(window["window_start"])
        end = _timestamp(window["window_end"])
        closed_candle_count = int(
            ((frame["date"] >= start) & (frame["date"] < end)).sum()
        )
        if closed_candle_count < int(window["minimum_closed_candles"]):
            raise Blocked("lookahead window has insufficient closed-candle coverage")
        export_path = Path(f"/work/lookahead-{index:04d}.csv")
        log_path = Path(f"/work/lookahead-{index:04d}.stderr")
        command = (
            "/opt/freqtrade-ai/bin/canonical-v13-research-worker",
            "freqtrade-offline",
            "--metadata",
            str(request["exchange_metadata"]["path"]),
            "lookahead-analysis",
            "--no-color",
            "--config",
            str(config_path),
            "--datadir",
            "/work/data",
            "--strategy-path",
            "/input",
            "--userdir",
            "/work/user_data",
            "--timerange",
            _freqtrade_timerange(start, end),
            "--timeframe",
            str(target["timeframe"]),
            "--fee",
            str(fee),
            "--minimum-trade-amount",
            "10",
            "--targeted-trade-amount",
            "20",
            "--lookahead-analysis-exportfilename",
            str(export_path),
            "--strategy-list",
            strategy_class,
        )
        with log_path.open("wb") as stderr:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=stderr,
                env=FREQTRADE_SUBPROCESS_ENV,
                close_fds=True,
                timeout=840,
                check=False,
            )
        if log_path.stat().st_size > _MAXIMUM_LOOKAHEAD_LOG_BYTES:
            log_hasher = sha256(bytes(captured_stderr))
            with log_path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    log_hasher.update(chunk)
            return _lookahead_output(
                request,
                status="BLOCKED",
                has_bias=None,
                observed_signal_count=0,
                window_results=[],
                failure_stage="FREQTRADE_PROCESS",
                failure_code="LOOKAHEAD_LOG_LIMIT_EXCEEDED",
                tool_return_code=completed.returncode,
                stdout_digest=sha256(bytes(captured_stdout)).hexdigest(),
                stderr_digest=log_hasher.hexdigest(),
                redacted_detail="Freqtrade lookahead log exceeded the safe limit",
            )
        log_bytes = log_path.read_bytes()
        captured_stderr.extend(log_bytes)
        log_text = log_bytes.decode("utf-8", errors="replace")
        if completed.returncode != 0:
            return _lookahead_output(
                request,
                status="BLOCKED",
                has_bias=None,
                observed_signal_count=0,
                window_results=[],
                failure_stage="FREQTRADE_PROCESS",
                failure_code="LOOKAHEAD_PROCESS_FAILED",
                tool_return_code=completed.returncode,
                stdout_digest=sha256(bytes(captured_stdout)).hexdigest(),
                stderr_digest=sha256(bytes(captured_stderr)).hexdigest(),
                redacted_detail="Freqtrade lookahead did not complete successfully",
            )
        if not export_path.is_file():
            return _lookahead_output(
                request,
                status="BLOCKED",
                has_bias=None,
                observed_signal_count=0,
                window_results=[],
                failure_stage="OUTPUT_INTERPRETATION",
                failure_code="LOOKAHEAD_EXPORT_MISSING",
                tool_return_code=completed.returncode,
                stdout_digest=sha256(bytes(captured_stdout)).hexdigest(),
                stderr_digest=sha256(bytes(captured_stderr)).hexdigest(),
                redacted_detail="Freqtrade lookahead export is unavailable",
            )
        with export_path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        selected = [row for row in rows if row.get("strategy") == strategy_class]
        if len(selected) != 1:
            insufficient = _INSUFFICIENT_TRADES.search(log_text)
            if insufficient is not None:
                return _lookahead_output(
                    request,
                    status="BLOCKED",
                    has_bias=None,
                    observed_signal_count=0,
                    window_results=[],
                    failure_stage="OUTPUT_INTERPRETATION",
                    failure_code="LOOKAHEAD_INSUFFICIENT_TRADES",
                    tool_return_code=completed.returncode,
                    stdout_digest=sha256(bytes(captured_stdout)).hexdigest(),
                    stderr_digest=sha256(bytes(captured_stderr)).hexdigest(),
                    redacted_detail="Freqtrade observed fewer trades than required",
                    blocked_observed_trade_count=int(insufficient.group("observed")),
                    blocked_required_trade_count=int(insufficient.group("required")),
                )
            return _lookahead_output(
                request,
                status="BLOCKED",
                has_bias=None,
                observed_signal_count=0,
                window_results=[],
                failure_stage="OUTPUT_INTERPRETATION",
                failure_code="LOOKAHEAD_RESULT_AMBIGUOUS",
                tool_return_code=completed.returncode,
                stdout_digest=sha256(bytes(captured_stdout)).hexdigest(),
                stderr_digest=sha256(bytes(captured_stderr)).hexdigest(),
                redacted_detail="Freqtrade lookahead result does not uniquely match the strategy",
            )
        row = selected[0]
        if row.get("has_bias") not in {"True", "False"}:
            raise Blocked("Freqtrade lookahead bias result is invalid")
        try:
            total_signals = int(row["total_signals"])
            biased_entry = int(row["biased_entry_signals"])
            biased_exit = int(row["biased_exit_signals"])
        except (KeyError, TypeError, ValueError) as exc:
            raise Blocked("Freqtrade lookahead counts are invalid") from exc
        if min(total_signals, biased_entry, biased_exit) < 0:
            raise Blocked("Freqtrade lookahead counts are negative")
        window_results.append(
            {
                "window_key": window["window_key"],
                "window_member_digest": window["window_member_digest"],
                "has_bias": row["has_bias"] == "True",
                "observed_signal_count": total_signals,
                "biased_entry_signal_count": biased_entry,
                "biased_exit_signal_count": biased_exit,
            }
        )
    has_bias = any(bool(row["has_bias"]) for row in window_results)
    observed_signal_count = sum(int(row["observed_signal_count"]) for row in window_results)
    return _lookahead_output(
        request,
        status="FAILED" if has_bias else "PASSED",
        has_bias=has_bias,
        observed_signal_count=observed_signal_count,
        window_results=window_results,
        failure_stage=None,
        failure_code=None,
        tool_return_code=0,
        stdout_digest=sha256(bytes(captured_stdout)).hexdigest(),
        stderr_digest=sha256(bytes(captured_stderr)).hexdigest(),
        redacted_detail=None,
    )


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "freqtrade-offline":
        return _run_freqtrade_offline(sys.argv[2:])
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("preflight")
    run = subparsers.add_parser("backtest")
    for name in ("request", "bundle", "plan", "strategy"):
        run.add_argument(f"--{name}", type=Path, required=True)
    run.add_argument("--output", choices=("-",), required=True)
    gate = subparsers.add_parser("lookahead")
    for name in ("request", "bundle", "strategy"):
        gate.add_argument(f"--{name}", type=Path, required=True)
    gate.add_argument("--output", choices=("-",), required=True)
    args = parser.parse_args()
    if args.command == "preflight":
        result: dict[str, object] = preflight_evidence()
    elif args.command == "backtest":
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
    else:
        try:
            result = lookahead(args)
        except Exception as exc:
            try:
                request = _load_object(args.request)
            except Exception:
                return 2
            if isinstance(exc, LookaheadBlocked):
                failure_code = exc.reason_code
                blocked_observed_trade_count = exc.observed_trade_count
                blocked_required_trade_count = exc.required_trade_count
                failure_stage = (
                    "OUTPUT_INTERPRETATION"
                    if failure_code
                    in {
                        "LOOKAHEAD_EXPORT_MISSING",
                        "LOOKAHEAD_INSUFFICIENT_TRADES",
                        "LOOKAHEAD_RESULT_AMBIGUOUS",
                    }
                    else "FREQTRADE_PROCESS"
                )
            elif isinstance(exc, Blocked):
                failure_code = "LOOKAHEAD_WORKER_BLOCKED"
                blocked_observed_trade_count = None
                blocked_required_trade_count = None
                failure_stage = "WORKER"
            else:
                failure_code = "LOOKAHEAD_WORKER_INTERNAL_ERROR"
                blocked_observed_trade_count = None
                blocked_required_trade_count = None
                failure_stage = "WORKER"
            result = _lookahead_output(
                request,
                status="BLOCKED",
                has_bias=None,
                observed_signal_count=0,
                window_results=[],
                failure_stage=failure_stage,
                failure_code=failure_code,
                tool_return_code=None,
                stdout_digest=sha256(b"").hexdigest(),
                stderr_digest=sha256(b"").hexdigest(),
                redacted_detail=_LOOKAHEAD_FAILURE_DETAILS[failure_code],
                blocked_observed_trade_count=blocked_observed_trade_count,
                blocked_required_trade_count=blocked_required_trade_count,
            )
    sys.stdout.buffer.write(_canonical_bytes(result) + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
