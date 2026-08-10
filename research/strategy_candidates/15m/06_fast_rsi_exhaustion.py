from functools import reduce

import talib.abstract as ta
from pandas import DataFrame
from freqtrade.strategy import IStrategy


class Candidate06FastRsiExhaustion15M(IStrategy):
    timeframe = '15m'
    stoploss = -0.1
    minimal_roi = {'0': 0.03}
    can_short = True
    startup_candle_count = 98

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe['close_raw'] = dataframe['close']
        dataframe['mean'] = ta.SMA(dataframe, timeperiod=96)
        dataframe['rsi_fast'] = ta.RSI(dataframe, timeperiod=7)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        conditions = [
            dataframe['close_raw'] < dataframe['mean'],
            dataframe['rsi_fast'] < 22.0,
        ]
        if conditions:
            dataframe.loc[reduce(lambda left, right: left & right, conditions), 'enter_long'] = 1
        short_conditions = [
            dataframe['close_raw'] > dataframe['mean'],
            dataframe['rsi_fast'] > 78.0,
        ]
        if short_conditions:
            dataframe.loc[reduce(lambda left, right: left & right, short_conditions), 'enter_short'] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        conditions = [
            dataframe['rsi_fast'] > 50.0,
        ]
        if conditions:
            dataframe.loc[reduce(lambda left, right: left & right, conditions), 'exit_long'] = 1
        short_conditions = [
            dataframe['rsi_fast'] < 50.0,
        ]
        if short_conditions:
            dataframe.loc[reduce(lambda left, right: left & right, short_conditions), 'exit_short'] = 1
        return dataframe
