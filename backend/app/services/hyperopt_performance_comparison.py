from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional, Union

from app.schemas.backtest import BacktestResultCreate


HyperoptComparisonStatus = Literal["IMPROVED", "REGRESSED", "MIXED", "UNCHANGED", "BLOCKED"]
MetricImpact = Literal["improved", "regressed", "changed", "unchanged", "missing"]

CORE_METRICS = (
    "profit_total",
    "profit_pct",
    "max_drawdown_pct",
    "win_rate",
    "total_trades",
)
RISK_METRICS = (
    "sharpe",
    "sortino",
    "calmar",
    "sqn",
    "expectancy_ratio",
)
COMPARABLE_METRICS = CORE_METRICS + RISK_METRICS
HIGHER_IS_BETTER = {
    "profit_total",
    "profit_pct",
    "win_rate",
    "sharpe",
    "sortino",
    "calmar",
    "sqn",
    "expectancy_ratio",
}
LOWER_IS_BETTER = {"max_drawdown_pct"}


@dataclass(frozen=True)
class HyperoptMetricDelta:
    metric: str
    before: Optional[float]
    after: Optional[float]
    delta: Optional[float]
    delta_pct: Optional[float]
    impact: MetricImpact

    def to_dict(self) -> dict[str, object]:
        return {
            "metric": self.metric,
            "before": self.before,
            "after": self.after,
            "delta": self.delta,
            "delta_pct": self.delta_pct,
            "impact": self.impact,
        }


@dataclass(frozen=True)
class HyperoptPerformanceComparison:
    status: HyperoptComparisonStatus
    before_metrics: dict[str, Optional[float]] = field(default_factory=dict)
    after_metrics: dict[str, Optional[float]] = field(default_factory=dict)
    metric_deltas: list[HyperoptMetricDelta] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    elimination_signals: list[str] = field(default_factory=list)
    blocked_reason: Optional[str] = None

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "before_metrics": dict(self.before_metrics),
            "after_metrics": dict(self.after_metrics),
            "metric_deltas": [delta.to_dict() for delta in self.metric_deltas],
            "warnings": list(self.warnings),
            "elimination_signals": list(self.elimination_signals),
            "blocked_reason": self.blocked_reason,
        }


