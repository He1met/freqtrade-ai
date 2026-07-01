import json
from pathlib import Path
from typing import Any, Optional

from pydantic import ValidationError

from app.adapters.freqtrade.exceptions import FreqtradeResultParseError
from app.schemas.backtest import BacktestResultCreate


class FreqtradeResultParser:
    """Parses Freqtrade JSON reports into project-owned result DTOs.

    Freqtrade result keys can vary across versions and export modes, so the
    parser maps a small set of known aliases into the stable project schema.
    """

    def parse_backtest_result(
        self,
        result_path: Path,
        strategy_name: Optional[str] = None,
    ) -> BacktestResultCreate:
        payload = self._read_json(result_path)
        strategy_metrics = self._strategy_metrics(payload, strategy_name)
        try:
            return BacktestResultCreate(
                result_path=str(result_path),
                metrics_snapshot=strategy_metrics,
                profit_total=self._optional_float(
                    strategy_metrics,
                    "profit_total_abs",
                    "profit_total",
                    "profit_abs",
                ),
                profit_pct=self._optional_ratio(
                    strategy_metrics,
                    "profit_total_pct",
                    "profit_pct",
                    "profit_total",
                ),
                max_drawdown_pct=self._optional_ratio(
                    strategy_metrics,
                    "max_drawdown_pct",
                    "drawdown_pct",
                    "max_drawdown",
                ),
                win_rate=self._win_rate(strategy_metrics),
                total_trades=self._optional_int(
                    strategy_metrics,
                    "total_trades",
                    "trades_count",
                    "trade_count",
                ),
                timerange=self._timerange(strategy_metrics),
            )
        except ValidationError as exc:
            raise FreqtradeResultParseError(str(exc)) from exc

    def _read_json(self, result_path: Path) -> dict[str, Any]:
        if not result_path.exists():
            raise FreqtradeResultParseError(f"Result file does not exist: {result_path}")
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise FreqtradeResultParseError(f"Result file is not valid JSON: {result_path}") from exc
        if not isinstance(payload, dict):
            raise FreqtradeResultParseError("Freqtrade result root must be an object")
        return payload

    def _strategy_metrics(
        self,
        payload: dict[str, Any],
        strategy_name: Optional[str],
    ) -> dict[str, Any]:
        # Official backtesting exports usually nest metrics under "strategy".
        # Some tests and fixtures use a flattened single-strategy shape.
        strategies = payload.get("strategy")
        if isinstance(strategies, dict) and strategies:
            if strategy_name is not None:
                selected = strategies.get(strategy_name)
                if not isinstance(selected, dict):
                    raise FreqtradeResultParseError(
                        f"Strategy metrics not found for {strategy_name}"
                    )
                return selected

            if len(strategies) != 1:
                raise FreqtradeResultParseError(
                    "strategy_name is required when result contains multiple strategies"
                )
            selected = next(iter(strategies.values()))
            if isinstance(selected, dict):
                return selected

        if "strategy_name" in payload or "total_trades" in payload:
            return payload

        raise FreqtradeResultParseError("Freqtrade result does not contain strategy metrics")

    def _optional_float(self, metrics: dict[str, Any], *keys: str) -> Optional[float]:
        value = self._first_value(metrics, *keys)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise FreqtradeResultParseError(f"Metric must be numeric: {keys[0]}") from exc

    def _optional_ratio(self, metrics: dict[str, Any], *keys: str) -> Optional[float]:
        value = self._optional_float(metrics, *keys)
        if value is None:
            return None
        # Accept both 0.05 and 5.0 representations for percentage-like fields.
        if abs(value) > 1:
            return value / 100
        return value

    def _optional_int(self, metrics: dict[str, Any], *keys: str) -> Optional[int]:
        value = self._first_value(metrics, *keys)
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise FreqtradeResultParseError(f"Metric must be an integer: {keys[0]}") from exc

    def _win_rate(self, metrics: dict[str, Any]) -> Optional[float]:
        explicit = self._optional_ratio(metrics, "win_rate", "winrate")
        if explicit is not None:
            return explicit

        wins = self._optional_int(metrics, "wins")
        losses = self._optional_int(metrics, "losses")
        draws = self._optional_int(metrics, "draws")
        if wins is None or losses is None:
            return None
        total = wins + losses + (draws or 0)
        if total <= 0:
            return None
        return wins / total

    def _timerange(self, metrics: dict[str, Any]) -> Optional[str]:
        timerange = self._first_value(metrics, "timerange")
        if timerange is not None:
            return str(timerange)
        start = self._first_value(metrics, "backtest_start", "start_date")
        end = self._first_value(metrics, "backtest_end", "end_date")
        if start is None and end is None:
            return None
        return f"{start or ''}-{end or ''}"

    def _first_value(self, metrics: dict[str, Any], *keys: str) -> Any:
        for key in keys:
            value = metrics.get(key)
            if value is not None:
                return value
        return None
