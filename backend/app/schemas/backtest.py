from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


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

    model_config = {"from_attributes": True}


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

    model_config = {"from_attributes": True}


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

    model_config = {"from_attributes": True}
