"""Chaikin accumulation breakout confirmation hypothesis, BTC perpetuals 15m."""

import talib.abstract as ta
from pandas import DataFrame
from freqtrade.strategy import IStrategy


class Candidate09ChaikinBreakout(IStrategy):
    timeframe = "15m"
    can_short = True
    startup_candle_count = 120
    stoploss = -0.034
    minimal_roi = {"0": 0.020, "300": 0.008, "840": 0.0}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["adosc"] = ta.ADOSC(dataframe, fastperiod=3, slowperiod=10)
        dataframe["adosc_base"] = dataframe["adosc"].rolling(48).mean().shift(1)
        dataframe["upper"] = dataframe["high"].rolling(32).max().shift(1)
        dataframe["lower"] = dataframe["low"].rolling(32).min().shift(1)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[(dataframe["close"] > dataframe["upper"]) & (dataframe["adosc"] > 0) & (dataframe["adosc"] > dataframe["adosc_base"]) & (dataframe["volume"] > 0), "enter_long"] = 1
        dataframe.loc[(dataframe["close"] < dataframe["lower"]) & (dataframe["adosc"] < 0) & (dataframe["adosc"] < dataframe["adosc_base"]) & (dataframe["volume"] > 0), "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["adosc"] < 0, "exit_long"] = 1
        dataframe.loc[dataframe["adosc"] > 0, "exit_short"] = 1
        return dataframe
