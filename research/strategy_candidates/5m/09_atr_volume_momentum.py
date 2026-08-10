from functools import reduce

import talib.abstract as ta
from pandas import DataFrame
from freqtrade.strategy import IStrategy


class Candidate09AtrVolumeMomentum5M(IStrategy):
    timeframe = '5m'
    stoploss = -0.1
    minimal_roi = {'0': 0.03}
    can_short = True
    startup_candle_count = 104

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe['close_raw'] = dataframe['close']
        dataframe['volume_raw'] = dataframe['volume']
        dataframe['volume_mean'] = ta.SMA(dataframe['volume'], timeperiod=48)
        dataframe['atr'] = ta.ATR(dataframe, timeperiod=14)
        dataframe['ema_fast'] = ta.EMA(dataframe, timeperiod=34)
        dataframe['ema_slow'] = ta.EMA(dataframe, timeperiod=100)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        conditions = [
            dataframe['volume_raw'] > dataframe['volume_mean'],
            dataframe['atr'] > dataframe['atr'].shift(3),
            dataframe['ema_fast'] > dataframe['ema_slow'],
            dataframe['close_raw'] > dataframe['ema_fast'],
        ]
        if conditions:
            dataframe.loc[reduce(lambda left, right: left & right, conditions), 'enter_long'] = 1
        short_conditions = [
            dataframe['volume_raw'] > dataframe['volume_mean'],
            dataframe['atr'] > dataframe['atr'].shift(3),
            dataframe['ema_fast'] < dataframe['ema_slow'],
            dataframe['close_raw'] < dataframe['ema_fast'],
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
