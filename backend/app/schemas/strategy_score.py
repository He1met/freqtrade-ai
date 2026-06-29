from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class StrategyScoreCreate(BaseModel):
    strategy_id: int = Field(gt=0)
    strategy_version_id: int = Field(gt=0)
    backtest_result_id: Optional[int] = Field(default=None, gt=0)
    scoring_version: str = Field(min_length=1, max_length=80)
    total_score: float = Field(ge=0)
    profit_score: Optional[float] = Field(default=None, ge=0, le=100)
    risk_score: Optional[float] = Field(default=None, ge=0, le=100)
    stability_score: Optional[float] = Field(default=None, ge=0, le=100)
    quality_score: Optional[float] = Field(default=None, ge=0, le=100)
    metrics_snapshot: dict[str, Any] = Field(default_factory=dict)


class StrategyScoreRead(BaseModel):
    id: int
    strategy_id: int
    strategy_version_id: int
    backtest_result_id: Optional[int]
    scoring_version: str
    total_score: float
    profit_score: Optional[float]
    risk_score: Optional[float]
    stability_score: Optional[float]
    quality_score: Optional[float]
    metrics_snapshot: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class StrategyRankingEntry(BaseModel):
    score_id: int
    strategy_id: int
    strategy_version_id: int
    strategy_name: str
    strategy_slug: str
    version_number: int
    file_path: str
    scoring_version: str
    total_score: float
    profit_score: Optional[float]
    risk_score: Optional[float]
    stability_score: Optional[float]
    quality_score: Optional[float]
    metrics_snapshot: dict[str, Any]
    created_at: datetime
