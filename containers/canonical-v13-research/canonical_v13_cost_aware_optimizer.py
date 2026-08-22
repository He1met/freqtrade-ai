#!/usr/bin/env python3
"""Deterministic network-none TRAIN/VALIDATION optimizer for canonical V1.3."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
import random
import subprocess
import zipfile

import pandas as pd
from freqtrade.data.history.datahandlers.featherdatahandler import FeatherDataHandler
from freqtrade.enums import CandleType


CONTRACT = "canonical-v13-cost-aware-oos-optimization-result-v1"
SUBPROCESS_ENV = {
    "HOME": "/work/home",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONPATH": "/freqtrade:/home/ftuser/.local/lib/python3.14/site-packages",
}


class Blocked(RuntimeError):
    pass


def canonical_digest(value: object) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def load_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise Blocked("JSON root must be an object")
    return value


def iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise Blocked("window timezone is required")
    return parsed.astimezone(timezone.utc)


def timerange(start: datetime, end: datetime) -> str:
    if end <= start:
        raise Blocked("window interval is invalid")
    return f"{int(start.timestamp())}-{int(end.timestamp())}"


def prepare_data(market: Path) -> pd.DataFrame:
    rows = []
    with market.open("r", encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise Blocked("market row is invalid")
            rows.append(row)
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
        raise Blocked("market continuity is invalid")
    data_root = Path("/work/data")
    (data_root / "futures").mkdir(parents=True, exist_ok=True)
    handler = FeatherDataHandler(data_root)
    handler.ohlcv_store("BTC/USDT:USDT", "15m", frame, CandleType.FUTURES)
    return frame


def sample_trials(plan: dict[str, object]) -> list[dict[str, object]]:
    execution = plan["execution"]
    families = plan["families"]
    if not isinstance(execution, dict) or not isinstance(families, list):
        raise Blocked("optimization plan structure is invalid")
    seed = int(execution["seed"])
    trials: list[dict[str, object]] = []
    number = 0
    for family_index, family in enumerate(families):
        if not isinstance(family, dict) or not isinstance(family.get("parameters"), dict):
            raise Blocked("family structure is invalid")
        rng = random.Random(seed + family_index)
        space = family["parameters"]
        keys = sorted(space)
        seen: set[str] = set()
        family_trials = int(family["trial_count"])
        while len(seen) < family_trials:
            parameters = {key: rng.choice(space[key]) for key in keys}
            if not (
                int(parameters["fast_window"])
                < int(parameters["slow_window"])
                < int(parameters["regime_window"])
            ):
                continue
            digest = canonical_digest(parameters)
            if digest in seen:
                continue
            seen.add(digest)
            number += 1
            trials.append(
                {
                    "trial_number": number,
                    "family_key": family["family_key"],
                    "parameters": parameters,
                }
            )
    if len(trials) != int(execution["trial_budget"]):
        raise Blocked("trial budget drifted")
    return trials


def class_name(family: str, number: int) -> str:
    name = "".join(part.title() for part in family.split("-"))
    return f"CanonicalCostAware{name}Trial{number:03d}"


def render_strategy(
    family: str,
    number: int,
    p: dict[str, object],
    fixed_contract: dict[str, object] | None = None,
) -> tuple[str, str]:
    cls = class_name(family, number)
    bidirectional = family.startswith("bidirectional-")
    required_fixed_keys = {
        "startup_extra_closed_candles",
        "rsi_period",
        "adx_period",
        "atr_period",
        "atr_median_window",
        "volume_mean_window",
        "band_window",
        "band_deviation",
        "band_median_window",
        "atr_kill_multiplier",
        "momentum_kill_adx",
        "momentum_kill_rsi_band",
        "minimal_roi",
    }
    if bidirectional and (
        not isinstance(fixed_contract, dict)
        or not required_fixed_keys.issubset(fixed_contract)
    ):
        raise Blocked("bidirectional fixed strategy contract is required")
    fixed = fixed_contract or {
        "startup_extra_closed_candles": 48,
        "rsi_period": 14,
        "adx_period": 14,
        "atr_period": 14,
        "atr_median_window": 96,
        "volume_mean_window": 32,
        "band_window": 32,
        "band_deviation": 2.0,
        "band_median_window": 96,
        "atr_kill_multiplier": 2.5,
        "momentum_kill_adx": 14.0,
        "momentum_kill_rsi_band": 6.0,
        "minimal_roi": {"0": 0.02, "1440": 0.01, "2880": 0.005},
    }
    roi_source = json.dumps(fixed["minimal_roi"], sort_keys=True)
    direction_header = "can_short = True" if bidirectional else "can_short = False"
    leverage_expression = (
        "min(1.0, max_leverage)"
        if bidirectional
        else "min(2.0, max_leverage)"
    )
    bidirectional_position_contract = (
        '    max_entry_position_adjustment = 0\n'
        '    exit_position_semantics = "POSITION_CLOSING_REDUCE_ONLY"\n'
        if bidirectional
        else ""
    )
    common = f'''import talib.abstract as ta
from pandas import DataFrame
from freqtrade.strategy import IStrategy

class {cls}(IStrategy):
    timeframe = "15m"
    {direction_header}
    startup_candle_count = {int(p["regime_window"]) + int(fixed["startup_extra_closed_candles"])}
    stoploss = {float(p["stoploss"])}
    minimal_roi = {roi_source}
    position_adjustment_enable = False
{bidirectional_position_contract}    protections = [{{"method": "CooldownPeriod", "stop_duration_candles": {int(p["cooldown_bars"])}}}]

    def leverage(self, pair, current_time, current_rate, proposed_leverage, max_leverage, entry_tag, side, **kwargs):
        return {leverage_expression}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema_fast"] = ta.EMA(dataframe, timeperiod={int(p["fast_window"])})
        dataframe["ema_slow"] = ta.EMA(dataframe, timeperiod={int(p["slow_window"])})
        dataframe["ema_regime"] = ta.EMA(dataframe, timeperiod={int(p["regime_window"])})
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod={int(fixed["rsi_period"])})
        dataframe["adx"] = ta.ADX(dataframe, timeperiod={int(fixed["adx_period"])})
        dataframe["atr"] = ta.ATR(dataframe, timeperiod={int(fixed["atr_period"])})
        dataframe["atr_ratio"] = dataframe["atr"] / dataframe["close"]
        dataframe["atr_median"] = dataframe["atr_ratio"].rolling({int(fixed["atr_median_window"])}).median().shift(1)
        dataframe["volume_mean"] = dataframe["volume"].rolling({int(fixed["volume_mean_window"])}).mean().shift(1)
'''
    if family == "bidirectional-regime-trend":
        body = f'''        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        normal_atr = ((dataframe["atr_ratio"] >= dataframe["atr_median"] * {float(p["atr_low_multiplier"])}) &
                      (dataframe["atr_ratio"] <= dataframe["atr_median"] * {float(p["atr_high_multiplier"])}))
        participation = ((dataframe["volume"] >= dataframe["volume_mean"] * {float(p["volume_multiplier"])}) &
                         (dataframe["volume"] > 0) & normal_atr)
        long_regime = ((dataframe["ema_fast"] > dataframe["ema_slow"]) &
                       (dataframe["ema_slow"] > dataframe["ema_regime"]) &
                       (dataframe["ema_slow"] > dataframe["ema_slow"].shift({int(p["slope_bars"])})))
        short_regime = ((dataframe["ema_fast"] < dataframe["ema_slow"]) &
                        (dataframe["ema_slow"] < dataframe["ema_regime"]) &
                        (dataframe["ema_slow"] < dataframe["ema_slow"].shift({int(p["slope_bars"])})))
        long_recovery = ((dataframe["low"].shift(1) <= dataframe["ema_fast"].shift(1)) &
                         (dataframe["close"] > dataframe["ema_fast"]) &
                         (dataframe["rsi"] > {float(p["rsi_recovery"])}) &
                         (dataframe["rsi"] < {float(p["rsi_recovery"] + p["rsi_band"])}) &
                         (dataframe["close"] > dataframe["open"]))
        short_recovery = ((dataframe["high"].shift(1) >= dataframe["ema_fast"].shift(1)) &
                          (dataframe["close"] < dataframe["ema_fast"]) &
                          (dataframe["rsi"] < {float(100 - p["rsi_recovery"])}) &
                          (dataframe["rsi"] > {float(100 - p["rsi_recovery"] - p["rsi_band"])}) &
                          (dataframe["close"] < dataframe["open"]))
        dataframe.loc[long_regime & long_recovery & participation, "enter_long"] = 1
        dataframe.loc[short_regime & short_recovery & participation, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        atr_kill = dataframe["atr_ratio"] > dataframe["atr_median"] * {float(fixed["atr_kill_multiplier"])}
        dataframe.loc[((dataframe["close"] < dataframe["ema_slow"]) |
                       (dataframe["ema_slow"] < dataframe["ema_regime"]) | atr_kill), "exit_long"] = 1
        dataframe.loc[((dataframe["close"] > dataframe["ema_slow"]) |
                       (dataframe["ema_slow"] > dataframe["ema_regime"]) | atr_kill), "exit_short"] = 1
        return dataframe
'''
    elif family == "bidirectional-volatility-breakout":
        body = f'''        dataframe["range_high"] = dataframe["high"].rolling({int(p["breakout_lookback"])}).max().shift(1)
        dataframe["range_low"] = dataframe["low"].rolling({int(p["breakout_lookback"])}).min().shift(1)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        normal_atr = ((dataframe["atr_ratio"] >= dataframe["atr_median"] * {float(p["atr_low_multiplier"])}) &
                      (dataframe["atr_ratio"] <= dataframe["atr_median"] * {float(p["atr_high_multiplier"])}))
        participation = ((dataframe["volume"] >= dataframe["volume_mean"] * {float(p["volume_multiplier"])}) &
                         (dataframe["volume"] > 0) & normal_atr)
        long_regime = ((dataframe["ema_fast"] > dataframe["ema_slow"]) &
                       (dataframe["ema_slow"] > dataframe["ema_regime"]) &
                       (dataframe["ema_slow"] > dataframe["ema_slow"].shift({int(p["slope_bars"])})))
        short_regime = ((dataframe["ema_fast"] < dataframe["ema_slow"]) &
                        (dataframe["ema_slow"] < dataframe["ema_regime"]) &
                        (dataframe["ema_slow"] < dataframe["ema_slow"].shift({int(p["slope_bars"])})))
        long_breakout = ((dataframe["close"] > dataframe["range_high"]) &
                         (dataframe["close"].shift(1) <= dataframe["range_high"].shift(1)))
        short_breakout = ((dataframe["close"] < dataframe["range_low"]) &
                          (dataframe["close"].shift(1) >= dataframe["range_low"].shift(1)))
        dataframe.loc[long_regime & long_breakout & participation, "enter_long"] = 1
        dataframe.loc[short_regime & short_breakout & participation, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        atr_kill = dataframe["atr_ratio"] > dataframe["atr_median"] * {float(fixed["atr_kill_multiplier"])}
        dataframe.loc[((dataframe["close"] < dataframe["ema_fast"]) |
                       (dataframe["ema_slow"] < dataframe["ema_regime"]) | atr_kill), "exit_long"] = 1
        dataframe.loc[((dataframe["close"] > dataframe["ema_fast"]) |
                       (dataframe["ema_slow"] > dataframe["ema_regime"]) | atr_kill), "exit_short"] = 1
        return dataframe
'''
    elif family == "bidirectional-momentum-continuation":
        body = f'''        bands = ta.BBANDS(dataframe, timeperiod={int(fixed["band_window"])}, nbdevup={float(fixed["band_deviation"])}, nbdevdn={float(fixed["band_deviation"])})
        dataframe["band_width"] = (bands["upperband"] - bands["lowerband"]) / bands["middleband"]
        dataframe["band_median"] = dataframe["band_width"].rolling({int(fixed["band_median_window"])}).median().shift(1)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        normal_atr = ((dataframe["atr_ratio"] >= dataframe["atr_median"] * {float(p["atr_low_multiplier"])}) &
                      (dataframe["atr_ratio"] <= dataframe["atr_median"] * {float(p["atr_high_multiplier"])}))
        participation = ((dataframe["volume"] >= dataframe["volume_mean"] * {float(p["volume_multiplier"])}) &
                         (dataframe["volume"] > 0) & normal_atr &
                         (dataframe["band_width"] >= dataframe["band_median"] * {float(p["bandwidth_multiplier"])}))
        long_regime = ((dataframe["ema_slow"] > dataframe["ema_regime"]) &
                       (dataframe["ema_slow"] > dataframe["ema_slow"].shift({int(p["slope_bars"])})) &
                       (dataframe["adx"] >= {float(p["adx_floor"])}))
        short_regime = ((dataframe["ema_slow"] < dataframe["ema_regime"]) &
                        (dataframe["ema_slow"] < dataframe["ema_slow"].shift({int(p["slope_bars"])})) &
                        (dataframe["adx"] >= {float(p["adx_floor"])}))
        long_recovery = ((dataframe["rsi"] > {float(p["rsi_recovery"])}) &
                         (dataframe["rsi"].shift(1) <= {float(p["rsi_recovery"])}) &
                         (dataframe["rsi"] < {float(p["rsi_recovery"] + p["rsi_band"])}) &
                         (dataframe["close"] > dataframe["ema_fast"]) &
                         (dataframe["close"] > dataframe["close"].shift(1)))
        short_recovery = ((dataframe["rsi"] < {float(100 - p["rsi_recovery"])}) &
                          (dataframe["rsi"].shift(1) >= {float(100 - p["rsi_recovery"])}) &
                          (dataframe["rsi"] > {float(100 - p["rsi_recovery"] - p["rsi_band"])}) &
                          (dataframe["close"] < dataframe["ema_fast"]) &
                          (dataframe["close"] < dataframe["close"].shift(1)))
        dataframe.loc[long_regime & long_recovery & participation, "enter_long"] = 1
        dataframe.loc[short_regime & short_recovery & participation, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        atr_kill = dataframe["atr_ratio"] > dataframe["atr_median"] * {float(fixed["atr_kill_multiplier"])}
        dataframe.loc[((dataframe["close"] < dataframe["ema_slow"]) |
                       (dataframe["ema_slow"] < dataframe["ema_regime"]) |
                       ((dataframe["adx"] < {float(fixed["momentum_kill_adx"])}) & (dataframe["rsi"] < {float(50 - fixed["momentum_kill_rsi_band"])})) | atr_kill), "exit_long"] = 1
        dataframe.loc[((dataframe["close"] > dataframe["ema_slow"]) |
                       (dataframe["ema_slow"] > dataframe["ema_regime"]) |
                       ((dataframe["adx"] < {float(fixed["momentum_kill_adx"])}) & (dataframe["rsi"] > {float(50 + fixed["momentum_kill_rsi_band"])})) | atr_kill), "exit_short"] = 1
        return dataframe
'''
    elif family == "trend-pullback":
        body = f'''        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        regime = ((dataframe["ema_slow"] > dataframe["ema_regime"]) &
                  (dataframe["ema_slow"] > dataframe["ema_slow"].shift({int(p["slope_bars"])})) &
                  (dataframe["close"] > dataframe["ema_regime"]))
        recovery = ((dataframe["low"].shift(1) <= dataframe["ema_fast"].shift(1)) &
                    (dataframe["close"] > dataframe["ema_fast"]) &
                    (dataframe["close"] > dataframe["open"]))
        normal_atr = ((dataframe["atr_ratio"] >= dataframe["atr_median"] * {float(p["atr_low_multiplier"])}) &
                      (dataframe["atr_ratio"] <= dataframe["atr_median"] * {float(p["atr_high_multiplier"])}))
        entry = (regime & recovery & dataframe["rsi"].between({float(p["rsi_low"])}, {float(p["rsi_high"])}) &
                 normal_atr & (dataframe["volume"] >= dataframe["volume_mean"] * {float(p["volume_multiplier"])}) &
                 (dataframe["volume"] > 0))
        dataframe.loc[entry, "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        exit_long = ((dataframe["close"] < dataframe["ema_slow"]) |
                     (dataframe["ema_slow"] < dataframe["ema_regime"]) |
                     (dataframe["atr_ratio"] > dataframe["atr_median"] * 2.5) |
                     (dataframe["rsi"] > 78.0))
        dataframe.loc[exit_long, "exit_long"] = 1
        return dataframe
'''
    elif family == "volatility-breakout":
        body = f'''        dataframe["range_high"] = dataframe["high"].rolling({int(p["breakout_lookback"])}).max().shift(1)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        regime = ((dataframe["ema_fast"] > dataframe["ema_slow"]) &
                  (dataframe["ema_slow"] > dataframe["ema_regime"]) &
                  (dataframe["ema_slow"] > dataframe["ema_slow"].shift({int(p["slope_bars"])})))
        breakout = ((dataframe["close"] > dataframe["range_high"]) &
                    (dataframe["close"].shift(1) <= dataframe["range_high"].shift(1)) &
                    (dataframe["close"] > dataframe["open"]))
        normal_atr = ((dataframe["atr_ratio"] >= dataframe["atr_median"] * {float(p["atr_low_multiplier"])}) &
                      (dataframe["atr_ratio"] <= dataframe["atr_median"] * {float(p["atr_high_multiplier"])}))
        entry = (regime & breakout & normal_atr &
                 (dataframe["volume"] >= dataframe["volume_mean"] * {float(p["volume_multiplier"])}) &
                 (dataframe["volume"] > 0))
        dataframe.loc[entry, "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        exit_long = ((dataframe["close"] < dataframe["ema_fast"]) |
                     (dataframe["ema_slow"] < dataframe["ema_regime"]) |
                     (dataframe["atr_ratio"] > dataframe["atr_median"] * 2.5))
        dataframe.loc[exit_long, "exit_long"] = 1
        return dataframe
'''
    elif family == "regime-momentum":
        body = f'''        bands = ta.BBANDS(dataframe, timeperiod=32, nbdevup=2.0, nbdevdn=2.0)
        dataframe["band_width"] = (bands["upperband"] - bands["lowerband"]) / bands["middleband"]
        dataframe["band_median"] = dataframe["band_width"].rolling(96).median().shift(1)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        regime = ((dataframe["ema_slow"] > dataframe["ema_regime"]) &
                  (dataframe["ema_slow"] > dataframe["ema_slow"].shift({int(p["slope_bars"])})) &
                  (dataframe["adx"] >= {float(p["adx_floor"])}) &
                  (dataframe["band_width"] >= dataframe["band_median"] * {float(p["bandwidth_multiplier"])}))
        recovery = ((dataframe["rsi"] > {float(p["rsi_recovery"])}) &
                    (dataframe["rsi"].shift(1) <= {float(p["rsi_recovery"])}) &
                    (dataframe["rsi"] < {float(p["rsi_cap"])}) &
                    (dataframe["close"] > dataframe["ema_fast"]) &
                    (dataframe["close"] > dataframe["close"].shift(1)))
        normal_atr = ((dataframe["atr_ratio"] >= dataframe["atr_median"] * {float(p["atr_low_multiplier"])}) &
                      (dataframe["atr_ratio"] <= dataframe["atr_median"] * {float(p["atr_high_multiplier"])}))
        entry = (regime & recovery & normal_atr &
                 (dataframe["volume"] >= dataframe["volume_mean"] * {float(p["volume_multiplier"])}) &
                 (dataframe["volume"] > 0))
        dataframe.loc[entry, "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        exit_long = ((dataframe["close"] < dataframe["ema_slow"]) |
                     (dataframe["ema_slow"] < dataframe["ema_regime"]) |
                     ((dataframe["adx"] < 14.0) & (dataframe["rsi"] < 44.0)) |
                     (dataframe["atr_ratio"] > dataframe["atr_median"] * 2.5))
        dataframe.loc[exit_long, "exit_long"] = 1
        return dataframe
'''
    else:
        raise Blocked("unknown family")
    direction_specific_time_stop = (
        '"short_condition_bar_time_stop" if getattr(trade, "is_short", False) '
        'else "long_condition_bar_time_stop"'
        if bidirectional
        else '"condition_bar_time_stop"'
    )
    custom_exit = f'''
    def custom_exit(self, pair, trade, current_time, current_rate, current_profit, **kwargs):
        if (current_time - trade.open_date_utc).total_seconds() >= {int(p["max_holding_bars"]) * 900}:
            return {direction_specific_time_stop}
        return None
'''
    return cls, common + body + custom_exit


def read_result(result_dir: Path, strategy_class: str) -> dict[str, object]:
    archives = list(result_dir.glob("*.zip"))
    if len(archives) != 1:
        raise Blocked(
            "backtest archive is ambiguous:"
            + ",".join(sorted(path.name for path in result_dir.iterdir()))
        )
    with zipfile.ZipFile(archives[0]) as archive:
        names = [name for name in archive.namelist() if Path(name).name.startswith("backtest-result-") and name.endswith(".json") and not name.endswith(".meta.json") and not name.endswith("_config.json")]
        if len(names) != 1:
            raise Blocked("backtest payload is ambiguous")
        payload = json.loads(archive.read(names[0]))
    result = payload.get("strategy", {}).get(strategy_class)
    if not isinstance(result, dict):
        raise Blocked("strategy result is absent")
    return result


def run_window(strategy_class: str, strategy_path: Path, key: str, start: datetime, end: datetime, metadata: Path, fee: float) -> dict[str, object]:
    result_dir = Path("/work/results") / f"{strategy_class}-{key}"
    result_dir.mkdir(parents=True, exist_ok=False)
    command = (
        "/opt/freqtrade-ai/bin/canonical-v13-research-worker", "freqtrade-offline",
        "--metadata", str(metadata), "backtesting", "--config", "/work/config.json",
        "--datadir", "/work/data", "--strategy-path", str(strategy_path.parent),
        "--strategy", strategy_class, "--userdir", "/work/user_data",
        "--pairs", "BTC/USDT:USDT", "--timeframe", "15m", "--timerange", timerange(start, end),
        "--fee", str(fee), "--cache", "none", "--export", "trades",
        "--backtest-directory", str(result_dir), "--no-color",
    )
    log_path = result_dir / "freqtrade.stderr"
    with log_path.open("wb") as log:
        completed = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, env=SUBPROCESS_ENV, close_fds=True, timeout=840, check=False)
    if completed.returncode != 0:
        detail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
        raise Blocked(f"backtest failed for {strategy_class}:{key}:{detail}")
    if not list(result_dir.glob("*.zip")):
        detail = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
        if "No trades made" in detail:
            return {
                "total_trades": 0,
                "profit_total": 0.0,
                "max_drawdown_account": 0.0,
                "trades": [],
            }
        raise Blocked(f"backtest produced no archive for {strategy_class}:{key}:{detail}")
    return read_result(result_dir, strategy_class)


def directional_drawdown(profits: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    for profit in profits:
        equity += profit
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


def direction_metrics(
    trades: list[dict[str, object]], *, is_short: bool, fee: float, slippage: float
) -> dict[str, object]:
    profits = [
        float(row.get("profit_ratio", 0.0))
        for row in trades
        if bool(row.get("is_short", False)) is is_short
    ]
    trade_count = len(profits)
    fee_inclusive_return = sum(profits)
    modeled_fee_cost = 2.0 * fee * trade_count
    modeled_slippage_cost = 2.0 * slippage * trade_count
    positive = [value for value in profits if value > 0]
    negative = [value for value in profits if value < 0]
    values = {
        "trade_count": trade_count,
        "gross_return_before_cost": fee_inclusive_return + modeled_fee_cost,
        "fee_inclusive_return": fee_inclusive_return,
        "modeled_fee_cost": modeled_fee_cost,
        "modeled_slippage_cost": modeled_slippage_cost,
        "net_return_after_cost": fee_inclusive_return - modeled_slippage_cost,
        "win_count": len(positive),
        "loss_count": len(negative),
        "top_winning_trade_profit_share": (
            max(positive) / sum(positive)
            if positive and sum(positive) > 0
            else 1.0
        ),
        "loss_return_share": (
            abs(min(negative)) / abs(sum(negative))
            if negative and sum(negative) < 0
            else 0.0
        ),
        "maximum_drawdown_contribution": directional_drawdown(profits),
    }
    if not all(math.isfinite(float(value)) for value in values.values()):
        raise Blocked("non-finite directional metrics")
    return values


def metrics(
    result: dict[str, object],
    fee: float,
    slippage: float,
    sensitivity_fee: float,
    sensitivity_slippage: float,
    *,
    include_directional: bool = False,
) -> dict[str, object]:
    trades = result.get("trades") if isinstance(result.get("trades"), list) else []
    typed_trades = [row for row in trades if isinstance(row, dict)]
    profits = [float(row.get("profit_ratio", 0.0)) for row in typed_trades]
    trade_count = int(result.get("total_trades", len(profits)))
    raw = float(result.get("profit_total", float(result.get("profit_total_pct", 0.0)) / 100.0))
    net = raw - 2.0 * slippage * trade_count
    sensitivity = net - 2.0 * ((sensitivity_fee - fee) + (sensitivity_slippage - slippage)) * trade_count
    positive = [value for value in profits if value > 0]
    concentration = max(positive) / sum(positive) if positive and sum(positive) > 0 else 1.0
    mean = sum(profits) / len(profits) if profits else 0.0
    variance = sum((value - mean) ** 2 for value in profits) / len(profits) if profits else 0.0
    values = {
        "trade_count": trade_count,
        "raw_return": raw,
        "net_return_after_cost": net,
        "sensitivity_net_after_cost": sensitivity,
        "maximum_drawdown": float(result.get("max_drawdown_account", result.get("max_drawdown", 0.0))),
        "top_trade_profit_share": concentration,
        "return_stability": 1.0 / (1.0 + math.sqrt(variance)),
        "fee_rate": fee,
        "slippage_rate": slippage,
    }
    if not all(math.isfinite(float(value)) for value in values.values()):
        raise Blocked("non-finite metrics")
    if include_directional:
        directions = {
            "long": direction_metrics(
                typed_trades, is_short=False, fee=fee, slippage=slippage
            ),
            "short": direction_metrics(
                typed_trades, is_short=True, fee=fee, slippage=slippage
            ),
        }
        directional_net = sum(
            float(item["net_return_after_cost"]) for item in directions.values()
        )
        values["direction_attribution"] = directions
        values["directional_net_reconciliation_error"] = abs(net - directional_net)
    return values


def evaluate(plan: dict[str, object], market: Path, metadata: Path) -> dict[str, object]:
    if plan.get("contract") != "canonical-v13-cost-aware-oos-optimization-plan-v1":
        raise Blocked("plan contract drifted")
    if sorted(path.name for path in Path("/sys/class/net").iterdir()) != ["lo"]:
        raise Blocked("network-none invariant failed")
    Path("/work/home").mkdir(parents=True, exist_ok=True)
    Path("/work/user_data").mkdir(parents=True, exist_ok=True)
    Path("/work/strategies").mkdir(parents=True, exist_ok=True)
    Path("/work/results").mkdir(parents=True, exist_ok=True)
    frame = prepare_data(market)
    directionality = plan.get("directionality", {})
    bidirectional = (
        isinstance(directionality, dict)
        and directionality.get("can_short") is True
    )
    position_sizing = plan.get("position_sizing", {}) if bidirectional else {}
    fixed_contract = plan.get("fixed_strategy_contract") if bidirectional else None
    if bidirectional and (
        not isinstance(position_sizing, dict)
        or float(position_sizing.get("stake_amount_quote", 0)) <= 0
        or float(position_sizing.get("dry_run_wallet_quote", 0)) <= 0
        or int(position_sizing.get("maximum_open_trades", 0)) != 1
        or position_sizing.get("position_adjustment") is not False
        or float(position_sizing.get("stake_amount_quote", 0))
        / float(position_sizing.get("dry_run_wallet_quote", 1))
        > float(position_sizing.get("maximum_nominal_wallet_fraction", 0))
    ):
        raise Blocked("bidirectional position sizing contract is invalid")
    if bidirectional and not isinstance(fixed_contract, dict):
        raise Blocked("bidirectional fixed strategy contract is invalid")
    config = {
        "dry_run": True,
        "dry_run_wallet": float(position_sizing.get("dry_run_wallet_quote", 10000)),
        "stake_currency": "USDT",
        "stake_amount": float(position_sizing.get("stake_amount_quote", 100)),
        "max_open_trades": int(position_sizing.get("maximum_open_trades", 1)),
        "timeframe": "15m",
        "trading_mode": "futures", "margin_mode": "isolated",
        "exchange": {"name": "okx", "pair_whitelist": ["BTC/USDT:USDT"], "enable_ws": False},
        "pairlists": [{"method": "StaticPairList"}],
    }
    Path("/work/config.json").write_text(json.dumps(config, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    isolation = plan["data_isolation"]
    costs = plan["costs"]
    if not isinstance(isolation, dict) or not isinstance(costs, dict):
        raise Blocked("plan costs/windows are invalid")
    windows = {key: (iso(isolation[key]["start_at"]), iso(isolation[key]["end_at"])) for key in ("train", "validation")}
    if not (frame["date"].min().to_pydatetime() <= windows["train"][0] and frame["date"].max().to_pydatetime() >= windows["validation"][1]):
        raise Blocked("market coverage is insufficient")
    fee = float(costs["qualification_fee_rate"])
    slippage = float(costs["qualification_slippage_rate"])
    sensitivity_fee = float(costs["sensitivity_fee_rate"])
    sensitivity_slippage = float(costs["sensitivity_slippage_rate"])
    objective_contract = plan.get("objective")
    if not isinstance(objective_contract, dict):
        raise Blocked("plan objective is invalid")
    selection_thresholds = objective_contract.get("selection_thresholds", {})
    required_selection_keys = {
        "minimum_total_trades_30d",
        "maximum_train_validation_drawdown",
        "maximum_total_top_trade_profit_share",
    }
    if bidirectional and (
        not isinstance(selection_thresholds, dict)
        or not required_selection_keys.issubset(selection_thresholds)
    ):
        raise Blocked("bidirectional selection thresholds are invalid")
    minimum_total_trades = int(selection_thresholds.get("minimum_total_trades_30d", 30))
    maximum_drawdown = float(selection_thresholds.get("maximum_train_validation_drawdown", 0.15))
    maximum_concentration = float(selection_thresholds.get("maximum_total_top_trade_profit_share", 0.35))
    rows = []
    for trial in sample_trials(plan):
        number = int(trial["trial_number"])
        family = str(trial["family_key"])
        parameters = dict(trial["parameters"])
        cls, source = render_strategy(
            family,
            number,
            parameters,
            fixed_contract if isinstance(fixed_contract, dict) else None,
        )
        source_path = Path("/work/strategies") / f"{cls}.py"
        source_path.write_text(source, encoding="utf-8")
        observed = {}
        for key, (start, end) in windows.items():
            observed[key] = metrics(
                run_window(cls, source_path, key, start, end, metadata, fee),
                fee,
                slippage,
                sensitivity_fee,
                sensitivity_slippage,
                include_directional=bidirectional,
            )
        train = observed["train"]
        validation = observed["validation"]
        scaled_train_trades = float(train["trade_count"]) / 4.0
        drift = abs(float(train["net_return_after_cost"]) / 4.0 - float(validation["net_return_after_cost"]))
        directional_eligible = True
        directional_imbalance = 0.0
        worst_validation_direction_net = float(validation["net_return_after_cost"])
        if bidirectional:
            minimum_directional_trades = int(
                directionality["minimum_directional_trades_30d"]
            )
            minimum_directional_net = float(
                directionality["minimum_directional_net_after_cost"]
            )
            maximum_directional_concentration = float(
                directionality["maximum_directional_top_trade_share"]
            )
            train_directions = train["direction_attribution"]
            validation_directions = validation["direction_attribution"]
            directional_eligible = all(
                float(train_directions[direction]["trade_count"]) / 4.0
                >= minimum_directional_trades
                and int(validation_directions[direction]["trade_count"])
                >= minimum_directional_trades
                and float(train_directions[direction]["net_return_after_cost"])
                >= minimum_directional_net
                and float(validation_directions[direction]["net_return_after_cost"])
                >= minimum_directional_net
                and float(
                    validation_directions[direction][
                        "top_winning_trade_profit_share"
                    ]
                )
                <= maximum_directional_concentration
                for direction in ("long", "short")
            )
            validation_direction_nets = [
                float(validation_directions[direction]["net_return_after_cost"])
                for direction in ("long", "short")
            ]
            directional_imbalance = abs(
                validation_direction_nets[0] - validation_direction_nets[1]
            )
            worst_validation_direction_net = min(validation_direction_nets)
        eligible = (
            float(train["net_return_after_cost"]) > 0
            and float(validation["net_return_after_cost"]) > 0
            and scaled_train_trades >= minimum_total_trades
            and int(validation["trade_count"]) >= minimum_total_trades
            and float(train["maximum_drawdown"]) <= maximum_drawdown
            and float(validation["maximum_drawdown"]) <= maximum_drawdown
            and float(validation["top_trade_profit_share"])
            <= maximum_concentration
            and directional_eligible
        )
        penalties = plan["objective"]["penalties"]
        turnover_penalty = (
            float(penalties["turnover"])
            * 2.0
            * (fee + slippage)
            * int(validation["trade_count"])
        )
        low_trade_penalty = (
            float(penalties["low_trade_count"])
            * (
                max(0.0, float(minimum_total_trades) - scaled_train_trades)
                + max(
                    0.0,
                    float(minimum_total_trades)
                    - float(validation["trade_count"]),
                )
            )
            / float(minimum_total_trades)
            * 0.01
        )
        objective = min(
            float(train["net_return_after_cost"]) / 4.0,
            float(validation["net_return_after_cost"]),
            worst_validation_direction_net,
        ) - float(penalties["train_validation_drift"]) * drift - float(penalties.get("directional_net_imbalance", 0.0)) * directional_imbalance - float(penalties["maximum_drawdown"]) * float(validation["maximum_drawdown"]) - max(0.0, float(validation["top_trade_profit_share"]) - maximum_concentration) * float(penalties["profit_concentration"]) - turnover_penalty - low_trade_penalty
        rows.append({
            "trial_number": number, "family_key": family, "parameters_json": parameters,
            "metrics_json": {"train": train, "validation": validation, "scaled_train_trade_count_30d": scaled_train_trades, "train_validation_drift": drift, "directional_net_imbalance": directional_imbalance, "worst_validation_direction_net_after_cost": worst_validation_direction_net, "turnover_penalty": turnover_penalty, "low_trade_count_penalty": low_trade_penalty, "objective": objective, "eligible": eligible, "rule_complexity": 6 if family in {"trend-pullback", "bidirectional-regime-trend"} else 7},
            "strategy_class": cls, "strategy_source": source, "strategy_source_digest": sha256(source.encode()).hexdigest(),
        })
    eligible_rows = [row for row in rows if row["metrics_json"]["eligible"]]
    ranked = sorted(eligible_rows, key=lambda row: (-float(row["metrics_json"]["validation"]["net_return_after_cost"]), -float(row["metrics_json"]["worst_validation_direction_net_after_cost"]), -float(row["metrics_json"]["validation"]["sensitivity_net_after_cost"]), float(row["metrics_json"]["train_validation_drift"]), float(row["metrics_json"]["directional_net_imbalance"]), float(row["metrics_json"]["validation"]["maximum_drawdown"]), int(row["metrics_json"]["rule_complexity"]), int(row["trial_number"])))
    finalists = []
    used_families: set[str] = set()
    for row in ranked:
        if row["family_key"] in used_families:
            continue
        finalists.append(int(row["trial_number"]))
        used_families.add(str(row["family_key"]))
        if len(finalists) == int(plan["execution"]["finalist_limit"]):
            break
    evidence_rows = [{"trial_number": row["trial_number"], "parameters_json": row["parameters_json"], "metrics_json": row["metrics_json"]} for row in rows]
    return {"contract": CONTRACT, "plan_digest": canonical_digest(plan), "market_digest": sha256(market.read_bytes()).hexdigest(), "trial_count": len(rows), "selected_trial_numbers": finalists, "trial_evidence_digest": canonical_digest(evidence_rows), "trials": rows, "execution_side_effects": 0, "credential_access": "NONE", "trading_capability": "TRADING_DISABLED"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--market", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(load_json(args.plan), args.market, args.metadata)
    args.output.write_text(json.dumps(result, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
