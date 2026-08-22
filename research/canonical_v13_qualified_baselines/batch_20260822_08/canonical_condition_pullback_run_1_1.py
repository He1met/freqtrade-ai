import talib.abstract as ta
from pandas import DataFrame
from freqtrade.strategy import IStrategy


class CanonicalConditionPullbackLong(IStrategy):
    timeframe = "15m"
    can_short = False
    startup_candle_count = 200
    stoploss = -0.025
    minimal_roi = {"0": 0.008, "240": 0.004, "720": 0.0}
    position_adjustment_enable = False
    protections = [{"method": "CooldownPeriod", "stop_duration_candles": 2}]

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
        return min(2.0, max_leverage)

    def custom_stake_amount(
        self,
        pair,
        current_time,
        current_rate,
        proposed_stake,
        min_stake,
        max_stake,
        leverage,
        entry_tag,
        side,
        **kwargs,
    ):
        bounded_stake = min(proposed_stake, max_stake)
        if bounded_stake <= 0 or (
            min_stake is not None and bounded_stake < min_stake
        ):
            return 0.0
        return bounded_stake

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["volume_mean20"] = dataframe["volume"].rolling(20).mean()
        dataframe["atr_ratio"] = dataframe["atr"] / dataframe["close"]
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        pullback_reclaimed = (
            (dataframe["low"].shift(1) <= dataframe["ema20"].shift(1))
            & (dataframe["close"] > dataframe["ema20"])
            & (dataframe["close"] > dataframe["open"])
        )
        long_entries = (
            (dataframe["ema20"] > dataframe["ema50"])
            & pullback_reclaimed
            & dataframe["rsi"].between(45.0, 62.0)
            & (dataframe["volume"] >= dataframe["volume_mean20"] * 0.8)
            & dataframe["atr_ratio"].between(0.001, 0.03)
            & (dataframe["volume"] > 0)
        )
        dataframe.loc[long_entries, "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        long_exits = (
            (dataframe["close"] < dataframe["ema50"])
            | (dataframe["rsi"] > 72.0)
            | (dataframe["atr_ratio"] > 0.04)
        )
        dataframe.loc[long_exits, "exit_long"] = 1
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
        if exposure_seconds >= 16 * 60 * 60:
            return "condition_time_stop"
        return None
