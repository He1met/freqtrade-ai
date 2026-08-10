from functools import reduce

import talib.abstract as ta
from pandas import DataFrame
from freqtrade.strategy import IStrategy


class Candidate07ParticipationMomentum5M(IStrategy):
    timeframe = '5m'
    stoploss = -0.1
    minimal_roi = {'0': 0.03}
    can_short = True
    startup_candle_count = 84

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe['close_raw'] = dataframe['close']
        dataframe['volume_raw'] = dataframe['volume']
        dataframe['volume_mean'] = ta.SMA(dataframe['volume'], timeperiod=24)
        dataframe['ema'] = ta.EMA(dataframe, timeperiod=80)
        dataframe['rsi_fast'] = ta.RSI(dataframe, timeperiod=10)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        conditions = [
            dataframe['volume_raw'] > dataframe['volume_mean'],
            dataframe['rsi_fast'] > dataframe['rsi_fast'].shift(3),
            dataframe['rsi_fast'] > 55.0,
            dataframe['close_raw'] > dataframe['ema'],
        ]
        if conditions:
            dataframe.loc[reduce(lambda left, right: left & right, conditions), 'enter_long'] = 1
        short_conditions = [
            dataframe['volume_raw'] > dataframe['volume_mean'],
            dataframe['rsi_fast'] < dataframe['rsi_fast'].shift(3),
            dataframe['rsi_fast'] < 45.0,
            dataframe['close_raw'] < dataframe['ema'],
        ]
        if short_conditions:
            dataframe.loc[reduce(lambda left, right: left & right, short_conditions), 'enter_short'] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        conditions = [
            dataframe['rsi_fast'] < 50.0,
        ]
        if conditions:
            dataframe.loc[reduce(lambda left, right: left & right, conditions), 'exit_long'] = 1
        short_conditions = [
            dataframe['rsi_fast'] > 50.0,
        ]
        if short_conditions:
            dataframe.loc[reduce(lambda left, right: left & right, short_conditions), 'exit_short'] = 1
        return dataframe
