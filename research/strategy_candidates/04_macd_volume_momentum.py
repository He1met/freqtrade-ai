"""ATR squeeze expansion breakout hypothesis for BTC perpetuals, 15m."""

import talib.abstract as ta
from pandas import DataFrame
from freqtrade.strategy import IStrategy


class Candidate04AtrSqueezeExpansion(IStrategy):
    timeframe = "5m"
    can_short = True
    startup_candle_count = 150
    stoploss = -0.034
    minimal_roi = {"0": 0.022, "300": 0.009, "900": 0.0}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["atr_pct"] = dataframe["atr"] / dataframe["close"]
        dataframe["atr_baseline"] = dataframe["atr_pct"].rolling(96).median().shift(1)
        dataframe["upper"] = dataframe["high"].rolling(24).max().shift(1)
        dataframe["lower"] = dataframe["low"].rolling(24).min().shift(1)
        dataframe["ema"] = ta.EMA(dataframe, timeperiod=80)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        expanding = (dataframe["atr_pct"] > dataframe["atr_baseline"]) & (dataframe["atr_pct"].shift(4) <= dataframe["atr_baseline"].shift(4))
        dataframe.loc[expanding & (dataframe["close"] > dataframe["upper"]) & (dataframe["close"] > dataframe["ema"]) & (dataframe["volume"] > 0), "enter_long"] = 1
        dataframe.loc[expanding & (dataframe["close"] < dataframe["lower"]) & (dataframe["close"] < dataframe["ema"]) & (dataframe["volume"] > 0), "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["close"] < dataframe["ema"], "exit_long"] = 1
        dataframe.loc[dataframe["close"] > dataframe["ema"], "exit_short"] = 1
        return dataframe
