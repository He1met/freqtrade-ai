from functools import reduce

import talib.abstract as ta
from pandas import DataFrame
from freqtrade.strategy import IStrategy


class Candidate05FastEmaContinuation5M(IStrategy):
    timeframe = '5m'
    stoploss = -0.1
    minimal_roi = {'0': 0.03}
    can_short = True
    startup_candle_count = 102

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe['close_raw'] = dataframe['close']
        dataframe['ema_fast'] = ta.EMA(dataframe, timeperiod=20)
        dataframe['ema_regime'] = ta.EMA(dataframe, timeperiod=100)
        dataframe['rsi'] = ta.RSI(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        conditions = [
            (dataframe['close_raw'] > dataframe['ema_fast']) & (dataframe['close_raw'].shift(1) <= dataframe['ema_fast'].shift(1)),
            dataframe['ema_fast'] > dataframe['ema_regime'],
            dataframe['rsi'] > 50.0,
        ]
        if conditions:
            dataframe.loc[reduce(lambda left, right: left & right, conditions), 'enter_long'] = 1
        short_conditions = [
            (dataframe['close_raw'] < dataframe['ema_fast']) & (dataframe['close_raw'].shift(1) >= dataframe['ema_fast'].shift(1)),
            dataframe['ema_fast'] < dataframe['ema_regime'],
            dataframe['rsi'] < 50.0,
        ]
        if short_conditions:
            dataframe.loc[reduce(lambda left, right: left & right, short_conditions), 'enter_short'] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        conditions = [
            dataframe['close_raw'] < dataframe['ema_regime'],
        ]
        if conditions:
            dataframe.loc[reduce(lambda left, right: left & right, conditions), 'exit_long'] = 1
        short_conditions = [
            dataframe['close_raw'] > dataframe['ema_regime'],
        ]
        if short_conditions:
            dataframe.loc[reduce(lambda left, right: left & right, short_conditions), 'exit_short'] = 1
        return dataframe
