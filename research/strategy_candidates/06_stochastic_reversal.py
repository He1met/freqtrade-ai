"""Stochastic exhaustion reversal candidate.

15m horizon.  It requires both a stochastic cross and a recent Bollinger-band
extreme.  It targets short-lived snapbacks; persistent one-way trends can keep
the oscillator pinned and remain the dominant failure mode.
"""

import talib.abstract as ta
from pandas import DataFrame
from freqtrade.strategy import IStrategy


class Candidate06StochasticReversal(IStrategy):
    timeframe = "15m"
    can_short = True
    startup_candle_count = 80
    stoploss = -0.025
    minimal_roi = {"0": 0.01, "150": 0.003, "360": 0.0}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        stoch = ta.STOCHF(dataframe, fastk_period=14, fastd_period=3, fastd_matype=0)
        dataframe["fastk"] = stoch["fastk"]
        dataframe["fastd"] = stoch["fastd"]
        bands = ta.BBANDS(dataframe, timeperiod=20, nbdevup=2.0, nbdevdn=2.0)
        dataframe["bb_upper"] = bands["upperband"]
        dataframe["bb_middle"] = bands["middleband"]
        dataframe["bb_lower"] = bands["lowerband"]
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        cross_up = (dataframe["fastk"] > dataframe["fastd"]) & (
            dataframe["fastk"].shift(1) <= dataframe["fastd"].shift(1)
        )
        cross_down = (dataframe["fastk"] < dataframe["fastd"]) & (
            dataframe["fastk"].shift(1) >= dataframe["fastd"].shift(1)
        )
        dataframe.loc[
            cross_up
            & (dataframe["fastk"] < 30)
            & (dataframe["low"].shift(1) < dataframe["bb_lower"].shift(1))
            & (dataframe["volume"] > 0),
            "enter_long",
        ] = 1
        dataframe.loc[
            cross_down
            & (dataframe["fastk"] > 70)
            & (dataframe["high"].shift(1) > dataframe["bb_upper"].shift(1))
            & (dataframe["volume"] > 0),
            "enter_short",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["fastk"] > 70, "exit_long"] = 1
        dataframe.loc[dataframe["fastk"] < 30, "exit_short"] = 1
        return dataframe
