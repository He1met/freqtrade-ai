"""Williams percent-R range reversal hypothesis for BTC perpetuals, 15m."""

import talib.abstract as ta
from pandas import DataFrame
from freqtrade.strategy import IStrategy


class Candidate06WilliamsRangeReversal(IStrategy):
    timeframe = "15m"
    can_short = True
    startup_candle_count = 100
    stoploss = -0.022
    minimal_roi = {"0": 0.009, "120": 0.003, "360": 0.0}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["willr"] = ta.WILLR(dataframe, timeperiod=21)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["ema"] = ta.EMA(dataframe, timeperiod=48)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[(dataframe["willr"] > -80) & (dataframe["willr"].shift(1) <= -80) & (dataframe["adx"] < 22) & (dataframe["close"] < dataframe["ema"]) & (dataframe["volume"] > 0), "enter_long"] = 1
        dataframe.loc[(dataframe["willr"] < -20) & (dataframe["willr"].shift(1) >= -20) & (dataframe["adx"] < 22) & (dataframe["close"] > dataframe["ema"]) & (dataframe["volume"] > 0), "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["willr"] >= -35, "exit_long"] = 1
        dataframe.loc[dataframe["willr"] <= -65, "exit_short"] = 1
        return dataframe
