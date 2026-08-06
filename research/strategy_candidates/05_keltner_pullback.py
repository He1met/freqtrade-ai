"""CCI zero-line trend pullback hypothesis for BTC perpetuals, 15m."""

import talib.abstract as ta
from pandas import DataFrame
from freqtrade.strategy import IStrategy


class Candidate05CciTrendPullback(IStrategy):
    timeframe = "15m"
    can_short = True
    startup_candle_count = 220
    stoploss = -0.03
    minimal_roi = {"0": 0.016, "240": 0.006, "720": 0.0}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["cci"] = ta.CCI(dataframe, timeperiod=20)
        dataframe["ema_trend"] = ta.EMA(dataframe, timeperiod=72)
        dataframe["ema_regime"] = ta.EMA(dataframe, timeperiod=200)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        cross_up = (dataframe["cci"] > 0) & (dataframe["cci"].shift(1) <= 0)
        cross_down = (dataframe["cci"] < 0) & (dataframe["cci"].shift(1) >= 0)
        dataframe.loc[cross_up & (dataframe["cci"].rolling(8).min().shift(1) < -100) & (dataframe["ema_trend"] > dataframe["ema_regime"]) & (dataframe["volume"] > 0), "enter_long"] = 1
        dataframe.loc[cross_down & (dataframe["cci"].rolling(8).max().shift(1) > 100) & (dataframe["ema_trend"] < dataframe["ema_regime"]) & (dataframe["volume"] > 0), "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["cci"] < -100, "exit_long"] = 1
        dataframe.loc[dataframe["cci"] > 100, "exit_short"] = 1
        return dataframe
