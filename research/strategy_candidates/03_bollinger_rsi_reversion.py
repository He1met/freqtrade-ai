"""Bollinger/RSI mean-reversion candidate for range-bound BTC perpetuals.

15m horizon.  Entries require a close outside the band followed by re-entry,
with low ADX as a regime guard.  The main risk is fading a genuine breakout.
"""

import talib.abstract as ta
from pandas import DataFrame
from freqtrade.strategy import IStrategy


class Candidate03BollingerRsiReversion(IStrategy):
    timeframe = "15m"
    can_short = True
    startup_candle_count = 80
    stoploss = -0.028
    minimal_roi = {"0": 0.012, "180": 0.004, "480": 0.0}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        bands = ta.BBANDS(
            dataframe, timeperiod=24, nbdevup=2.2, nbdevdn=2.2, matype=0
        )
        dataframe["bb_upper"] = bands["upperband"]
        dataframe["bb_middle"] = bands["middleband"]
        dataframe["bb_lower"] = bands["lowerband"]
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=10)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (dataframe["close"] > dataframe["bb_lower"])
            & (dataframe["close"].shift(1) <= dataframe["bb_lower"].shift(1))
            & (dataframe["rsi"] < 42)
            & (dataframe["adx"] < 28)
            & (dataframe["volume"] > 0),
            "enter_long",
        ] = 1
        dataframe.loc[
            (dataframe["close"] < dataframe["bb_upper"])
            & (dataframe["close"].shift(1) >= dataframe["bb_upper"].shift(1))
            & (dataframe["rsi"] > 58)
            & (dataframe["adx"] < 28)
            & (dataframe["volume"] > 0),
            "enter_short",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["close"] >= dataframe["bb_middle"], "exit_long"] = 1
        dataframe.loc[dataframe["close"] <= dataframe["bb_middle"], "exit_short"] = 1
        return dataframe
