"""Money-flow impulse continuation hypothesis for BTC perpetuals, 15m."""

import talib.abstract as ta
from pandas import DataFrame
from freqtrade.strategy import IStrategy


class Candidate07MfiImpulse(IStrategy):
    timeframe = "15m"
    can_short = True
    startup_candle_count = 140
    stoploss = -0.032
    minimal_roi = {"0": 0.019, "300": 0.007, "840": 0.0}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["mfi"] = ta.MFI(dataframe, timeperiod=14)
        dataframe["ema"] = ta.EMA(dataframe, timeperiod=100)
        dataframe["volume_base"] = dataframe["volume"].rolling(48).median().shift(1)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        volume_impulse = dataframe["volume"] > dataframe["volume_base"] * 1.5
        dataframe.loc[(dataframe["mfi"] > 60) & (dataframe["mfi"].shift(1) <= 60) & (dataframe["close"] > dataframe["ema"]) & volume_impulse, "enter_long"] = 1
        dataframe.loc[(dataframe["mfi"] < 40) & (dataframe["mfi"].shift(1) >= 40) & (dataframe["close"] < dataframe["ema"]) & volume_impulse, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["mfi"] < 50, "exit_long"] = 1
        dataframe.loc[dataframe["mfi"] > 50, "exit_short"] = 1
        return dataframe
