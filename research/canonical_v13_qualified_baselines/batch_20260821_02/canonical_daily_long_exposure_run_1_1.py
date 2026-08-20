from pandas import DataFrame
from freqtrade.strategy import IStrategy


class CanonicalDailyLongExposureBaseline(IStrategy):
    timeframe = "15m"
    can_short = False
    startup_candle_count = 1
    stoploss = -0.08
    minimal_roi = {"0": 100.0}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        daily_entry = (
            (dataframe["date"].dt.hour == 0)
            & (dataframe["date"].dt.minute == 0)
            & (dataframe["volume"] > 0)
        )
        dataframe.loc[daily_entry, "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        daily_exit = (
            (dataframe["date"].dt.hour == 23)
            & (dataframe["date"].dt.minute == 30)
            & (dataframe["volume"] > 0)
        )
        dataframe.loc[daily_exit, "exit_long"] = 1
        return dataframe
