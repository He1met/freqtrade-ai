import talib.abstract as ta
from pandas import DataFrame
from freqtrade.strategy import IStrategy


class CanonicalTrendPullbackBaseline(IStrategy):
    timeframe = "15m"
    can_short = False
    startup_candle_count = 240
    stoploss = -0.04
    minimal_roi = {"0": 0.012, "180": 0.007, "480": 0.003}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema_fast"] = ta.EMA(dataframe, timeperiod=24)
        dataframe["ema_trend"] = ta.EMA(dataframe, timeperiod=96)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        long_entries = (
            (dataframe["close"] > dataframe["ema_trend"])
            & (dataframe["ema_fast"] > dataframe["ema_trend"])
            & (dataframe["adx"] > 16)
            & (dataframe["rsi"] > 38)
            & (dataframe["rsi"] < 55)
            & (dataframe["volume"] > 0)
        )
        dataframe.loc[long_entries, "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        long_exits = (
            (dataframe["rsi"] > 68)
            | (dataframe["ema_fast"] < dataframe["ema_trend"])
        )
        dataframe.loc[long_exits, "exit_long"] = 1
        return dataframe
