"""Leakage-safe Ichimoku-style cloud trend candidate.

15m horizon.  All channel values use present/past candles only; no displaced
future cloud or lagging-span comparison is used.  The approach can react late
to sharp reversals and performs poorly in narrow clouds.
"""

from pandas import DataFrame
from freqtrade.strategy import IStrategy


class Candidate08IchimokuCloud(IStrategy):
    timeframe = "15m"
    can_short = True
    startup_candle_count = 100
    stoploss = -0.04
    minimal_roi = {"0": 0.024, "420": 0.01, "1200": 0.0}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        high_9 = dataframe["high"].rolling(9).max()
        low_9 = dataframe["low"].rolling(9).min()
        high_26 = dataframe["high"].rolling(26).max()
        low_26 = dataframe["low"].rolling(26).min()
        high_52 = dataframe["high"].rolling(52).max()
        low_52 = dataframe["low"].rolling(52).min()
        dataframe["tenkan"] = (high_9 + low_9) / 2
        dataframe["kijun"] = (high_26 + low_26) / 2
        dataframe["cloud_a"] = (dataframe["tenkan"] + dataframe["kijun"]) / 2
        dataframe["cloud_b"] = (high_52 + low_52) / 2
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        cross_up = (dataframe["tenkan"] > dataframe["kijun"]) & (
            dataframe["tenkan"].shift(1) <= dataframe["kijun"].shift(1)
        )
        cross_down = (dataframe["tenkan"] < dataframe["kijun"]) & (
            dataframe["tenkan"].shift(1) >= dataframe["kijun"].shift(1)
        )
        cloud_top = dataframe[["cloud_a", "cloud_b"]].max(axis=1)
        cloud_bottom = dataframe[["cloud_a", "cloud_b"]].min(axis=1)
        dataframe.loc[cross_up & (dataframe["close"] > cloud_top) & (dataframe["volume"] > 0), "enter_long"] = 1
        dataframe.loc[cross_down & (dataframe["close"] < cloud_bottom) & (dataframe["volume"] > 0), "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["tenkan"] < dataframe["kijun"], "exit_long"] = 1
        dataframe.loc[dataframe["tenkan"] > dataframe["kijun"], "exit_short"] = 1
        return dataframe
