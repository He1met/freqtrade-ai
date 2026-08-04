"""MACD momentum candidate with volume confirmation.

15m horizon.  It trades fresh MACD crosses only when volume exceeds its
lagged baseline and price agrees with the 100 EMA.  Risks are late entries and
volume spikes caused by liquidation rather than durable demand.
"""

import talib.abstract as ta
from pandas import DataFrame
from freqtrade.strategy import IStrategy


class Candidate04MacdVolumeMomentum(IStrategy):
    timeframe = "15m"
    can_short = True
    startup_candle_count = 120
    stoploss = -0.032
    minimal_roi = {"0": 0.016, "300": 0.006, "720": 0.0}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        macd = ta.MACD(dataframe, fastperiod=12, slowperiod=26, signalperiod=9)
        dataframe["macd"] = macd["macd"]
        dataframe["signal"] = macd["macdsignal"]
        dataframe["ema"] = ta.EMA(dataframe, timeperiod=100)
        dataframe["volume_mean"] = dataframe["volume"].rolling(32).mean().shift(1)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        cross_up = (dataframe["macd"] > dataframe["signal"]) & (
            dataframe["macd"].shift(1) <= dataframe["signal"].shift(1)
        )
        cross_down = (dataframe["macd"] < dataframe["signal"]) & (
            dataframe["macd"].shift(1) >= dataframe["signal"].shift(1)
        )
        volume_ok = dataframe["volume"] > dataframe["volume_mean"] * 1.15
        dataframe.loc[cross_up & (dataframe["close"] > dataframe["ema"]) & volume_ok, "enter_long"] = 1
        dataframe.loc[cross_down & (dataframe["close"] < dataframe["ema"]) & volume_ok, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["macd"] < dataframe["signal"], "exit_long"] = 1
        dataframe.loc[dataframe["macd"] > dataframe["signal"], "exit_short"] = 1
        return dataframe
