from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

from app.schemas.backtest import BacktestArtifactIngestResponse, LocalBacktestTriggerResponse
from app.schemas.data_source import DataSourceTrace, unknown_source
from app.schemas.operation_evidence import OperationEvidence
from app.schemas.strategy_generation_run import StrategyGenerationApiResponse, StrategyGenerationRunRead


DeepSeekBacktestLoopStatus = Literal["succeeded", "failed", "blocked"]


class StrategyValidationWindowRequest(BaseModel):
    window_kind: Literal["OOS", "WALK_FORWARD"]
    timerange: str = Field(pattern=r"^\d{8}-\d{8}$")
    expected_market_data_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    market_state: Optional[Literal["bull", "bear", "range"]] = None

    @model_validator(mode="after")
    def validate_market_state(self) -> "StrategyValidationWindowRequest":
        if self.window_kind == "OOS" and self.market_state is not None:
            raise ValueError("OOS validation window must not claim a market state")
        if self.window_kind == "WALK_FORWARD" and self.market_state is None:
            raise ValueError("walk-forward validation window requires a market state slot")
        return self


class DeepSeekBacktestLoopRequest(BaseModel):
    prompt_summary: str = Field(min_length=1, max_length=4000)
    allow_real_call: bool = False
    backtest_profile: dict = Field(default_factory=dict)
    validation_windows: list[StrategyValidationWindowRequest] = Field(
        default_factory=list
    )
    timeout_seconds: Optional[int] = Field(default=None, gt=0, le=3600)


class DeepSeekBacktestExecutionRead(BaseModel):
    status: Literal["succeeded", "failed", "blocked"]
    manifest_path: str
    result_path: str
    command_args: list[str] = Field(default_factory=list)
    return_code: Optional[int] = None
    blocked_reason: Optional[str] = None
    failed_reason: Optional[str] = None
    created_at: datetime
    data_source: DataSourceTrace = Field(
        default_factory=lambda: unknown_source("unvalidated deepseek backtest execution source")
    )

    @model_validator(mode="after")
    def attach_data_source(self) -> "DeepSeekBacktestExecutionRead":
        artifact_refs = {
            "artifact_manifest_path": self.manifest_path,
            "backtest_result_path": self.result_path,
        }
        self.data_source = DataSourceTrace(
            source_type="unknown",
            source_detail="deepseek backtest execution artifact was produced before DB/API reconciliation",
            core_data=False,
            artifact_refs=artifact_refs,
            freshness=self.created_at,
        )
        return self


class DeepSeekBacktestLoopResponse(BaseModel):
    overall_status: DeepSeekBacktestLoopStatus
    generation_run: Optional[StrategyGenerationRunRead] = None
    generation: Optional[StrategyGenerationApiResponse] = None
    backtest: Optional[LocalBacktestTriggerResponse] = None
    execution: Optional[DeepSeekBacktestExecutionRead] = None
    artifact_ingest: Optional[BacktestArtifactIngestResponse] = None
    evidence: OperationEvidence
    data_source: DataSourceTrace = Field(
        default_factory=lambda: unknown_source("unvalidated deepseek backtest loop response")
    )

    @model_validator(mode="after")
    def attach_data_source(self) -> "DeepSeekBacktestLoopResponse":
        self.data_source = self.evidence.data_source
        return self
