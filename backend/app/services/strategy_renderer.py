from app.schemas.strategy_blueprint import SignalRule, StrategyBlueprint


class StrategyCodeRenderer:
    def render(self, blueprint: StrategyBlueprint) -> str:
        indicator_lines = self._render_indicators(blueprint)
        entry_conditions = self._render_conditions(blueprint.entry_rules)
        exit_conditions = self._render_conditions(blueprint.exit_rules)

        return "\n".join(
            [
                "from functools import reduce",
                "",
                "import talib.abstract as ta",
                "from pandas import DataFrame",
                "from freqtrade.strategy import IStrategy",
                "",
                "",
                f"class {blueprint.class_name}(IStrategy):",
                f"    timeframe = {blueprint.timeframe!r}",
                f"    stoploss = {blueprint.stoploss!r}",
                f"    minimal_roi = {blueprint.minimal_roi!r}",
                "    can_short = False",
                "    startup_candle_count = 50",
                "",
                "    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:",
                *indicator_lines,
                "        return dataframe",
                "",
                "    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:",
                "        conditions = [",
                *entry_conditions,
                "        ]",
                "        if conditions:",
                "            dataframe.loc[reduce(lambda left, right: left & right, conditions), 'enter_long'] = 1",
                "        return dataframe",
                "",
                "    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:",
                "        conditions = [",
                *exit_conditions,
                "        ]",
                "        if conditions:",
                "            dataframe.loc[reduce(lambda left, right: left & right, conditions), 'exit_long'] = 1",
                "        return dataframe",
                "",
            ]
        )

    def _render_indicators(self, blueprint: StrategyBlueprint) -> list[str]:
        lines: list[str] = []
        for indicator in blueprint.indicators:
            if indicator.kind == "rsi":
                lines.append(
                    f"        dataframe[{indicator.name!r}] = ta.RSI(dataframe, timeperiod={indicator.period})"
                )
            elif indicator.kind == "ema":
                lines.append(
                    f"        dataframe[{indicator.name!r}] = ta.EMA(dataframe, timeperiod={indicator.period})"
                )
            elif indicator.kind == "sma":
                lines.append(
                    f"        dataframe[{indicator.name!r}] = ta.SMA(dataframe, timeperiod={indicator.period})"
                )
        return lines

    def _render_conditions(self, rules: list[SignalRule]) -> list[str]:
        return [
            f"            dataframe[{rule.indicator!r}] {rule.operator} {rule.value!r},"
            for rule in rules
        ]
