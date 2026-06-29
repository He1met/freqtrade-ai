from typing import Optional

from sqlalchemy.orm import Session

from app.models.backtest import BacktestResult
from app.models.strategy_score import StrategyScore
from app.repositories import StrategyScoreRepository
from app.schemas import StrategyScoreCreate


SCORING_VERSION = "phase1-mvp-v1"


class StrategyScoringService:
    def __init__(self, db: Session, scoring_version: str = SCORING_VERSION) -> None:
        self.db = db
        self.repository = StrategyScoreRepository(db)
        self.scoring_version = scoring_version

    def score_backtest_result(self, backtest_result_id: int) -> Optional[StrategyScore]:
        result = self.db.get(BacktestResult, backtest_result_id)
        if result is None:
            return None

        strategy_version = result.run.strategy_version
        component_scores = self.calculate_component_scores(result)
        total_score = round(
            component_scores["profit_score"] * 0.40
            + component_scores["risk_score"] * 0.25
            + component_scores["stability_score"] * 0.20
            + component_scores["quality_score"] * 0.15,
            6,
        )
        metrics_snapshot = self._metrics_snapshot(result, component_scores)

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

    def calculate_component_scores(self, result: BacktestResult) -> dict[str, float]:
        profit_score = self._score_profit(result.profit_pct)
        risk_score = self._score_risk(result.max_drawdown_pct)
        stability_score = self._score_stability(result.win_rate)
        quality_score = self._score_quality(result.total_trades)
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
        return {
            "scoring_version": self.scoring_version,
            "source": "backtest_result",
            "backtest_result_id": result.id,
            "profit_pct": result.profit_pct,
            "max_drawdown_pct": result.max_drawdown_pct,
            "win_rate": result.win_rate,
            "total_trades": result.total_trades,
            "component_scores": component_scores,
            "missing_metrics": missing_metrics,
        }

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

    def _score_quality(self, total_trades: Optional[int]) -> float:
        if total_trades is None or total_trades <= 0:
            return 0.0
        return self._clamp(total_trades / 30.0 * 100.0)

    def _clamp(self, value: float) -> float:
        return round(max(0.0, min(100.0, value)), 6)
