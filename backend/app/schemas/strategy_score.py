from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator

from app.schemas.data_source import (
    DataSourceTrace,
    api_aggregate_source,
    database_record_source,
    phase8_local_test_metadata_from_payload,
    phase8_local_test_source,
    unknown_source,
)


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
    data_source: DataSourceTrace = Field(default_factory=lambda: unknown_source("unvalidated strategy score source"))

    model_config = {"from_attributes": True}

    @model_validator(mode="after")
    def attach_database_source(self) -> "StrategyScoreRead":
        database_ids = {
            "strategy_score_id": self.id,
            "strategy_id": self.strategy_id,
            "strategy_version_id": self.strategy_version_id,
        }
        if self.backtest_result_id is not None:
            database_ids["backtest_result_id"] = self.backtest_result_id
        local_test_source = phase8_local_test_source(
            "strategy_score",
            phase8_local_test_metadata_from_payload(self.metrics_snapshot),
            database_ids,
            freshness=self.created_at,
        )
        if local_test_source is not None:
            self.data_source = local_test_source
            return self

        self.data_source = database_record_source(
            "strategy_score",
            database_ids,
            freshness=self.created_at,
        )
        return self


class StrategyRankingEntry(BaseModel):
    score_id: int
    strategy_id: int
    strategy_version_id: int
    backtest_result_id: int
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
    data_source: DataSourceTrace = Field(
        default_factory=lambda: unknown_source("unvalidated strategy ranking source")
    )

    @model_validator(mode="after")
    def attach_api_aggregate_source(self) -> "StrategyRankingEntry":
        database_ids = {
            "strategy_score_id": self.score_id,
            "strategy_id": self.strategy_id,
            "strategy_version_id": self.strategy_version_id,
            "backtest_result_id": self.backtest_result_id,
        }
        local_test_source = phase8_local_test_source(
            "strategy_ranking_entry",
            phase8_local_test_metadata_from_payload(self.metrics_snapshot),
            database_ids,
            artifact_refs={"strategy_file_path": self.file_path},
            freshness=self.created_at,
        )
        if local_test_source is not None:
            self.data_source = local_test_source
            return self

        self.data_source = api_aggregate_source(
            "strategy_ranking_entry",
            database_ids,
            artifact_refs={"strategy_file_path": self.file_path},
            freshness=self.created_at,
        )
        return self
