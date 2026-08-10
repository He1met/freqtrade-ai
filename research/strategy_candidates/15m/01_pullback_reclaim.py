from functools import reduce

import talib.abstract as ta
from pandas import DataFrame
from freqtrade.strategy import IStrategy


class Candidate01PullbackReclaim15M(IStrategy):
    timeframe = '15m'
    stoploss = -0.1
    minimal_roi = {'0': 0.03}
    can_short = True
    startup_candle_count = 98

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe['close_raw'] = dataframe['close']
        dataframe['ema_mid'] = ta.EMA(dataframe, timeperiod=32)
        dataframe['ema_slow'] = ta.EMA(dataframe, timeperiod=96)
        dataframe['rsi'] = ta.RSI(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        conditions = [
            (dataframe['close_raw'] > dataframe['ema_mid']) & (dataframe['close_raw'].shift(1) <= dataframe['ema_mid'].shift(1)),
            dataframe['ema_mid'] > dataframe['ema_slow'],
            dataframe['rsi'] > 52.0,
        ]
        if conditions:
            dataframe.loc[reduce(lambda left, right: left & right, conditions), 'enter_long'] = 1
        short_conditions = [
            (dataframe['close_raw'] < dataframe['ema_mid']) & (dataframe['close_raw'].shift(1) >= dataframe['ema_mid'].shift(1)),
            dataframe['ema_mid'] < dataframe['ema_slow'],
            dataframe['rsi'] < 48.0,
        ]
        if short_conditions:
            dataframe.loc[reduce(lambda left, right: left & right, short_conditions), 'enter_short'] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        conditions = [
            dataframe['ema_mid'] < dataframe['ema_slow'],
        ]
        if conditions:
            dataframe.loc[reduce(lambda left, right: left & right, conditions), 'exit_long'] = 1
        short_conditions = [
            dataframe['ema_mid'] > dataframe['ema_slow'],
        ]
        if short_conditions:
            dataframe.loc[reduce(lambda left, right: left & right, short_conditions), 'exit_short'] = 1
        return dataframe
