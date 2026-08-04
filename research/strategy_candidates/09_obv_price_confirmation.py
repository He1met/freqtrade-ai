"""Price/OBV confirmation candidate.

15m horizon.  A dual EMA cross is accepted only when on-balance volume agrees
with direction.  It assumes reported volume contains useful participation
information; exchange-specific volume distortions are the primary risk.
"""

import talib.abstract as ta
from pandas import DataFrame
from freqtrade.strategy import IStrategy


class Candidate09ObvPriceConfirmation(IStrategy):
    timeframe = "15m"
    can_short = True
    startup_candle_count = 120
    stoploss = -0.034
    minimal_roi = {"0": 0.017, "300": 0.007, "720": 0.0}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema_fast"] = ta.EMA(dataframe, timeperiod=16)
        dataframe["ema_slow"] = ta.EMA(dataframe, timeperiod=48)
        dataframe["obv"] = ta.OBV(dataframe)
        dataframe["obv_ema"] = ta.EMA(dataframe["obv"], timeperiod=34)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        price_up = (dataframe["ema_fast"] > dataframe["ema_slow"]) & (
            dataframe["ema_fast"].shift(1) <= dataframe["ema_slow"].shift(1)
        )
        price_down = (dataframe["ema_fast"] < dataframe["ema_slow"]) & (
            dataframe["ema_fast"].shift(1) >= dataframe["ema_slow"].shift(1)
        )
        dataframe.loc[price_up & (dataframe["obv"] > dataframe["obv_ema"]), "enter_long"] = 1
        dataframe.loc[price_down & (dataframe["obv"] < dataframe["obv_ema"]), "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["ema_fast"] < dataframe["ema_slow"], "exit_long"] = 1
        dataframe.loc[dataframe["ema_fast"] > dataframe["ema_slow"], "exit_short"] = 1
        return dataframe
