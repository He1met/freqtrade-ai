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

try:
    from freqtrade.data.history.datahandlers.featherdatahandler import FeatherDataHandler
    from freqtrade.enums import CandleType
except ModuleNotFoundError:  # Pure accounting/integrity tests do not install Freqtrade.
    FeatherDataHandler = None  # type: ignore[assignment,misc]
    CandleType = None  # type: ignore[assignment,misc]


CONTRACT = "canonical-v13-cost-aware-oos-optimization-result-v1"
TIMEFRAME_SECONDS = 15 * 60
RECONCILIATION_TOLERANCE = 1e-10
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


def load_market_frame(market: Path) -> pd.DataFrame:
    rows = []
    with market.open("r", encoding="utf-8") as stream:
        for line in stream:
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise Blocked("market row is invalid")
                opened_at = datetime.fromisoformat(
                    str(row["opened_at"]).replace("Z", "+00:00")
                )
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise Blocked("market timestamp is invalid") from exc
            if (
                opened_at.tzinfo is None
                or opened_at.utcoffset() != timezone.utc.utcoffset(opened_at)
            ):
                raise Blocked("market timestamp must be UTC")
            if int(opened_at.timestamp()) % TIMEFRAME_SECONDS != 0:
                raise Blocked("market timestamp must align to a 900-second boundary")
            rows.append(row)
    try:
        frame = pd.DataFrame(
            {
                "date": pd.to_datetime([row["opened_at"] for row in rows], utc=True),
                "open": [float(row["open"]) for row in rows],
                "high": [float(row["high"]) for row in rows],
                "low": [float(row["low"]) for row in rows],
                "close": [float(row["close"]) for row in rows],
                "volume": [float(row["volume"]) for row in rows],
            }
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise Blocked("market numeric fields are invalid") from exc
    if frame.empty or frame["date"].duplicated().any() or not frame["date"].is_monotonic_increasing:
        raise Blocked("market continuity is invalid")
    numeric = frame[["open", "high", "low", "close", "volume"]]
    if not numeric.map(math.isfinite).all().all():
        raise Blocked("market contains non-finite values")
    if not (frame[["open", "high", "low", "close"]] > 0).all().all():
        raise Blocked("market prices must be positive")
    if not (frame["volume"] >= 0).all():
        raise Blocked("market volume must be nonnegative")
    if not (
        (frame["low"] <= frame[["open", "close"]].min(axis=1))
        & (frame["high"] >= frame[["open", "close"]].max(axis=1))
        & (frame["low"] <= frame["high"])
    ).all():
        raise Blocked("market OHLC relationship is invalid")
    deltas = frame["date"].diff().dropna().dt.total_seconds()
    if not (deltas == TIMEFRAME_SECONDS).all():
        raise Blocked("market must be strictly continuous at 900 seconds")
    return frame


def window_frame(frame: pd.DataFrame, *, start: datetime, end: datetime) -> pd.DataFrame:
    if end <= start:
        raise Blocked("window interval is invalid")
    visible = frame.loc[frame["date"] < end].copy()
    if visible.empty or visible["date"].max().to_pydatetime() != end - pd.Timedelta(
        seconds=TIMEFRAME_SECONDS
    ):
        raise Blocked("window end-exclusive boundary is not closed-candle complete")
    if visible["date"].min().to_pydatetime() > start:
        raise Blocked("window warmup coverage is insufficient")
    return visible


def store_window_data(
    frame: pd.DataFrame,
    *,
    start: datetime,
    end: datetime,
    key: str,
    pair: str,
) -> Path:
    if FeatherDataHandler is None or CandleType is None:
        raise Blocked("Freqtrade data handler is unavailable")
    visible = window_frame(frame, start=start, end=end)
    data_root = Path("/work/window-data") / key
    (data_root / "futures").mkdir(parents=True, exist_ok=True)
    handler = FeatherDataHandler(data_root)
    handler.ohlcv_store(pair, "15m", visible, CandleType.FUTURES)
    return data_root


def directory_digest(root: Path) -> str:
    evidence = [
        {
            "path": str(path.relative_to(root)),
            "digest": sha256(path.read_bytes()).hexdigest(),
        }
        for path in sorted(root.rglob("*"))
        if path.is_file()
    ]
    if not evidence:
        raise Blocked("window data artifact is empty")
    return canonical_digest(evidence)


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


def backtest_command(
    *,
    strategy_class: str,
    strategy_path: Path,
    data_root: Path,
    result_dir: Path,
    start: datetime,
    end: datetime,
    metadata: Path,
    fee: float,
    pair: str,
) -> tuple[str, ...]:
    return (
        "/opt/freqtrade-ai/bin/canonical-v13-research-worker", "freqtrade-offline",
        "--metadata", str(metadata), "backtesting", "--config", "/work/config.json",
        "--datadir", str(data_root), "--strategy-path", str(strategy_path.parent),
        "--strategy", strategy_class, "--userdir", "/work/user_data",
        "--pairs", pair, "--timeframe", "15m", "--timerange", timerange(start, end),
        "--fee", str(fee), "--cache", "none", "--export", "trades",
        "--enable-protections",
        "--backtest-directory", str(result_dir), "--no-color",
    )


def run_window(
    strategy_class: str,
    strategy_path: Path,
    key: str,
    start: datetime,
    end: datetime,
    metadata: Path,
    fee: float,
    *,
    data_root: Path,
    wallet: float,
    pair: str,
) -> dict[str, object]:
    result_dir = Path("/work/results") / f"{strategy_class}-{key}"
    result_dir.mkdir(parents=True, exist_ok=False)
    command = backtest_command(
        strategy_class=strategy_class,
        strategy_path=strategy_path,
        data_root=data_root,
        result_dir=result_dir,
        start=start,
        end=end,
        metadata=metadata,
        fee=fee,
        pair=pair,
    )
    command_evidence = {
        "command_digest": canonical_digest(list(command)),
        "protections_enabled": "--enable-protections" in command,
        "window_end_exclusive": end.isoformat(),
        "window_data_digest": directory_digest(data_root),
        "last_visible_candle": (
            end - pd.Timedelta(seconds=TIMEFRAME_SECONDS)
        ).isoformat(),
    }
    if command_evidence["protections_enabled"] is not True:
        raise Blocked("backtest protections are not enabled")
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
                "starting_balance": wallet,
                "max_drawdown_account": 0.0,
                "trades": [],
                "canonical_execution_evidence": command_evidence,
            }
        raise Blocked(f"backtest produced no archive for {strategy_class}:{key}:{detail}")
    result = read_result(result_dir, strategy_class)
    result["canonical_execution_evidence"] = command_evidence
    return result


