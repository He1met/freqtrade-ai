"""Aroon persistence trend hypothesis for BTC perpetuals, 15m."""

import talib.abstract as ta
from pandas import DataFrame
from freqtrade.strategy import IStrategy


class Candidate08AroonPersistence(IStrategy):
    timeframe = "15m"
    can_short = True
    startup_candle_count = 140
    stoploss = -0.036
    minimal_roi = {"0": 0.021, "360": 0.008, "960": 0.0}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        aroon = ta.AROON(dataframe, timeperiod=25)
        dataframe["aroon_up"] = aroon["aroonup"]
        dataframe["aroon_down"] = aroon["aroondown"]
        dataframe["ema"] = ta.EMA(dataframe, timeperiod=100)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[(dataframe["aroon_up"] > 70) & (dataframe["aroon_down"] < 30) & (dataframe["aroon_up"].shift(1) <= 70) & (dataframe["close"] > dataframe["ema"]) & (dataframe["volume"] > 0), "enter_long"] = 1
        dataframe.loc[(dataframe["aroon_down"] > 70) & (dataframe["aroon_up"] < 30) & (dataframe["aroon_down"].shift(1) <= 70) & (dataframe["close"] < dataframe["ema"]) & (dataframe["volume"] > 0), "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["aroon_up"] < dataframe["aroon_down"], "exit_long"] = 1
        dataframe.loc[dataframe["aroon_down"] < dataframe["aroon_up"], "exit_short"] = 1
        return dataframe
