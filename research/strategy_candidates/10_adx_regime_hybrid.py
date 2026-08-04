"""ADX regime-switching hybrid candidate.

15m horizon.  Trending candles use EMA continuation, while low-ADX candles use
Bollinger reversion.  The explicit split is intended to diversify signal
sources, but regime lag can select the wrong sub-model around transitions.
"""

import talib.abstract as ta
from pandas import DataFrame
from freqtrade.strategy import IStrategy


class Candidate10AdxRegimeHybrid(IStrategy):
    timeframe = "15m"
    can_short = True
    startup_candle_count = 210
    stoploss = -0.032
    minimal_roi = {"0": 0.015, "240": 0.006, "600": 0.0}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["ema_fast"] = ta.EMA(dataframe, timeperiod=24)
        dataframe["ema_slow"] = ta.EMA(dataframe, timeperiod=72)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        bands = ta.BBANDS(dataframe, timeperiod=24, nbdevup=2.0, nbdevdn=2.0)
        dataframe["bb_upper"] = bands["upperband"]
        dataframe["bb_middle"] = bands["middleband"]
        dataframe["bb_lower"] = bands["lowerband"]
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        trend_long = (
            (dataframe["adx"] >= 24)
            & (dataframe["ema_fast"] > dataframe["ema_slow"])
            & (dataframe["rsi"] > 52)
            & (dataframe["rsi"].shift(1) <= 52)
        )
        trend_short = (
            (dataframe["adx"] >= 24)
            & (dataframe["ema_fast"] < dataframe["ema_slow"])
            & (dataframe["rsi"] < 48)
            & (dataframe["rsi"].shift(1) >= 48)
        )
        range_long = (
            (dataframe["adx"] < 24)
            & (dataframe["close"] > dataframe["bb_lower"])
            & (dataframe["close"].shift(1) <= dataframe["bb_lower"].shift(1))
        )
        range_short = (
            (dataframe["adx"] < 24)
            & (dataframe["close"] < dataframe["bb_upper"])
            & (dataframe["close"].shift(1) >= dataframe["bb_upper"].shift(1))
        )
        dataframe.loc[(trend_long | range_long) & (dataframe["volume"] > 0), "enter_long"] = 1
        dataframe.loc[(trend_short | range_short) & (dataframe["volume"] > 0), "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            ((dataframe["adx"] >= 24) & (dataframe["ema_fast"] < dataframe["ema_slow"]))
            | ((dataframe["adx"] < 24) & (dataframe["close"] >= dataframe["bb_middle"])),
            "exit_long",
        ] = 1
        dataframe.loc[
            ((dataframe["adx"] >= 24) & (dataframe["ema_fast"] > dataframe["ema_slow"]))
            | ((dataframe["adx"] < 24) & (dataframe["close"] <= dataframe["bb_middle"])),
            "exit_short",
        ] = 1
        return dataframe
