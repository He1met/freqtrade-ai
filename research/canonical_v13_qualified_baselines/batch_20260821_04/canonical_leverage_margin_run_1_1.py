from pandas import DataFrame
from freqtrade.strategy import IStrategy


class CanonicalLeverageMarginBaseline(IStrategy):
    timeframe = "15m"
    can_short = False
    startup_candle_count = 1
    stoploss = -0.30
    minimal_roi = {"0": 100.0}

    def leverage(
        self,
        pair,
        current_time,
        current_rate,
        proposed_leverage,
        max_leverage,
        entry_tag,
        side,
        **kwargs,
    ):
        return min(25.0, max_leverage)

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        daily_entry = (
            (dataframe["date"].dt.hour == 13)
            & (dataframe["date"].dt.minute == 0)
            & (dataframe["volume"] > 0)
        )
        dataframe.loc[daily_entry, "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        daily_exit = (
            (dataframe["date"].dt.hour == 12)
            & (dataframe["date"].dt.minute == 30)
            & (dataframe["volume"] > 0)
        )
        dataframe.loc[daily_exit, "exit_long"] = 1
        return dataframe
