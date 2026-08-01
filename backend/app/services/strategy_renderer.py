from app.schemas.strategy_blueprint import SignalRule, StrategyBlueprint


class StrategyCodeRenderer:
    def render(self, blueprint: StrategyBlueprint) -> str:
        indicator_lines = self._render_indicators(blueprint)
        entry_conditions = self._render_conditions(blueprint.entry_rules)
        exit_conditions = self._render_conditions(blueprint.exit_rules)
        short_entry_conditions = self._render_conditions(
            blueprint.short_entry_rules
        )
        short_exit_conditions = self._render_conditions(
            blueprint.short_exit_rules
        )
        regime_lines = self._render_regime_masks(blueprint)

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
                f"    can_short = {blueprint.can_short!r}",
                f"    startup_candle_count = {self._startup_candle_count(blueprint)}",
                "",
                "    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:",
                *indicator_lines,
                "        return dataframe",
                "",
                "    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:",
                *regime_lines,
                "        conditions = [",
                *entry_conditions,
                "        ]",
                "        if conditions:",
                "            dataframe.loc[reduce(lambda left, right: left & right, conditions), 'enter_long'] = 1",
                "        short_conditions = [",
                *short_entry_conditions,
                "        ]",
                "        if short_conditions:",
                "            dataframe.loc[reduce(lambda left, right: left & right, short_conditions), 'enter_short'] = 1",
                "        return dataframe",
                "",
                "    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:",
                *regime_lines,
                "        conditions = [",
                *exit_conditions,
                "        ]",
                "        if conditions:",
                "            dataframe.loc[reduce(lambda left, right: left & right, conditions), 'exit_long'] = 1",
                "        short_conditions = [",
                *short_exit_conditions,
                "        ]",
                "        if short_conditions:",
                "            dataframe.loc[reduce(lambda left, right: left & right, short_conditions), 'exit_short'] = 1",
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
            f"            {self._render_rule_expression(rule)},"
            for rule in rules
        ]

    def _startup_candle_count(self, blueprint: StrategyBlueprint) -> int:
        max_period = max(indicator.period for indicator in blueprint.indicators)
        all_rules = [
            *blueprint.entry_rules,
            *blueprint.exit_rules,
            *blueprint.short_entry_rules,
            *blueprint.short_exit_rules,
            *[rule for item in blueprint.regime_rules for rule in item.rules],
        ]
        max_lookback = max((rule.lookback for rule in all_rules), default=1)
        return max(50, max_period + max_lookback + 1)

    def _render_regime_masks(self, blueprint: StrategyBlueprint) -> list[str]:
        if not blueprint.regime_rules:
            return []
        lines = [
            "        regime_masks = {",
        ]
        for regime_rule in blueprint.regime_rules:
            expressions = [
                self._render_rule_expression(rule)
                for rule in regime_rule.rules
            ]
            lines.extend(
                [
                    f"            {regime_rule.regime!r}: reduce(lambda left, right: left & right, [",
                    *[f"                {expression}," for expression in expressions],
                    "            ]),",
                ]
            )
        lines.append("        }")
        return lines

    def _render_rule_expression(self, rule: SignalRule) -> str:
        indicator = f"dataframe[{rule.indicator!r}]"
        if rule.operator in {"<", "<=", ">", ">=", "=="}:
            if rule.compare_indicator is not None:
                right = f"dataframe[{rule.compare_indicator!r}]"
            else:
                right = repr(rule.value)
            expression = f"{indicator} {rule.operator} {right}"
        elif rule.operator == "crosses_above":
            compare = f"dataframe[{rule.compare_indicator!r}]"
            lag = rule.lookback
            expression = (
                f"({indicator} > {compare}) & "
                f"({indicator}.shift({lag}) <= {compare}.shift({lag}))"
            )
        elif rule.operator == "crosses_below":
            compare = f"dataframe[{rule.compare_indicator!r}]"
            lag = rule.lookback
            expression = (
                f"({indicator} < {compare}) & "
                f"({indicator}.shift({lag}) >= {compare}.shift({lag}))"
            )
        elif rule.operator == "rising":
            expression = f"{indicator} > {indicator}.shift({rule.lookback})"
        elif rule.operator == "falling":
            expression = f"{indicator} < {indicator}.shift({rule.lookback})"
        else:  # pragma: no cover - Pydantic validates the operator vocabulary.
            raise ValueError(f"unsupported signal operator: {rule.operator}")
        if rule.regime is not None:
            expression = f"({expression}) & regime_masks[{rule.regime!r}]"
        return expression
