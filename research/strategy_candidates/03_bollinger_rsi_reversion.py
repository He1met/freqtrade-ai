"""Rolling VWAP z-score reversion hypothesis for BTC perpetuals, 15m."""

import talib.abstract as ta
from pandas import DataFrame
from freqtrade.strategy import IStrategy


class Candidate03RollingVwapZscore(IStrategy):
    timeframe = "15m"
    can_short = True
    startup_candle_count = 120
    stoploss = -0.024
    minimal_roi = {"0": 0.010, "150": 0.003, "420": 0.0}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        typical = (dataframe["high"] + dataframe["low"] + dataframe["close"]) / 3
        volume_sum = dataframe["volume"].rolling(48).sum()
        dataframe["rvwap"] = (typical * dataframe["volume"]).rolling(48).sum() / volume_sum
        dataframe["spread_std"] = (dataframe["close"] - dataframe["rvwap"]).rolling(48).std()
        dataframe["zscore"] = (dataframe["close"] - dataframe["rvwap"]) / dataframe["spread_std"]
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[(dataframe["zscore"] > -1.6) & (dataframe["zscore"].shift(1) <= -1.6) & (dataframe["adx"] < 26) & (dataframe["volume"] > 0), "enter_long"] = 1
        dataframe.loc[(dataframe["zscore"] < 1.6) & (dataframe["zscore"].shift(1) >= 1.6) & (dataframe["adx"] < 26) & (dataframe["volume"] > 0), "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["zscore"] >= 0, "exit_long"] = 1
        dataframe.loc[dataframe["zscore"] <= 0, "exit_short"] = 1
        return dataframe
