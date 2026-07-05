from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from app.schemas.data_source import DataSourceTrace, database_record_source, unknown_source


BacktestStatus = Literal["pending", "running", "succeeded", "failed", "cancelled"]


class BacktestRunCreate(BaseModel):
    strategy_version_id: int
    profile_name: Optional[str] = Field(default=None, max_length=120)
    config_snapshot: dict[str, Any] = Field(default_factory=dict)


class BacktestRunStatusUpdate(BaseModel):
    status: BacktestStatus


class BacktestTaskCreate(BaseModel):
    pair: str = Field(min_length=1, max_length=80)
    timeframe: str = Field(min_length=1, max_length=32)
    config_path: Optional[str] = None


class BacktestTaskStatusUpdate(BaseModel):
    status: BacktestStatus
    result_path: Optional[str] = None
    error_message: Optional[str] = None


class BacktestResultCreate(BaseModel):
    result_path: str = Field(min_length=1)
    metrics_snapshot: dict[str, Any] = Field(default_factory=dict)
    profit_total: Optional[float] = None
    profit_pct: Optional[float] = None
    max_drawdown_pct: Optional[float] = None
    win_rate: Optional[float] = Field(default=None, ge=0, le=1)
    total_trades: Optional[int] = Field(default=None, ge=0)
    timerange: Optional[str] = Field(default=None, max_length=80)


class BacktestRunRead(BaseModel):
    id: int
    strategy_version_id: int
    profile_name: Optional[str]
    config_snapshot: dict[str, Any]
    status: BacktestStatus
    requested_task_count: int
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    created_at: datetime
    data_source: DataSourceTrace = Field(default_factory=lambda: unknown_source("unvalidated backtest run source"))

    model_config = {"from_attributes": True}

    @model_validator(mode="after")
    def attach_database_source(self) -> "BacktestRunRead":
        self.data_source = database_record_source(
            "backtest_run",
            {
                "backtest_run_id": self.id,
                "strategy_version_id": self.strategy_version_id,
            },
            freshness=self.created_at,
        )
        return self


class BacktestTaskRead(BaseModel):
    id: int
    backtest_run_id: int
    pair: str
    timeframe: str
    status: BacktestStatus
    config_path: Optional[str]
    result_path: Optional[str]
    error_message: Optional[str]
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    created_at: datetime
    data_source: DataSourceTrace = Field(
        default_factory=lambda: unknown_source("unvalidated backtest task source")
    )

    model_config = {"from_attributes": True}

    @model_validator(mode="after")
    def attach_database_source(self) -> "BacktestTaskRead":
        artifact_refs = {}
        if self.config_path is not None:
            artifact_refs["config_path"] = self.config_path
        if self.result_path is not None:
            artifact_refs["result_path"] = self.result_path
        self.data_source = database_record_source(
            "backtest_task",
            {
                "backtest_task_id": self.id,
                "backtest_run_id": self.backtest_run_id,
            },
            artifact_refs=artifact_refs,
            freshness=self.created_at,
        )
        return self


class BacktestResultRead(BaseModel):
    id: int
    backtest_run_id: int
    backtest_task_id: int
    result_path: str
    metrics_snapshot: dict[str, Any]
    profit_total: Optional[float]
    profit_pct: Optional[float]
    max_drawdown_pct: Optional[float]
    win_rate: Optional[float]
    total_trades: Optional[int]
    timerange: Optional[str]
    created_at: datetime
    data_source: DataSourceTrace = Field(
        default_factory=lambda: unknown_source("unvalidated backtest result source")
    )

    model_config = {"from_attributes": True}

    @model_validator(mode="after")
    def attach_database_source(self) -> "BacktestResultRead":
        self.data_source = database_record_source(
            "backtest_result",
            {
                "backtest_result_id": self.id,
                "backtest_run_id": self.backtest_run_id,
                "backtest_task_id": self.backtest_task_id,
            },
            artifact_refs={"result_path": self.result_path},
            freshness=self.created_at,
        )
        return self
