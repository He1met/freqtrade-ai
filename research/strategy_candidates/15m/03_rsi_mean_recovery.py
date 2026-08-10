from functools import reduce

import talib.abstract as ta
from pandas import DataFrame
from freqtrade.strategy import IStrategy


class Candidate03RsiMeanRecovery15M(IStrategy):
    timeframe = '15m'
    stoploss = -0.1
    minimal_roi = {'0': 0.03}
    can_short = True
    startup_candle_count = 51

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe['close_raw'] = dataframe['close']
        dataframe['mean'] = ta.SMA(dataframe, timeperiod=48)
        dataframe['rsi'] = ta.RSI(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        conditions = [
            dataframe['close_raw'] < dataframe['mean'],
            dataframe['rsi'] < 30.0,
            dataframe['rsi'] > dataframe['rsi'].shift(2),
        ]
        if conditions:
            dataframe.loc[reduce(lambda left, right: left & right, conditions), 'enter_long'] = 1
        short_conditions = [
            dataframe['close_raw'] > dataframe['mean'],
            dataframe['rsi'] > 70.0,
            dataframe['rsi'] < dataframe['rsi'].shift(2),
        ]
        if short_conditions:
            dataframe.loc[reduce(lambda left, right: left & right, short_conditions), 'enter_short'] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        conditions = [
            dataframe['close_raw'] >= dataframe['mean'],
        ]
        if conditions:
            dataframe.loc[reduce(lambda left, right: left & right, conditions), 'exit_long'] = 1
        short_conditions = [
            dataframe['close_raw'] <= dataframe['mean'],
        ]
        if short_conditions:
            dataframe.loc[reduce(lambda left, right: left & right, short_conditions), 'exit_short'] = 1
        return dataframe
