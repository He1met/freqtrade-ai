"""DMI slope trend-pullback hypothesis for BTC perpetuals, 15m."""

import talib.abstract as ta
from pandas import DataFrame
from freqtrade.strategy import IStrategy


class Candidate01DmiSlopePullback(IStrategy):
    timeframe = "5m"
    can_short = True
    startup_candle_count = 220
    stoploss = -0.03
    minimal_roi = {"0": 0.018, "240": 0.008, "720": 0.0}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema_mid"] = ta.EMA(dataframe, timeperiod=32)
        dataframe["ema_slow"] = ta.EMA(dataframe, timeperiod=96)
        dataframe["ema_regime"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["plus_di"] = ta.PLUS_DI(dataframe, timeperiod=14)
        dataframe["minus_di"] = ta.MINUS_DI(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        reclaim = (dataframe["close"] > dataframe["ema_mid"]) & (dataframe["close"].shift(1) <= dataframe["ema_mid"].shift(1))
        reject = (dataframe["close"] < dataframe["ema_mid"]) & (dataframe["close"].shift(1) >= dataframe["ema_mid"].shift(1))
        rising_adx = dataframe["adx"] > dataframe["adx"].shift(4)
        dataframe.loc[reclaim & rising_adx & (dataframe["plus_di"] > dataframe["minus_di"]) & (dataframe["ema_slow"] > dataframe["ema_regime"]) & (dataframe["volume"] > 0), "enter_long"] = 1
        dataframe.loc[reject & rising_adx & (dataframe["minus_di"] > dataframe["plus_di"]) & (dataframe["ema_slow"] < dataframe["ema_regime"]) & (dataframe["volume"] > 0), "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["plus_di"] < dataframe["minus_di"], "exit_long"] = 1
        dataframe.loc[dataframe["minus_di"] < dataframe["plus_di"], "exit_short"] = 1
        return dataframe
