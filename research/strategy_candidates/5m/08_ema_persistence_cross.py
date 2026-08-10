from functools import reduce

import talib.abstract as ta
from pandas import DataFrame
from freqtrade.strategy import IStrategy


class Candidate08EmaPersistenceCross5M(IStrategy):
    timeframe = '5m'
    stoploss = -0.1
    minimal_roi = {'0': 0.03}
    can_short = True
    startup_candle_count = 74

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe['ema_fast'] = ta.EMA(dataframe, timeperiod=18)
        dataframe['ema_slow'] = ta.EMA(dataframe, timeperiod=72)
        dataframe['rsi'] = ta.RSI(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        conditions = [
            (dataframe['ema_fast'] > dataframe['ema_slow']) & (dataframe['ema_fast'].shift(1) <= dataframe['ema_slow'].shift(1)),
            dataframe['rsi'] > 55.0,
        ]
        if conditions:
            dataframe.loc[reduce(lambda left, right: left & right, conditions), 'enter_long'] = 1
        short_conditions = [
            (dataframe['ema_fast'] < dataframe['ema_slow']) & (dataframe['ema_fast'].shift(1) >= dataframe['ema_slow'].shift(1)),
            dataframe['rsi'] < 45.0,
        ]
        if short_conditions:
            dataframe.loc[reduce(lambda left, right: left & right, short_conditions), 'enter_short'] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        conditions = [
            dataframe['ema_fast'] < dataframe['ema_slow'],
        ]
        if conditions:
            dataframe.loc[reduce(lambda left, right: left & right, conditions), 'exit_long'] = 1
        short_conditions = [
            dataframe['ema_fast'] > dataframe['ema_slow'],
        ]
        if short_conditions:
            dataframe.loc[reduce(lambda left, right: left & right, short_conditions), 'exit_short'] = 1
        return dataframe
