"""Keltner pullback continuation candidate.

15m horizon.  It buys or sells a return through the EMA after a pullback while
the 50/200 EMA regime remains intact.  Sideways EMA tangles and gap-like moves
through the channel are the principal risks.
"""

import talib.abstract as ta
from pandas import DataFrame
from freqtrade.strategy import IStrategy


class Candidate05KeltnerPullback(IStrategy):
    timeframe = "15m"
    can_short = True
    startup_candle_count = 210
    stoploss = -0.03
    minimal_roi = {"0": 0.014, "240": 0.005, "600": 0.0}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema_mid"] = ta.EMA(dataframe, timeperiod=24)
        dataframe["ema_trend"] = ta.EMA(dataframe, timeperiod=80)
        dataframe["ema_regime"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=20)
        dataframe["kc_upper"] = dataframe["ema_mid"] + dataframe["atr"] * 1.6
        dataframe["kc_lower"] = dataframe["ema_mid"] - dataframe["atr"] * 1.6
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        reclaim = (dataframe["close"] > dataframe["ema_mid"]) & (
            dataframe["close"].shift(1) <= dataframe["ema_mid"].shift(1)
        )
        reject = (dataframe["close"] < dataframe["ema_mid"]) & (
            dataframe["close"].shift(1) >= dataframe["ema_mid"].shift(1)
        )
        dataframe.loc[
            reclaim
            & (dataframe["ema_trend"] > dataframe["ema_regime"])
            & (dataframe["low"].shift(1) < dataframe["kc_lower"].shift(1))
            & (dataframe["volume"] > 0),
            "enter_long",
        ] = 1
        dataframe.loc[
            reject
            & (dataframe["ema_trend"] < dataframe["ema_regime"])
            & (dataframe["high"].shift(1) > dataframe["kc_upper"].shift(1))
            & (dataframe["volume"] > 0),
            "enter_short",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["close"] >= dataframe["kc_upper"], "exit_long"] = 1
        dataframe.loc[dataframe["close"] <= dataframe["kc_lower"], "exit_short"] = 1
        return dataframe
