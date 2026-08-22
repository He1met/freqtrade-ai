import talib.abstract as ta
from pandas import DataFrame
from freqtrade.strategy import IStrategy


class CanonicalR2MulticyclePullbackLong(IStrategy):
    timeframe = "15m"
    can_short = False
    startup_candle_count = 240
    stoploss = -0.018
    minimal_roi = {"0": 0.012, "480": 0.008, "1440": 0.004}
    position_adjustment_enable = False
    protections = [{"method": "CooldownPeriod", "stop_duration_candles": 8}]

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
        dataframe["ema16"] = ta.EMA(dataframe, timeperiod=16)
        dataframe["ema64"] = ta.EMA(dataframe, timeperiod=64)
        dataframe["ema192"] = ta.EMA(dataframe, timeperiod=192)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["atr_ratio"] = dataframe["atr"] / dataframe["close"]
        dataframe["atr_median96"] = dataframe["atr_ratio"].rolling(96).median().shift(1)
        dataframe["volume_mean32"] = dataframe["volume"].rolling(32).mean().shift(1)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        slow_regime = (
            (dataframe["ema64"] > dataframe["ema192"])
            & (dataframe["ema64"] > dataframe["ema64"].shift(16))
            & (dataframe["ema192"] > dataframe["ema192"].shift(16))
            & (dataframe["close"] > dataframe["ema192"])
        )
        pullback_recovery = (
            (dataframe["low"].shift(1) <= dataframe["ema16"].shift(1))
            & (dataframe["close"].shift(1) <= dataframe["ema16"].shift(1))
            & (dataframe["close"] > dataframe["ema16"])
            & (dataframe["close"] > dataframe["open"])
        )
        normal_volatility = (
            (dataframe["atr_ratio"] >= dataframe["atr_median96"] * 0.65)
            & (dataframe["atr_ratio"] <= dataframe["atr_median96"] * 1.8)
        )
        long_entries = (
            slow_regime
            & pullback_recovery
            & dataframe["rsi"].between(48.0, 66.0)
            & normal_volatility
            & (dataframe["volume"] >= dataframe["volume_mean32"] * 0.75)
            & (dataframe["volume"] > 0)
        )
        dataframe.loc[long_entries, "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        long_exits = (
            (dataframe["close"] < dataframe["ema64"])
            | (dataframe["ema64"] < dataframe["ema192"])
            | (dataframe["atr_ratio"] > dataframe["atr_median96"] * 2.5)
            | (dataframe["rsi"] > 78.0)
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