class HyperoptPerformanceComparisonService:
    """Compares pre/post Hyperopt fixture metrics without running trading workflows."""

    def compare(
        self,
        *,
        before_result: Optional[Union[BacktestResultCreate, dict[str, Any]]],
        after_result: Optional[Union[BacktestResultCreate, dict[str, Any]]],
    ) -> HyperoptPerformanceComparison:
        if before_result is None:
            return HyperoptPerformanceComparison(
                status="BLOCKED",
                blocked_reason="BLOCKED: before backtest result is required",
            )
        if after_result is None:
            return HyperoptPerformanceComparison(
                status="BLOCKED",
                blocked_reason="BLOCKED: after Hyperopt backtest result is required",
            )

        before = self._validate_result(before_result)
        after = self._validate_result(after_result)
        before_metrics = self._metrics(before)
        after_metrics = self._metrics(after)
        if not self._has_shared_metric(before_metrics, after_metrics):
            return HyperoptPerformanceComparison(
                status="BLOCKED",
                before_metrics=before_metrics,
                after_metrics=after_metrics,
                blocked_reason="BLOCKED: no shared before/after metrics are available",
            )

        metric_deltas = [
            self._metric_delta(metric, before_metrics[metric], after_metrics[metric])
            for metric in COMPARABLE_METRICS
        ]
        warnings = self._warnings(metric_deltas)
        elimination_signals = self._elimination_signals(metric_deltas, after_metrics)

        return HyperoptPerformanceComparison(
            status=self._status(metric_deltas, warnings, elimination_signals),
            before_metrics=before_metrics,
            after_metrics=after_metrics,
            metric_deltas=metric_deltas,
            warnings=warnings,
            elimination_signals=elimination_signals,
        )

    def _validate_result(
        self,
        result: Union[BacktestResultCreate, dict[str, Any]],
    ) -> BacktestResultCreate:
        if isinstance(result, BacktestResultCreate):
            return result
        return BacktestResultCreate.model_validate(result)

    def _metrics(self, result: BacktestResultCreate) -> dict[str, Optional[float]]:
        normalized = result.metrics_snapshot.get("normalized_metrics")
        if not isinstance(normalized, dict):
            normalized = {}
        return {
            "profit_total": self._float_or_none(result.profit_total),
            "profit_pct": self._float_or_none(result.profit_pct),
            "max_drawdown_pct": self._float_or_none(result.max_drawdown_pct),
            "win_rate": self._float_or_none(result.win_rate),
            "total_trades": self._float_or_none(result.total_trades),
            **{
                metric: self._float_or_none(normalized.get(metric))
                for metric in RISK_METRICS
            },
        }

    def _float_or_none(self, value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _has_shared_metric(
        self,
        before_metrics: dict[str, Optional[float]],
        after_metrics: dict[str, Optional[float]],
    ) -> bool:
        return any(
            before_metrics.get(metric) is not None and after_metrics.get(metric) is not None
            for metric in COMPARABLE_METRICS
        )

    def _metric_delta(
        self,
        metric: str,
        before_value: Optional[float],
        after_value: Optional[float],
    ) -> HyperoptMetricDelta:
        if before_value is None or after_value is None:
            return HyperoptMetricDelta(
                metric=metric,
                before=before_value,
                after=after_value,
                delta=None,
                delta_pct=None,
                impact="missing",
            )

        delta = after_value - before_value
        if abs(delta) <= 1e-12:
            impact: MetricImpact = "unchanged"
        elif metric in HIGHER_IS_BETTER:
            impact = "improved" if delta > 0 else "regressed"
        elif metric in LOWER_IS_BETTER:
            impact = "improved" if delta < 0 else "regressed"
        else:
            impact = "changed"

        return HyperoptMetricDelta(
            metric=metric,
            before=before_value,
            after=after_value,
            delta=delta,
            delta_pct=self._delta_pct(before_value, delta),
            impact=impact,
        )

    def _delta_pct(self, before_value: float, delta: float) -> Optional[float]:
        if abs(before_value) <= 1e-12:
            return None
        return delta / abs(before_value)

    def _warnings(self, metric_deltas: list[HyperoptMetricDelta]) -> list[str]:
        warnings: list[str] = []
        for delta in metric_deltas:
            if delta.impact != "regressed":
                continue
            if delta.metric == "max_drawdown_pct":
                warnings.append(
                    "Risk worsened: max_drawdown_pct increased "
                    f"from {delta.before} to {delta.after}."
                )
            elif delta.metric in RISK_METRICS:
                warnings.append(
                    f"Risk metric worsened: {delta.metric} changed "
                    f"from {delta.before} to {delta.after}."
                )
            else:
                warnings.append(
                    f"Performance metric worsened: {delta.metric} changed "
                    f"from {delta.before} to {delta.after}."
                )
        return warnings

    def _elimination_signals(
        self,
        metric_deltas: list[HyperoptMetricDelta],
        after_metrics: dict[str, Optional[float]],
    ) -> list[str]:
        signals: list[str] = []
        after_profit_pct = after_metrics.get("profit_pct")
        after_trades = after_metrics.get("total_trades")
        if after_profit_pct is not None and after_profit_pct <= 0:
            signals.append("after_profit_pct_non_positive")
        if after_trades is not None and after_trades <= 0:
            signals.append("after_total_trades_zero")
        for delta in metric_deltas:
            if delta.metric == "max_drawdown_pct" and delta.impact == "regressed":
                signals.append("max_drawdown_worsened")
            if delta.metric in RISK_METRICS and delta.impact == "regressed":
                signals.append(f"{delta.metric}_worsened")
        return signals

    def _status(
        self,
        metric_deltas: list[HyperoptMetricDelta],
        warnings: list[str],
        elimination_signals: list[str],
    ) -> HyperoptComparisonStatus:
        has_improvement = any(delta.impact == "improved" for delta in metric_deltas)
        has_regression = bool(warnings or elimination_signals)
        has_change = any(
            delta.impact in {"improved", "regressed", "changed"} for delta in metric_deltas
        )
        if has_improvement and has_regression:
            return "MIXED"
        if has_regression:
            return "REGRESSED"
        if has_improvement:
            return "IMPROVED"
        if has_change:
            return "MIXED"
        return "UNCHANGED"
