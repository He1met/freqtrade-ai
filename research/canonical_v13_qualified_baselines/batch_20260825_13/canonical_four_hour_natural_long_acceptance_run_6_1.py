# Immutable acceptance requalification revision 6; trading behavior is unchanged.

from pandas import DataFrame
from freqtrade.strategy import IStrategy


class CanonicalFourHourNaturalLongBaseline(IStrategy):
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
        return min(12.0, max_leverage)

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        four_hour_opportunity = (
            (dataframe["date"].dt.hour % 4 == 2)
            & (dataframe["date"].dt.minute == 0)
            & (dataframe["volume"] > 0)
        )
        dataframe.loc[four_hour_opportunity, "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[:, "exit_long"] = 0
        return dataframe

    def custom_exit(
        self,
        pair,
        trade,
        current_time,
        current_rate,
        current_profit,
        **kwargs,
    ):
        exposure_seconds = (current_time - trade.open_date_utc).total_seconds()
        if exposure_seconds >= 20 * 60 * 60:
            return "fixed_20h_exposure"
        return None
