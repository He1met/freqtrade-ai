"""Bollinger-width regime-switching trend/reversion hypothesis, BTC 15m."""

import talib.abstract as ta
from pandas import DataFrame
from freqtrade.strategy import IStrategy


class Candidate10BandwidthRegimeSwitch(IStrategy):
    timeframe = "15m"
    can_short = True
    startup_candle_count = 180
    stoploss = -0.03
    minimal_roi = {"0": 0.015, "240": 0.006, "600": 0.0}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        bands = ta.BBANDS(dataframe, timeperiod=24, nbdevup=2.0, nbdevdn=2.0)
        dataframe["upper"] = bands["upperband"]
        dataframe["middle"] = bands["middleband"]
        dataframe["lower"] = bands["lowerband"]
        dataframe["width"] = (dataframe["upper"] - dataframe["lower"]) / dataframe["middle"]
        dataframe["width_base"] = dataframe["width"].rolling(96).median().shift(1)
        dataframe["ema_fast"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema_slow"] = ta.EMA(dataframe, timeperiod=60)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        expanding = dataframe["width"] >= dataframe["width_base"]
        trend_long = expanding & (dataframe["ema_fast"] > dataframe["ema_slow"]) & (dataframe["close"] > dataframe["upper"].shift(1))
        trend_short = expanding & (dataframe["ema_fast"] < dataframe["ema_slow"]) & (dataframe["close"] < dataframe["lower"].shift(1))
        range_long = (~expanding) & (dataframe["close"] > dataframe["lower"]) & (dataframe["close"].shift(1) <= dataframe["lower"].shift(1))
        range_short = (~expanding) & (dataframe["close"] < dataframe["upper"]) & (dataframe["close"].shift(1) >= dataframe["upper"].shift(1))
        dataframe.loc[(trend_long | range_long) & (dataframe["volume"] > 0), "enter_long"] = 1
        dataframe.loc[(trend_short | range_short) & (dataframe["volume"] > 0), "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["close"] < dataframe["middle"], "exit_long"] = 1
        dataframe.loc[dataframe["close"] > dataframe["middle"], "exit_short"] = 1
        return dataframe
