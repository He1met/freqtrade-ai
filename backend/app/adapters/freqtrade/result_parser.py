import json
from pathlib import Path
from typing import Any, Optional

from pydantic import ValidationError

from app.adapters.freqtrade.exceptions import FreqtradeResultParseError
from app.schemas.backtest import BacktestResultCreate


CORE_METRIC_ALIASES = {
    "profit_total": (
        "profit_total_abs",
        "total_profit_abs",
        "profit_abs",
        "profit_total",
        "total_profit",
    ),
    "profit_pct": (
        "profit_total_pct",
        "total_profit_pct",
        "profit_pct",
        "profit_ratio",
        "profit_total",
    ),
    "max_drawdown_pct": (
        "max_drawdown_pct",
        "drawdown_pct",
        "max_relative_drawdown",
        "max_drawdown",
    ),
    "total_trades": (
        "total_trades",
        "trades_count",
        "trade_count",
        "closed_trades",
        "trades",
    ),
    "timerange": ("timerange",),
}

RISK_METRIC_ALIASES = {
    "sharpe": ("sharpe", "sharpe_ratio"),
    "sortino": ("sortino", "sortino_ratio"),
    "calmar": ("calmar", "calmar_ratio"),
    "sqn": ("sqn", "system_quality_number"),
    "expectancy_ratio": ("expectancy_ratio",),
}


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
        profit_total = self._optional_float(
            strategy_metrics,
            *CORE_METRIC_ALIASES["profit_total"],
        )
        profit_pct = self._optional_ratio(strategy_metrics, *CORE_METRIC_ALIASES["profit_pct"])
        max_drawdown_pct = self._optional_ratio(
            strategy_metrics,
            *CORE_METRIC_ALIASES["max_drawdown_pct"],
        )
        win_rate = self._win_rate(strategy_metrics)
        total_trades = self._optional_int(
            strategy_metrics,
            *CORE_METRIC_ALIASES["total_trades"],
        )
        timerange = self._timerange(strategy_metrics)
        risk_metrics = self._risk_metrics(strategy_metrics)
        normalized_metrics = {
            "profit_total": profit_total,
            "profit_pct": profit_pct,
            "max_drawdown_pct": max_drawdown_pct,
            "win_rate": win_rate,
            "total_trades": total_trades,
            "timerange": timerange,
            **risk_metrics,
        }
        metrics_snapshot = self._metrics_snapshot(strategy_metrics, normalized_metrics)
        try:
            return BacktestResultCreate(
                result_path=str(result_path),
                metrics_snapshot=metrics_snapshot,
                profit_total=profit_total,
                profit_pct=profit_pct,
                max_drawdown_pct=max_drawdown_pct,
                win_rate=win_rate,
                total_trades=total_trades,
                timerange=timerange,
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

        comparison = self._strategy_comparison_metrics(payload, strategy_name)
        if comparison is not None:
            return comparison

        if "strategy_name" in payload or self._contains_supported_metric(payload):
            return payload

        raise FreqtradeResultParseError("Freqtrade result does not contain strategy metrics")

    def _strategy_comparison_metrics(
        self,
        payload: dict[str, Any],
        strategy_name: Optional[str],
    ) -> Optional[dict[str, Any]]:
        comparison = payload.get("strategy_comparison")
        if not isinstance(comparison, list) or not comparison:
            return None

        rows = [row for row in comparison if isinstance(row, dict)]
        if strategy_name is not None:
            for row in rows:
                row_strategy = self._first_value(row, "key", "strategy", "strategy_name")
                if row_strategy == strategy_name:
                    return row
            raise FreqtradeResultParseError(f"Strategy metrics not found for {strategy_name}")

        if len(rows) != 1:
            raise FreqtradeResultParseError(
                "strategy_name is required when result contains multiple strategies"
            )
        return rows[0]

    def _contains_supported_metric(self, metrics: dict[str, Any]) -> bool:
        aliases = set()
        for keys in CORE_METRIC_ALIASES.values():
            aliases.update(keys)
        aliases.update(("win_rate", "winrate", "winning_rate", "wins", "winning_trades"))
        for keys in RISK_METRIC_ALIASES.values():
            aliases.update(keys)
        return any(metrics.get(key) is not None for key in aliases)

    def _optional_float(self, metrics: dict[str, Any], *keys: str) -> Optional[float]:
        value = self._first_value(metrics, *keys)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise FreqtradeResultParseError(
                f"Metric must be numeric for aliases {', '.join(keys)}"
            ) from exc

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
        explicit = self._optional_ratio(metrics, "win_rate", "winrate", "winning_rate")
        if explicit is not None:
            return explicit

        wins = self._optional_int(metrics, "wins", "winning_trades")
        losses = self._optional_int(metrics, "losses", "losing_trades")
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

    def _risk_metrics(self, metrics: dict[str, Any]) -> dict[str, Optional[float]]:
        return {
            metric_name: self._optional_float(metrics, *aliases)
            for metric_name, aliases in RISK_METRIC_ALIASES.items()
        }

    def _metrics_snapshot(
        self,
        raw_metrics: dict[str, Any],
        normalized_metrics: dict[str, Any],
    ) -> dict[str, Any]:
        snapshot = dict(raw_metrics)
        missing_metrics = self._missing_metrics(normalized_metrics)
        snapshot["normalized_metrics"] = normalized_metrics
        snapshot["parser_metadata"] = {
            "source": "freqtrade_result_parser",
            "missing_metrics": missing_metrics,
            "risk_metrics_available": [
                key for key in RISK_METRIC_ALIASES if normalized_metrics.get(key) is not None
            ],
            "risk_metrics_missing": [
                {
                    "metric": key,
                    "aliases": list(RISK_METRIC_ALIASES[key]),
                    "reason": "No supported risk metric field was present in result JSON.",
                }
                for key in RISK_METRIC_ALIASES
                if normalized_metrics.get(key) is None
            ],
        }
        return snapshot

    def _missing_metrics(self, normalized_metrics: dict[str, Any]) -> list[dict[str, Any]]:
        required_aliases = {
            **CORE_METRIC_ALIASES,
            "win_rate": ("win_rate", "winrate", "winning_rate", "wins/losses"),
        }
        return [
            {
                "metric": metric_name,
                "aliases": list(aliases),
                "reason": "No supported field was present in result JSON.",
            }
            for metric_name, aliases in required_aliases.items()
            if normalized_metrics.get(metric_name) is None
        ]

    def _first_value(self, metrics: dict[str, Any], *keys: str) -> Any:
        for key in keys:
            value = metrics.get(key)
            if value is not None:
                return value
        return None
