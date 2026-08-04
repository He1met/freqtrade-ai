"""Rate-of-change impulse candidate with ADX confirmation.

15m horizon.  The strategy joins statistically large 12-candle moves only in
an established directional regime.  Momentum crashes and clustered entries
after liquidation impulses are key risks.
"""

import talib.abstract as ta
from pandas import DataFrame
from freqtrade.strategy import IStrategy


class Candidate07RocAdxImpulse(IStrategy):
    timeframe = "15m"
    can_short = True
    startup_candle_count = 120
    stoploss = -0.038
    minimal_roi = {"0": 0.022, "300": 0.009, "900": 0.0}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["roc"] = ta.ROC(dataframe, timeperiod=12)
        dataframe["roc_abs_mean"] = dataframe["roc"].abs().rolling(96).mean().shift(1)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["ema"] = ta.EMA(dataframe, timeperiod=100)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        threshold = dataframe["roc_abs_mean"] * 1.6
        dataframe.loc[
            (dataframe["roc"] > threshold)
            & (dataframe["adx"] > 22)
            & (dataframe["close"] > dataframe["ema"])
            & (dataframe["volume"] > 0),
            "enter_long",
        ] = 1
        dataframe.loc[
            (dataframe["roc"] < -threshold)
            & (dataframe["adx"] > 22)
            & (dataframe["close"] < dataframe["ema"])
            & (dataframe["volume"] > 0),
            "enter_short",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["roc"] < 0, "exit_long"] = 1
        dataframe.loc[dataframe["roc"] > 0, "exit_short"] = 1
        return dataframe