def directional_drawdown(profits: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    for profit in profits:
        equity += profit
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


def finite_positive(value: object, *, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise Blocked(f"{field} must be numeric") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise Blocked(f"{field} must be finite and positive")
    return parsed


def finite_nonnegative(value: object, *, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise Blocked(f"{field} must be numeric") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise Blocked(f"{field} must be finite and nonnegative")
    return parsed


def trade_accounting(
    trade: dict[str, object],
    *,
    wallet: float,
    fee: float,
    slippage: float,
    stake_limit: float,
    leverage_limit: float,
) -> dict[str, float | bool]:
    amount = finite_positive(trade.get("amount"), field="trade amount")
    open_rate = finite_positive(trade.get("open_rate"), field="trade open_rate")
    close_rate = finite_positive(trade.get("close_rate"), field="trade close_rate")
    stake_amount = finite_positive(trade.get("stake_amount"), field="trade stake_amount")
    leverage = finite_positive(trade.get("leverage"), field="trade leverage")
    if stake_amount > stake_limit * (1.0 + 1e-6):
        raise Blocked("trade stake exceeds the digest-bound sizing contract")
    if leverage > leverage_limit * (1.0 + 1e-9):
        raise Blocked("trade leverage exceeds the digest-bound target")
    entry_notional = amount * open_rate
    declared_notional = stake_amount * leverage
    if not math.isclose(
        entry_notional,
        declared_notional,
        rel_tol=1e-6,
        abs_tol=1e-6,
    ):
        raise Blocked("trade amount does not reconcile with stake and leverage")
    for key in ("fee_open", "fee_close"):
        observed_fee = finite_nonnegative(
            trade.get(key), field=f"trade {key}"
        )
        if not math.isfinite(observed_fee) or not math.isclose(
            observed_fee, fee, rel_tol=0.0, abs_tol=1e-12
        ):
            raise Blocked("trade fee does not match the frozen fee contract")
    profit_abs = float(trade.get("profit_abs", math.nan))
    funding_abs = float(trade.get("funding_fees", 0.0) or 0.0)
    if not math.isfinite(profit_abs) or not math.isfinite(funding_abs):
        raise Blocked("trade PnL fields must be finite")
    turnover_abs = amount * (open_rate + close_rate)
    modeled_fee_abs = fee * turnover_abs
    modeled_slippage_abs = slippage * turnover_abs
    if not isinstance(trade.get("is_short"), bool):
        raise Blocked("trade direction must be an explicit boolean")
    is_short = bool(trade["is_short"])
    price_pnl_abs = amount * (
        open_rate - close_rate if is_short else close_rate - open_rate
    )
    expected_profit_abs = price_pnl_abs - modeled_fee_abs + funding_abs
    if not math.isclose(
        profit_abs,
        expected_profit_abs,
        rel_tol=0.0,
        abs_tol=max(RECONCILIATION_TOLERANCE * wallet, 1e-7),
    ):
        raise Blocked("trade profit_abs does not reconcile with price, fee, and funding")
    return {
        "is_short": is_short,
        "profit_account": profit_abs / wallet,
        "price_pnl_account": price_pnl_abs / wallet,
        "modeled_fee_account": modeled_fee_abs / wallet,
        "modeled_slippage_account": modeled_slippage_abs / wallet,
        "turnover_account": turnover_abs / wallet,
    }


def direction_metrics(
    accounted_trades: list[dict[str, float | bool]], *, is_short: bool
) -> dict[str, object]:
    selected = [row for row in accounted_trades if bool(row["is_short"]) is is_short]
    profits = [float(row["profit_account"]) for row in selected]
    trade_count = len(profits)
    fee_inclusive_return = sum(profits)
    modeled_fee_cost = sum(float(row["modeled_fee_account"]) for row in selected)
    modeled_slippage_cost = sum(
        float(row["modeled_slippage_account"]) for row in selected
    )
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
        "win_rate": len(positive) / trade_count if trade_count else 0.0,
        "average_profit_account": (
            fee_inclusive_return / trade_count if trade_count else 0.0
        ),
        "median_profit_account": (
            float(pd.Series(profits).median()) if trade_count else 0.0
        ),
        "turnover_account": sum(
            float(row["turnover_account"]) for row in selected
        ),
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
    wallet: float,
    stake_limit: float,
    leverage_limit: float,
    sizing_digest: str,
    include_directional: bool = False,
) -> dict[str, object]:
    wallet = finite_positive(wallet, field="wallet")
    stake_limit = finite_positive(stake_limit, field="stake limit")
    leverage_limit = finite_positive(leverage_limit, field="leverage limit")
    fee = finite_nonnegative(fee, field="fee")
    slippage = finite_nonnegative(slippage, field="slippage")
    sensitivity_fee = finite_nonnegative(
        sensitivity_fee, field="sensitivity fee"
    )
    sensitivity_slippage = finite_nonnegative(
        sensitivity_slippage, field="sensitivity slippage"
    )
    if sensitivity_fee < fee or sensitivity_slippage < slippage:
        raise Blocked("sensitivity costs must not be lower than qualification costs")
    execution_evidence = result.get("canonical_execution_evidence")
    if (
        not isinstance(execution_evidence, dict)
        or execution_evidence.get("protections_enabled") is not True
        or not execution_evidence.get("command_digest")
        or not execution_evidence.get("window_data_digest")
    ):
        raise Blocked("backtest protections and window evidence are required")
    trades = result.get("trades") if isinstance(result.get("trades"), list) else []
    typed_trades = [row for row in trades if isinstance(row, dict)]
    trade_count = int(result.get("total_trades", len(typed_trades)))
    if trade_count != len(typed_trades):
        raise Blocked("trade count does not match exported trades")
    starting_balance = finite_positive(
        result.get("starting_balance"), field="backtest starting_balance"
    )
    if not math.isclose(starting_balance, wallet, rel_tol=0.0, abs_tol=1e-9):
        raise Blocked("backtest wallet does not match the digest-bound sizing contract")
    accounted = [
        trade_accounting(
            row,
            wallet=wallet,
            fee=fee,
            slippage=slippage,
            stake_limit=stake_limit,
            leverage_limit=leverage_limit,
        )
        for row in typed_trades
    ]
    profits = [float(row["profit_account"]) for row in accounted]
    raw = sum(profits)
    engine_raw = float(
        result.get("profit_total", float(result.get("profit_total_pct", 0.0)) / 100.0)
    )
    if not math.isclose(
        raw, engine_raw, rel_tol=0.0, abs_tol=RECONCILIATION_TOLERANCE
    ):
        raise Blocked("profit_abs account return does not reconcile with profit_total")
    turnover = sum(float(row["turnover_account"]) for row in accounted)
    modeled_fee_cost = sum(float(row["modeled_fee_account"]) for row in accounted)
    modeled_slippage_cost = sum(
        float(row["modeled_slippage_account"]) for row in accounted
    )
    net = raw - modeled_slippage_cost
    sensitivity = net - (
        (sensitivity_fee - fee) + (sensitivity_slippage - slippage)
    ) * turnover
    positive = [value for value in profits if value > 0]
    concentration = max(positive) / sum(positive) if positive and sum(positive) > 0 else 1.0
    mean = sum(profits) / len(profits) if profits else 0.0
    variance = sum((value - mean) ** 2 for value in profits) / len(profits) if profits else 0.0
    values = {
        "trade_count": trade_count,
        "raw_return": raw,
        "fee_inclusive_return": raw,
        "turnover_account": turnover,
        "modeled_fee_cost": modeled_fee_cost,
        "modeled_slippage_cost": modeled_slippage_cost,
        "net_return_after_cost": net,
        "positive_profit_account": sum(positive),
        "largest_winning_trade_account": max(positive) if positive else 0.0,
        "sensitivity_net_after_cost": sensitivity,
        "maximum_drawdown": float(result.get("max_drawdown_account", result.get("max_drawdown", 0.0))),
        "top_trade_profit_share": concentration,
        "return_stability": 1.0 / (1.0 + math.sqrt(variance)),
        "fee_rate": fee,
        "slippage_rate": slippage,
        "wallet": wallet,
        "stake_limit": stake_limit,
        "leverage_limit": leverage_limit,
        "sizing_digest": sizing_digest,
        "execution_evidence": execution_evidence,
    }
    numeric_metric_keys = {
        "trade_count",
        "raw_return",
        "fee_inclusive_return",
        "turnover_account",
        "modeled_fee_cost",
        "modeled_slippage_cost",
        "net_return_after_cost",
        "sensitivity_net_after_cost",
        "maximum_drawdown",
        "top_trade_profit_share",
        "return_stability",
        "fee_rate",
        "slippage_rate",
        "wallet",
        "stake_limit",
        "leverage_limit",
        "positive_profit_account",
        "largest_winning_trade_account",
    }
    if not all(math.isfinite(float(values[key])) for key in numeric_metric_keys):
        raise Blocked("non-finite metrics")
    if include_directional:
        directions = {
            "long": direction_metrics(
                accounted, is_short=False
            ),
            "short": direction_metrics(
                accounted, is_short=True
            ),
        }
        directional_net = sum(
            float(item["net_return_after_cost"]) for item in directions.values()
        )
        values["direction_attribution"] = directions
        values["directional_net_reconciliation_error"] = abs(net - directional_net)
        if values["directional_net_reconciliation_error"] > RECONCILIATION_TOLERANCE:
            raise Blocked("directional account returns do not reconcile with total")
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
    frame = load_market_frame(market)
    directionality = plan.get("directionality", {})
    bidirectional = (
        isinstance(directionality, dict)
        and directionality.get("can_short") is True
    )
    position_sizing = plan.get("position_sizing")
    fixed_contract = plan.get("fixed_strategy_contract") if bidirectional else None
    if not isinstance(position_sizing, dict):
        raise Blocked("position sizing contract is invalid")
    try:
        declared_wallet = finite_positive(
            position_sizing.get("dry_run_wallet_quote"),
            field="position sizing wallet",
        )
        declared_stake = finite_positive(
            position_sizing.get("stake_amount_quote"),
            field="position sizing stake",
        )
        maximum_fraction = finite_positive(
            position_sizing.get("maximum_nominal_wallet_fraction"),
            field="position sizing maximum fraction",
        )
        maximum_open_trades = int(position_sizing.get("maximum_open_trades", 0))
    except (TypeError, ValueError) as exc:
        raise Blocked("position sizing contract is invalid") from exc
    if (
        maximum_open_trades != 1
        or position_sizing.get("position_adjustment") is not False
        or declared_stake / declared_wallet > maximum_fraction
    ):
        raise Blocked("position sizing contract is invalid")
    if bidirectional and not isinstance(fixed_contract, dict):
        raise Blocked("bidirectional fixed strategy contract is invalid")
    target = plan.get("target")
    if not isinstance(target, dict):
        raise Blocked("target contract is invalid")
    leverage_limit = finite_positive(target.get("leverage"), field="target leverage")
    pair = str(target.get("pair", ""))
    instrument = str(target.get("instrument", ""))
    if not pair or not instrument:
        raise Blocked("target pair or instrument is invalid")
    namespace = instrument.replace("-", "_")
    wallet = declared_wallet
    stake_limit = declared_stake
    sizing_contract = {
        "dry_run_wallet_quote": wallet,
        "stake_amount_quote": stake_limit,
        "maximum_nominal_wallet_fraction": maximum_fraction,
        "maximum_open_trades": maximum_open_trades,
        "position_adjustment": position_sizing["position_adjustment"],
        "leverage_limit": leverage_limit,
    }
    sizing_digest = canonical_digest(sizing_contract)
    config = {
        "dry_run": True,
        "dry_run_wallet": wallet,
        "stake_currency": "USDT",
        "stake_amount": stake_limit,
        "max_open_trades": int(position_sizing["maximum_open_trades"]),
        "timeframe": "15m",
        "trading_mode": "futures", "margin_mode": "isolated",
        "exchange": {"name": "okx", "pair_whitelist": [pair], "enable_ws": False},
        "pairlists": [{"method": "StaticPairList"}],
    }
    Path("/work/config.json").write_text(json.dumps(config, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    isolation = plan["data_isolation"]
    costs = plan["costs"]
    if not isinstance(isolation, dict) or not isinstance(costs, dict):
        raise Blocked("plan costs/windows are invalid")
    windows = {key: (iso(isolation[key]["start_at"]), iso(isolation[key]["end_at"])) for key in ("train", "validation")}
    if windows["train"][1] != windows["validation"][0]:
        raise Blocked("TRAIN and VALIDATION boundaries must be contiguous and non-overlapping")
    expected_market_start = windows["train"][0] - pd.Timedelta(
        seconds=(
            int(isolation["warmup_closed_candles"])
            + int(isolation["integrity_margin_closed_candles"])
        )
        * TIMEFRAME_SECONDS
    )
    expected_market_end = windows["validation"][1] - pd.Timedelta(
        seconds=TIMEFRAME_SECONDS
    )
    if (
        frame["date"].min().to_pydatetime() != expected_market_start
        or frame["date"].max().to_pydatetime() != expected_market_end
    ):
        raise Blocked("market artifact must contain only the frozen warmup, TRAIN, and VALIDATION prefix")
    window_data_roots = {
        key: store_window_data(
            frame,
            start=start,
            end=end,
            key=f"{namespace}-{key}",
            pair=pair,
        )
        for key, (start, end) in windows.items()
    }
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
                run_window(
                    cls,
                    source_path,
                    f"{namespace}-{key}",
                    start,
                    end,
                    metadata,
                    fee,
                    data_root=window_data_roots[key],
                    wallet=wallet,
                    pair=pair,
                ),
                fee,
                slippage,
                sensitivity_fee,
                sensitivity_slippage,
                wallet=wallet,
                stake_limit=stake_limit,
                leverage_limit=leverage_limit,
                sizing_digest=sizing_digest,
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
            * (
                float(validation["modeled_fee_cost"])
                + float(validation["modeled_slippage_cost"])
            )
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
    return {
        "contract": CONTRACT,
        "plan_digest": canonical_digest(plan),
        "market_digest": sha256(market.read_bytes()).hexdigest(),
        "accounting_contract": {
            "contract": "canonical-v13-account-notional-cost-v1",
            "fee_inclusive_source": "sum(profit_abs)/wallet",
            "fee_already_included": True,
            "slippage_source": "slippage_rate*sum(amount*(open_rate+close_rate))/wallet",
            "sizing_contract": sizing_contract,
            "sizing_digest": sizing_digest,
        },
        "window_contract": {
            "end_exclusive": True,
            "timeframe_seconds": TIMEFRAME_SECONDS,
            "window_data_digests": {
                key: directory_digest(root) for key, root in window_data_roots.items()
            },
        },
        "trial_count": len(rows),
        "selected_trial_numbers": finalists,
        "trial_evidence_digest": canonical_digest(evidence_rows),
        "trials": rows,
        "execution_side_effects": 0,
        "credential_access": "NONE",
        "trading_capability": "TRADING_DISABLED",
    }


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
