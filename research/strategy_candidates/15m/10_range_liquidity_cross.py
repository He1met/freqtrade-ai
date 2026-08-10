from functools import reduce

import talib.abstract as ta
from pandas import DataFrame
from freqtrade.strategy import IStrategy


class Candidate10RangeLiquidityCross15M(IStrategy):
    timeframe = '15m'
    stoploss = -0.1
    minimal_roi = {'0': 0.03}
    can_short = True
    startup_candle_count = 74

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe['close_raw'] = dataframe['close']
        dataframe['volume_raw'] = dataframe['volume']
        dataframe['volume_mean'] = ta.SMA(dataframe['volume'], timeperiod=72)
        dataframe['range_mean'] = ta.SMA(dataframe, timeperiod=32)
        dataframe['rsi'] = ta.RSI(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        conditions = [
            dataframe['volume_raw'] > dataframe['volume_mean'],
            (dataframe['close_raw'] > dataframe['range_mean']) & (dataframe['close_raw'].shift(1) <= dataframe['range_mean'].shift(1)),
            dataframe['rsi'] < 60.0,
        ]
        if conditions:
            dataframe.loc[reduce(lambda left, right: left & right, conditions), 'enter_long'] = 1
        short_conditions = [
            dataframe['volume_raw'] > dataframe['volume_mean'],
            (dataframe['close_raw'] < dataframe['range_mean']) & (dataframe['close_raw'].shift(1) >= dataframe['range_mean'].shift(1)),
            dataframe['rsi'] > 40.0,
        ]
        if short_conditions:
            dataframe.loc[reduce(lambda left, right: left & right, short_conditions), 'enter_short'] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        conditions = [
            dataframe['rsi'] > 65.0,
        ]
        if conditions:
            dataframe.loc[reduce(lambda left, right: left & right, conditions), 'exit_long'] = 1
        short_conditions = [
            dataframe['rsi'] < 35.0,
        ]
        if short_conditions:
            dataframe.loc[reduce(lambda left, right: left & right, short_conditions), 'exit_short'] = 1
        return dataframe
