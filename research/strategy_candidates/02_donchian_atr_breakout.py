"""Donchian/ATR breakout candidate for volatile BTC perpetual sessions.

15m horizon.  Prior-candle channel boundaries prevent current-bar leakage.
The thesis is volatility expansion after compression; false breakouts and
overnight reversals are the primary risks.
"""

import talib.abstract as ta
from pandas import DataFrame
from freqtrade.strategy import IStrategy


class Candidate02DonchianAtrBreakout(IStrategy):
    timeframe = "15m"
    can_short = True
    startup_candle_count = 100
    stoploss = -0.04
    minimal_roi = {"0": 0.025, "360": 0.012, "1080": 0.0}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["upper"] = dataframe["high"].rolling(48).max().shift(1)
        dataframe["lower"] = dataframe["low"].rolling(48).min().shift(1)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["atr_pct"] = dataframe["atr"] / dataframe["close"]
        dataframe["atr_floor"] = dataframe["atr_pct"].rolling(96).median().shift(1)
        dataframe["ema"] = ta.EMA(dataframe, timeperiod=100)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (dataframe["close"] > dataframe["upper"])
            & (dataframe["close"] > dataframe["ema"])
            & (dataframe["atr_pct"] > dataframe["atr_floor"])
            & (dataframe["volume"] > 0),
            "enter_long",
        ] = 1
        dataframe.loc[
            (dataframe["close"] < dataframe["lower"])
            & (dataframe["close"] < dataframe["ema"])
            & (dataframe["atr_pct"] > dataframe["atr_floor"])
            & (dataframe["volume"] > 0),
            "enter_short",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        midpoint = (dataframe["upper"] + dataframe["lower"]) / 2
        dataframe.loc[dataframe["close"] < midpoint, "exit_long"] = 1
        dataframe.loc[dataframe["close"] > midpoint, "exit_short"] = 1
        return dataframe
