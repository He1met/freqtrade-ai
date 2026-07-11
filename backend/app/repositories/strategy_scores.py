from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.strategy import Strategy, StrategyVersion
from app.models.strategy_score import StrategyScore
from app.schemas.strategy_score import StrategyRankingEntry, StrategyScoreCreate


class StrategyScoreRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get(self, score_id: int) -> Optional[StrategyScore]:
        return self.db.get(StrategyScore, score_id)

    def get_for_version(
        self,
        strategy_version_id: int,
        scoring_version: str,
    ) -> Optional[StrategyScore]:
        statement = select(StrategyScore).where(
            StrategyScore.strategy_version_id == strategy_version_id,
            StrategyScore.scoring_version == scoring_version,
        )
        return self.db.scalars(statement).first()

    def save(self, payload: StrategyScoreCreate, *, commit: bool = True) -> Optional[StrategyScore]:
        strategy = self.db.get(Strategy, payload.strategy_id)
        version = self.db.get(StrategyVersion, payload.strategy_version_id)
        if strategy is None or version is None or version.strategy_id != strategy.id:
            return None

        score = self.get_for_version(payload.strategy_version_id, payload.scoring_version)
        if score is None:
            score = StrategyScore(
                strategy_id=payload.strategy_id,
                strategy_version_id=payload.strategy_version_id,
                scoring_version=payload.scoring_version,
            )
            self.db.add(score)

        score.backtest_result_id = payload.backtest_result_id
        score.total_score = payload.total_score
        score.profit_score = payload.profit_score
        score.risk_score = payload.risk_score
        score.stability_score = payload.stability_score
        score.quality_score = payload.quality_score
        score.metrics_snapshot = payload.metrics_snapshot

        if commit:
            self.db.commit()
            self.db.refresh(score)
        return score

    def list_ranking(self, limit: int = 20) -> list[StrategyRankingEntry]:
        statement = (
            select(StrategyScore, Strategy, StrategyVersion)
            .join(Strategy, StrategyScore.strategy_id == Strategy.id)
            .join(StrategyVersion, StrategyScore.strategy_version_id == StrategyVersion.id)
            .where(StrategyScore.backtest_result_id.is_not(None))
            .order_by(
                StrategyScore.total_score.desc(),
                StrategyScore.created_at.desc(),
                StrategyScore.id.asc(),
            )
        )
        entries: list[StrategyRankingEntry] = []
        for score, strategy, version in self.db.execute(statement).all():
            if not self._is_core_backtest_score(score):
                continue
            entries.append(
                StrategyRankingEntry(
                    score_id=score.id,
                    strategy_id=strategy.id,
                    strategy_version_id=version.id,
                    backtest_result_id=score.backtest_result_id,
                    strategy_name=strategy.name,
                    strategy_slug=strategy.slug,
                    version_number=version.version_number,
                    file_path=version.file_path,
                    scoring_version=score.scoring_version,
                    total_score=score.total_score,
                    profit_score=score.profit_score,
                    risk_score=score.risk_score,
                    stability_score=score.stability_score,
                    quality_score=score.quality_score,
                    metrics_snapshot=score.metrics_snapshot,
                    created_at=score.created_at,
                )
            )
            if len(entries) >= limit:
                break
        return entries

    def _is_core_backtest_score(self, score: StrategyScore) -> bool:
        snapshot = score.metrics_snapshot or {}
        if score.backtest_result_id is None:
            return False
        if snapshot.get("source") != "backtest_result":
            return False
        if snapshot.get("backtest_result_id") != score.backtest_result_id:
            return False
        missing_metrics = snapshot.get("missing_metrics")
        return isinstance(missing_metrics, list) and not missing_metrics
