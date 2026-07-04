import json
from dataclasses import dataclass
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


@dataclass(frozen=True)
class HyperoptResultParsed:
    result_path: str
    strategy_name: str
    best_epoch: int
    loss: float
    score: Optional[float]
    is_best: bool
    spaces: list[str]
    best_params: dict[str, Any]
    metrics_snapshot: dict[str, Any]


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

    def parse_hyperopt_result(
        self,
        result_path: Path,
        strategy_name: Optional[str] = None,
    ) -> HyperoptResultParsed:
        payload = self._read_json(result_path)
        best_result = self._hyperopt_best_result(payload)
        best_epoch = self._required_int(
            best_result,
            "epoch",
            "current_epoch",
            "best_epoch",
        )
        loss = self._required_float(best_result, "loss", "loss_value", "objective")
        score = self._optional_float(best_result, "score", "profit_score", "objective_score")
        best_params = self._hyperopt_best_params(best_result)
        selected_strategy_name = self._hyperopt_strategy_name(
            payload,
            best_result,
            strategy_name,
        )
        spaces = self._hyperopt_spaces(payload, best_result, best_params)
        metrics = self._hyperopt_metrics(best_result)
        normalized_metrics = {
            "profit_total": self._optional_float(
                metrics,
                *CORE_METRIC_ALIASES["profit_total"],
            ),
            "profit_pct": self._optional_ratio(metrics, *CORE_METRIC_ALIASES["profit_pct"]),
            "max_drawdown_pct": self._optional_ratio(
                metrics,
                *CORE_METRIC_ALIASES["max_drawdown_pct"],
            ),
            "win_rate": self._win_rate(metrics),
            "total_trades": self._optional_int(
                metrics,
                *CORE_METRIC_ALIASES["total_trades"],
            ),
            **self._risk_metrics(metrics),
        }
        metrics_snapshot = dict(metrics)
        metrics_snapshot["normalized_metrics"] = normalized_metrics
        metrics_snapshot["parser_metadata"] = {
            "source": "freqtrade_hyperopt_result_parser",
            "missing_metrics": self._missing_hyperopt_metrics(normalized_metrics),
            "best_result_shape": self._hyperopt_result_shape(payload),
            "loss": loss,
            "score": score,
            "best_epoch": best_epoch,
            "spaces": spaces,
        }
        return HyperoptResultParsed(
            result_path=str(result_path),
            strategy_name=selected_strategy_name,
            best_epoch=best_epoch,
            loss=loss,
            score=score,
            is_best=self._hyperopt_is_best(best_result),
            spaces=spaces,
            best_params=best_params,
            metrics_snapshot=metrics_snapshot,
        )

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

    def _required_float(self, metrics: dict[str, Any], *keys: str) -> float:
        value = self._optional_float(metrics, *keys)
        if value is None:
            raise FreqtradeResultParseError(
                f"Required numeric field missing for aliases {', '.join(keys)}"
            )
        return value

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

    def _required_int(self, metrics: dict[str, Any], *keys: str) -> int:
        value = self._optional_int(metrics, *keys)
        if value is None:
            raise FreqtradeResultParseError(
                f"Required integer field missing for aliases {', '.join(keys)}"
            )
        return value

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

    def _hyperopt_best_result(self, payload: dict[str, Any]) -> dict[str, Any]:
        direct = payload.get("best_result")
        if isinstance(direct, dict):
            return direct

        for key in ("results", "epochs", "hyperopt_results"):
            rows = payload.get(key)
            if not isinstance(rows, list):
                continue
            candidates = [row for row in rows if isinstance(row, dict)]
            for row in candidates:
                if self._hyperopt_is_best(row):
                    return row
            raise FreqtradeResultParseError("Freqtrade hyperopt result has no best epoch")

        if self._contains_hyperopt_result(payload):
            return payload

        raise FreqtradeResultParseError("Freqtrade hyperopt result has no best result")

    def _contains_hyperopt_result(self, result: dict[str, Any]) -> bool:
        return any(
            result.get(key) is not None
            for key in (
                "params",
                "best_params",
                "params_dict",
                "params_details",
            )
        )

    def _hyperopt_result_shape(self, payload: dict[str, Any]) -> str:
        if isinstance(payload.get("best_result"), dict):
            return "best_result"
        for key in ("results", "epochs", "hyperopt_results"):
            if isinstance(payload.get(key), list):
                return key
        return "direct"

    def _hyperopt_strategy_name(
        self,
        payload: dict[str, Any],
        best_result: dict[str, Any],
        strategy_name: Optional[str],
    ) -> str:
        selected = strategy_name or self._first_value(
            best_result,
            "strategy_name",
            "strategy",
            "key",
        )
        if selected is None:
            selected = self._first_value(payload, "strategy_name", "strategy", "key")
        if selected is None:
            raise FreqtradeResultParseError("Freqtrade hyperopt result is missing strategy name")
        return str(selected)

    def _hyperopt_best_params(self, best_result: dict[str, Any]) -> dict[str, Any]:
        params = self._first_value(
            best_result,
            "best_params",
            "params",
            "params_details",
            "params_dict",
        )
        if not isinstance(params, dict) or not params:
            raise FreqtradeResultParseError("Freqtrade hyperopt best params must be a non-empty object")
        return dict(params)

    def _hyperopt_spaces(
        self,
        payload: dict[str, Any],
        best_result: dict[str, Any],
        best_params: dict[str, Any],
    ) -> list[str]:
        spaces = self._first_value(best_result, "spaces", "space")
        if spaces is None:
            spaces = self._first_value(payload, "spaces", "space")
        if isinstance(spaces, str):
            return [spaces]
        if isinstance(spaces, list) and all(isinstance(space, str) for space in spaces):
            return list(spaces)
        inferred_spaces = [
            key
            for key, value in best_params.items()
            if isinstance(key, str) and isinstance(value, (dict, list, str, int, float, bool))
        ]
        return inferred_spaces

    def _hyperopt_metrics(self, best_result: dict[str, Any]) -> dict[str, Any]:
        metrics = self._first_value(best_result, "results_metrics", "metrics", "result_metrics")
        if isinstance(metrics, dict):
            snapshot = dict(metrics)
        else:
            snapshot = {}
        for key, value in best_result.items():
            if key not in {
                "best_params",
                "params",
                "params_details",
                "params_dict",
                "results_metrics",
                "metrics",
                "result_metrics",
            }:
                snapshot.setdefault(key, value)
        return snapshot

    def _hyperopt_is_best(self, best_result: dict[str, Any]) -> bool:
        value = self._first_value(best_result, "is_best", "best")
        return True if value is None else bool(value)

    def _missing_hyperopt_metrics(
        self,
        normalized_metrics: dict[str, Any],
    ) -> list[dict[str, Any]]:
        required_aliases = {
            "profit_total": CORE_METRIC_ALIASES["profit_total"],
            "profit_pct": CORE_METRIC_ALIASES["profit_pct"],
            "max_drawdown_pct": CORE_METRIC_ALIASES["max_drawdown_pct"],
            "win_rate": ("win_rate", "winrate", "winning_rate", "wins/losses"),
            "total_trades": CORE_METRIC_ALIASES["total_trades"],
        }
        return [
            {
                "metric": metric_name,
                "aliases": list(aliases),
                "reason": "No supported field was present in Hyperopt result JSON.",
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
