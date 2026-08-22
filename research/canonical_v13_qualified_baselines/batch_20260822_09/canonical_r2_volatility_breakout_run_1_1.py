import talib.abstract as ta
from pandas import DataFrame
from freqtrade.strategy import IStrategy


class CanonicalR2VolatilityAdjustedBreakoutLong(IStrategy):
    timeframe = "15m"
    can_short = False
    startup_candle_count = 240
    stoploss = -0.018
    minimal_roi = {"0": 0.014, "480": 0.009, "1440": 0.005}
    position_adjustment_enable = False
    protections = [{"method": "CooldownPeriod", "stop_duration_candles": 12}]

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
        dataframe["ema32"] = ta.EMA(dataframe, timeperiod=32)
        dataframe["ema96"] = ta.EMA(dataframe, timeperiod=96)
        dataframe["ema192"] = ta.EMA(dataframe, timeperiod=192)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["atr_ratio"] = dataframe["atr"] / dataframe["close"]
        dataframe["atr_median96"] = dataframe["atr_ratio"].rolling(96).median().shift(1)
        dataframe["range_high32"] = dataframe["high"].rolling(32).max().shift(1)
        dataframe["volume_mean32"] = dataframe["volume"].rolling(32).mean().shift(1)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        slow_regime = (
            (dataframe["ema32"] > dataframe["ema96"])
            & (dataframe["ema96"] > dataframe["ema192"])
            & (dataframe["ema96"] > dataframe["ema96"].shift(16))
            & (dataframe["ema192"] > dataframe["ema192"].shift(16))
        )
        normalized_volatility = (
            (dataframe["atr_ratio"] >= dataframe["atr_median96"] * 0.75)
            & (dataframe["atr_ratio"] <= dataframe["atr_median96"] * 1.75)
        )
        confirmed_breakout = (
            (dataframe["close"] > dataframe["range_high32"])
            & (dataframe["close"].shift(1) <= dataframe["range_high32"].shift(1))
            & (dataframe["close"] > dataframe["open"])
        )
        long_entries = (
            slow_regime
            & normalized_volatility
            & confirmed_breakout
            & (dataframe["volume"] >= dataframe["volume_mean32"] * 1.05)
            & (dataframe["volume"] > 0)
        )
        dataframe.loc[long_entries, "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        long_exits = (
            (dataframe["close"] < dataframe["ema32"])
            | (dataframe["ema96"] < dataframe["ema192"])
            | (dataframe["atr_ratio"] > dataframe["atr_median96"] * 2.5)
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
        if exposure_seconds >= 48 * 60 * 60:
            return "condition_bar_time_stop"
        return None
