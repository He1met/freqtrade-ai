"""Failed Donchian breakout reversal hypothesis for BTC perpetuals, 15m."""

import talib.abstract as ta
from pandas import DataFrame
from freqtrade.strategy import IStrategy


class Candidate02FailedBreakoutReversal(IStrategy):
    timeframe = "5m"
    can_short = True
    startup_candle_count = 120
    stoploss = -0.026
    minimal_roi = {"0": 0.012, "180": 0.004, "480": 0.0}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["upper"] = dataframe["high"].rolling(64).max().shift(1)
        dataframe["lower"] = dataframe["low"].rolling(64).min().shift(1)
        dataframe["mid"] = (dataframe["upper"] + dataframe["lower"]) / 2
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[(dataframe["close"] > dataframe["lower"]) & (dataframe["close"].shift(1) <= dataframe["lower"].shift(1)) & (dataframe["adx"] < 24) & (dataframe["volume"] > 0), "enter_long"] = 1
        dataframe.loc[(dataframe["close"] < dataframe["upper"]) & (dataframe["close"].shift(1) >= dataframe["upper"].shift(1)) & (dataframe["adx"] < 24) & (dataframe["volume"] > 0), "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["close"] >= dataframe["mid"], "exit_long"] = 1
        dataframe.loc[dataframe["close"] <= dataframe["mid"], "exit_short"] = 1
        return dataframe
