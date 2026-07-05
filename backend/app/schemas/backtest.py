from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from app.schemas.data_source import (
    DataSourceTrace,
    database_record_source,
    phase8_local_test_metadata_from_payload,
    phase8_local_test_source,
    unknown_source,
)


BacktestStatus = Literal["pending", "running", "succeeded", "failed", "cancelled", "blocked"]


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


class LocalBacktestTriggerRequest(BaseModel):
    strategy_version_id: int = Field(gt=0)
    profile: dict[str, Any] = Field(default_factory=dict)


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
        database_ids = {
            "backtest_run_id": self.id,
            "strategy_version_id": self.strategy_version_id,
        }
        local_test_source = phase8_local_test_source(
            "backtest_run",
            phase8_local_test_metadata_from_payload(self.config_snapshot),
            database_ids,
            freshness=self.created_at,
        )
        if local_test_source is not None:
            self.data_source = local_test_source
            return self

        self.data_source = database_record_source(
            "backtest_run",
            database_ids,
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
        if self.config_path is not None and "phase8-local-test" in self.config_path:
            self.data_source = DataSourceTrace(
                source_type="fixture",
                source_detail="Phase 8 local-test backtest_task row from seed artifact paths; not core success.",
                core_data=False,
                database_ids={
                    "backtest_task_id": self.id,
                    "backtest_run_id": self.backtest_run_id,
                },
                artifact_refs=artifact_refs,
                freshness=self.created_at,
                blocked_reason=self.error_message,
            )
            return self

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


class LocalBacktestTriggerResponse(BaseModel):
    run: BacktestRunRead
    tasks: list[BacktestTaskRead]
    preflight_status: Literal["ready", "blocked"]
    blocked_reasons: list[str] = Field(default_factory=list)
    execution_mode: Literal["preflight_only"] = "preflight_only"


class BacktestArtifactIngestRequest(BaseModel):
    manifest_path: Optional[str] = Field(default=None, min_length=1)
    result_path: Optional[str] = Field(default=None, min_length=1)
    strategy_name: Optional[str] = Field(default=None, min_length=1, max_length=120)


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
        database_ids = {
            "backtest_result_id": self.id,
            "backtest_run_id": self.backtest_run_id,
            "backtest_task_id": self.backtest_task_id,
        }
        local_test_source = phase8_local_test_source(
            "backtest_result",
            phase8_local_test_metadata_from_payload(self.metrics_snapshot),
            database_ids,
            artifact_refs={"result_path": self.result_path},
            freshness=self.created_at,
        )
        if local_test_source is not None:
            self.data_source = local_test_source
            return self

        self.data_source = database_record_source(
            "backtest_result",
            database_ids,
            artifact_refs={"result_path": self.result_path},
            freshness=self.created_at,
        )
        return self


class BacktestArtifactIngestResponse(BaseModel):
    run: BacktestRunRead
    task: BacktestTaskRead
    result: Optional[BacktestResultRead] = None
    ingest_status: Literal["succeeded", "failed", "blocked"]
    reason: Optional[str] = None
    manifest_path: Optional[str] = None
    result_path: Optional[str] = None
    parser_source: Literal["freqtrade_result_parser"] = "freqtrade_result_parser"
    execution_mode: Literal["artifact_ingest_only"] = "artifact_ingest_only"
