from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models.backtest import BacktestResult
from app.models.strategy_score import StrategyScore
from app.repositories import StrategyScoreRepository
from app.schemas import StrategyScoreCreate
from app.services.strategy_failure_reasons import StrategyFailureReasonService


PHASE1_SCORING_VERSION = "phase1-mvp-v1"
SCORING_VERSION = "phase2-quality-v1"

COMPONENT_WEIGHTS = {
    "profit_score": 0.35,
    "risk_score": 0.25,
    "stability_score": 0.15,
    "quality_score": 0.25,
}

QUALITY_WEIGHTS = {
    "trade_activity": 0.35,
    "backtest_completeness": 0.20,
    "validation": 0.15,
    "static_review": 0.20,
    "failure_history": 0.10,
}


class StrategyScoringService:
    """Calculates explainable Phase 2 ranking scores from local result data."""

    def __init__(self, db: Session, scoring_version: str = SCORING_VERSION) -> None:
        self.db = db
        self.repository = StrategyScoreRepository(db)
        self.scoring_version = scoring_version

    def score_backtest_result(self, backtest_result_id: int) -> Optional[StrategyScore]:
        result = self.db.get(BacktestResult, backtest_result_id)
        if result is None:
            return None

        strategy_version = result.run.strategy_version
        quality_context = self._quality_context(result)
        component_scores = self.calculate_component_scores(result, quality_context)
        elimination_result = self._elimination_result(result, quality_context)
        raw_total_score = self._weighted_score(component_scores)
        total_score = 0.0 if elimination_result["eliminated"] else raw_total_score
        metrics_snapshot = self._metrics_snapshot(
            result,
            component_scores,
            quality_context,
            elimination_result,
            raw_total_score,
        )

        return self.repository.save(
            StrategyScoreCreate(
                strategy_id=strategy_version.strategy_id,
                strategy_version_id=strategy_version.id,
                backtest_result_id=result.id,
                scoring_version=self.scoring_version,
                total_score=total_score,
                profit_score=component_scores["profit_score"],
                risk_score=component_scores["risk_score"],
                stability_score=component_scores["stability_score"],
                quality_score=component_scores["quality_score"],
                metrics_snapshot=metrics_snapshot,
            )
        )

    def calculate_component_scores(
        self,
        result: BacktestResult,
        quality_context: Optional[dict[str, Any]] = None,
    ) -> dict[str, float]:
        # Missing metrics score as zero instead of raising so ranking can stay
        # available while parsers or fixture data are incomplete.
        quality_context = quality_context or self._quality_context(result)
        profit_score = self._score_profit(result.profit_pct)
        risk_score = self._score_risk(result.max_drawdown_pct)
        stability_score = self._score_stability(result.win_rate)
        quality_score = self._score_quality(result, quality_context)
        return {
            "profit_score": profit_score,
            "risk_score": risk_score,
            "stability_score": stability_score,
            "quality_score": quality_score,
        }

    def _metrics_snapshot(
        self,
        result: BacktestResult,
        component_scores: dict[str, float],
        quality_context: dict[str, Any],
        elimination_result: dict[str, Any],
        raw_total_score: float,
    ) -> dict:
        missing_metrics = [
            name
            for name, value in (
                ("profit_pct", result.profit_pct),
                ("max_drawdown_pct", result.max_drawdown_pct),
                ("win_rate", result.win_rate),
                ("total_trades", result.total_trades),
            )
            if value is None
        ]
        score_breakdown = [
            {
                "name": name,
                "score": component_scores[name],
                "weight": weight,
                "contribution": round(component_scores[name] * weight, 6),
            }
            for name, weight in COMPONENT_WEIGHTS.items()
        ]
        return {
            "scoring_version": self.scoring_version,
            "source": "backtest_result",
            "backtest_result_id": result.id,
            "profit_pct": result.profit_pct,
            "max_drawdown_pct": result.max_drawdown_pct,
            "win_rate": result.win_rate,
            "total_trades": result.total_trades,
            "component_scores": component_scores,
            "component_weights": COMPONENT_WEIGHTS,
            "score_breakdown": score_breakdown,
            "raw_total_score": raw_total_score,
            "missing_metrics": missing_metrics,
            "quality_inputs": quality_context["quality_inputs"],
            "quality_breakdown": quality_context["quality_breakdown"],
            "warnings": quality_context["warnings"],
            "elimination": elimination_result,
        }

    def _weighted_score(self, component_scores: dict[str, float]) -> float:
        return round(
            sum(
                component_scores[name] * weight
                for name, weight in COMPONENT_WEIGHTS.items()
            ),
            6,
        )

    def _score_profit(self, profit_pct: Optional[float]) -> float:
        if profit_pct is None:
            return 0.0
        return self._clamp(profit_pct * 500.0 + 50.0)

    def _score_risk(self, max_drawdown_pct: Optional[float]) -> float:
        if max_drawdown_pct is None:
            return 0.0
        return self._clamp(100.0 - abs(max_drawdown_pct) * 500.0)

    def _score_stability(self, win_rate: Optional[float]) -> float:
        if win_rate is None:
            return 0.0
        return self._clamp(win_rate * 100.0)

    def _score_quality(
        self,
        result: BacktestResult,
        quality_context: dict[str, Any],
    ) -> float:
        total_trades = result.total_trades
        trade_activity_score = self._score_trade_activity(total_trades)
        missing_metrics = quality_context["quality_inputs"]["backtest"]["missing_metrics"]
        backtest_completeness_score = self._clamp(
            100.0 - (len(missing_metrics) / 4.0 * 100.0)
        )
        validation_score = quality_context["quality_breakdown"]["validation"]["score"]
        static_review_score = quality_context["quality_breakdown"]["static_review"]["score"]
        failure_history_score = quality_context["quality_breakdown"]["failure_history"]["score"]
        quality_score = (
            trade_activity_score * QUALITY_WEIGHTS["trade_activity"]
            + backtest_completeness_score * QUALITY_WEIGHTS["backtest_completeness"]
            + validation_score * QUALITY_WEIGHTS["validation"]
            + static_review_score * QUALITY_WEIGHTS["static_review"]
            + failure_history_score * QUALITY_WEIGHTS["failure_history"]
        )
        quality_context["quality_breakdown"]["trade_activity"] = {
            "score": trade_activity_score,
            "weight": QUALITY_WEIGHTS["trade_activity"],
            "total_trades": total_trades,
        }
        quality_context["quality_breakdown"]["backtest_completeness"] = {
            "score": backtest_completeness_score,
            "weight": QUALITY_WEIGHTS["backtest_completeness"],
            "missing_metrics": missing_metrics,
        }
        return self._clamp(quality_score)

    def _score_trade_activity(self, total_trades: Optional[int]) -> float:
        if total_trades is None or total_trades <= 0:
            return 0.0
        return self._clamp(total_trades / 30.0 * 100.0)

    def _clamp(self, value: float) -> float:
        return round(max(0.0, min(100.0, value)), 6)

    def _quality_context(self, result: BacktestResult) -> dict[str, Any]:
        snapshot = result.metrics_snapshot or {}
        validation = self._extract_mapping(snapshot, "validation", "validation_result")
        static_review = self._extract_mapping(
            snapshot,
            "static_review",
            "static_review_result",
        )
        failure_reasons = StrategyFailureReasonService(self.db).list_version_failures(
            result.run.strategy_version_id
        )
        missing_metrics = [
            name
            for name, value in (
                ("profit_pct", result.profit_pct),
                ("max_drawdown_pct", result.max_drawdown_pct),
                ("win_rate", result.win_rate),
                ("total_trades", result.total_trades),
            )
            if value is None
        ]
        validation_errors = list(validation.get("errors") or [])
        validation_warnings = list(validation.get("warnings") or [])
        static_findings = list(static_review.get("findings") or [])
        static_errors = [
            finding
            for finding in static_findings
            if isinstance(finding, dict) and finding.get("severity") == "error"
        ]
        static_warnings = [
            finding
            for finding in static_findings
            if isinstance(finding, dict) and finding.get("severity") == "warning"
        ]
        failure_error_count = sum(1 for reason in failure_reasons if reason.severity == "error")
        failure_warning_count = sum(
            1 for reason in failure_reasons if reason.severity == "warning"
        )

        warnings = self._quality_warnings(
            result,
            validation_warnings,
            static_warnings,
            failure_warning_count,
        )
        validation_score = self._quality_signal_score(
            error_count=len(validation_errors),
            warning_count=len(validation_warnings),
            passed=validation.get("passed"),
        )
        static_score = self._quality_signal_score(
            error_count=len(static_errors),
            warning_count=len(static_warnings),
            passed=static_review.get("passed"),
        )
        failure_history_score = self._quality_signal_score(
            error_count=failure_error_count,
            warning_count=failure_warning_count,
            passed=None,
        )

        return {
            "quality_inputs": {
                "backtest": {
                    "profit_pct": result.profit_pct,
                    "max_drawdown_pct": result.max_drawdown_pct,
                    "win_rate": result.win_rate,
                    "total_trades": result.total_trades,
                    "missing_metrics": missing_metrics,
                },
                "validation": validation,
                "static_review": static_review,
                "failure_reasons": [
                    {
                        "stage": reason.stage,
                        "reason_type": reason.reason_type,
                        "severity": reason.severity,
                        "message": reason.message,
                    }
                    for reason in failure_reasons
                ],
            },
            "quality_breakdown": {
                "validation": {
                    "score": validation_score,
                    "weight": QUALITY_WEIGHTS["validation"],
                    "error_count": len(validation_errors),
                    "warning_count": len(validation_warnings),
                },
                "static_review": {
                    "score": static_score,
                    "weight": QUALITY_WEIGHTS["static_review"],
                    "error_count": len(static_errors),
                    "warning_count": len(static_warnings),
                },
                "failure_history": {
                    "score": failure_history_score,
                    "weight": QUALITY_WEIGHTS["failure_history"],
                    "error_count": failure_error_count,
                    "warning_count": failure_warning_count,
                },
            },
            "validation_errors": validation_errors,
            "static_errors": static_errors,
            "failure_error_count": failure_error_count,
            "warnings": warnings,
        }

    def _elimination_result(
        self,
        result: BacktestResult,
        quality_context: dict[str, Any],
    ) -> dict[str, Any]:
        reasons: list[dict[str, Any]] = []
        missing_metrics = quality_context["quality_inputs"]["backtest"]["missing_metrics"]
        if len(missing_metrics) == 4:
            reasons.append(
                {
                    "code": "missing_backtest_metrics",
                    "severity": "error",
                    "message": "All core backtest metrics are missing.",
                    "details": {"missing_metrics": missing_metrics},
                }
            )
        if result.max_drawdown_pct is not None and result.max_drawdown_pct >= 0.35:
            reasons.append(
                {
                    "code": "max_drawdown_too_high",
                    "severity": "error",
                    "message": "Maximum drawdown is above the Phase 2 elimination threshold.",
                    "details": {"max_drawdown_pct": result.max_drawdown_pct, "threshold": 0.35},
                }
            )
        if result.total_trades is not None and result.total_trades < 3:
            reasons.append(
                {
                    "code": "too_few_trades",
                    "severity": "error",
                    "message": "Backtest has too few trades for ranking confidence.",
                    "details": {"total_trades": result.total_trades, "minimum": 3},
                }
            )
        if quality_context["validation_errors"]:
            reasons.append(
                {
                    "code": "validation_errors",
                    "severity": "error",
                    "message": "Strategy validation reported blocking errors.",
                    "details": {"errors": quality_context["validation_errors"]},
                }
            )
        if quality_context["static_errors"]:
            reasons.append(
                {
                    "code": "static_review_errors",
                    "severity": "error",
                    "message": "Static strategy review reported blocking findings.",
                    "details": {"findings": quality_context["static_errors"]},
                }
            )
        if quality_context["failure_error_count"]:
            reasons.append(
                {
                    "code": "recorded_failure_reasons",
                    "severity": "error",
                    "message": "Strategy version has recorded error-level failure reasons.",
                    "details": {"count": quality_context["failure_error_count"]},
                }
            )

        return {
            "eliminated": bool(reasons),
            "reasons": reasons,
        }

    def _quality_warnings(
        self,
        result: BacktestResult,
        validation_warnings: list[Any],
        static_warnings: list[Any],
        failure_warning_count: int,
    ) -> list[dict[str, Any]]:
        warnings: list[dict[str, Any]] = []
        if result.max_drawdown_pct is not None and result.max_drawdown_pct >= 0.20:
            warnings.append(
                {
                    "code": "elevated_drawdown",
                    "message": "Maximum drawdown is elevated but below elimination threshold.",
                    "details": {"max_drawdown_pct": result.max_drawdown_pct},
                }
            )
        if result.total_trades is not None and result.total_trades < 10:
            warnings.append(
                {
                    "code": "low_trade_count",
                    "message": "Backtest trade count is low for ranking confidence.",
                    "details": {"total_trades": result.total_trades},
                }
            )
        if result.win_rate is not None and result.win_rate < 0.35:
            warnings.append(
                {
                    "code": "low_win_rate",
                    "message": "Win rate is weak but not a blocking failure.",
                    "details": {"win_rate": result.win_rate},
                }
            )
        if validation_warnings:
            warnings.append(
                {
                    "code": "validation_warnings",
                    "message": "Strategy validation reported non-blocking warnings.",
                    "details": {"warnings": validation_warnings},
                }
            )
        if static_warnings:
            warnings.append(
                {
                    "code": "static_review_warnings",
                    "message": "Static strategy review reported non-blocking findings.",
                    "details": {"findings": static_warnings},
                }
            )
        if failure_warning_count:
            warnings.append(
                {
                    "code": "recorded_warning_reasons",
                    "message": "Strategy version has warning-level failure reason history.",
                    "details": {"count": failure_warning_count},
                }
            )
        return warnings

    def _quality_signal_score(
        self,
        error_count: int,
        warning_count: int,
        passed: Optional[bool],
    ) -> float:
        score = 100.0
        if passed is False and error_count == 0:
            error_count = 1
        score -= error_count * 40.0
        score -= warning_count * 15.0
        return self._clamp(score)

    def _extract_mapping(self, source: dict[str, Any], *keys: str) -> dict[str, Any]:
        for key in keys:
            value = source.get(key)
            if isinstance(value, dict):
                return value
        return {}
