"""EMA/ADX trend continuation candidate for liquid BTC perpetuals.

15m horizon.  The candidate follows established trends after a shallow RSI
reset.  It assumes directional persistence and is vulnerable to whipsaw in
low-ADX ranges.  Parameters are deliberately round and fixed, not optimized.
"""

import talib.abstract as ta
from pandas import DataFrame
from freqtrade.strategy import IStrategy


class Candidate01EmaAdxTrend(IStrategy):
    timeframe = "15m"
    can_short = True
    startup_candle_count = 210
    stoploss = -0.035
    minimal_roi = {"0": 0.018, "240": 0.008, "720": 0.0}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema_fast"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema_slow"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema_regime"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        long_reset = (dataframe["rsi"] > 48) & (dataframe["rsi"].shift(1) <= 48)
        short_reset = (dataframe["rsi"] < 52) & (dataframe["rsi"].shift(1) >= 52)
        dataframe.loc[
            (dataframe["ema_fast"] > dataframe["ema_slow"])
            & (dataframe["close"] > dataframe["ema_regime"])
            & (dataframe["adx"] > 20)
            & long_reset
            & (dataframe["volume"] > 0),
            "enter_long",
        ] = 1
        dataframe.loc[
            (dataframe["ema_fast"] < dataframe["ema_slow"])
            & (dataframe["close"] < dataframe["ema_regime"])
            & (dataframe["adx"] > 20)
            & short_reset
            & (dataframe["volume"] > 0),
            "enter_short",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (dataframe["ema_fast"] < dataframe["ema_slow"]) | (dataframe["rsi"] > 72),
            "exit_long",
        ] = 1
        dataframe.loc[
            (dataframe["ema_fast"] > dataframe["ema_slow"]) | (dataframe["rsi"] < 28),
            "exit_short",
        ] = 1
        return dataframe
