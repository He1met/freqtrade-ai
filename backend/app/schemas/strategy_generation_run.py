from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from app.schemas.data_source import DataSourceTrace, api_aggregate_source, database_record_source, unknown_source
from app.schemas.operation_evidence import OperationEvidence
from app.schemas.strategy import StrategyRead, StrategyVersionRead


GenerationRunStatus = Literal["pending", "running", "succeeded", "failed", "cancelled"]


class StrategyGenerationRunCreate(BaseModel):
    provider: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=160)
    prompt_hash: Optional[str] = Field(default=None, max_length=128)
    prompt_summary: Optional[str] = None
    params_snapshot: dict[str, Any] = Field(default_factory=dict)
    requested_count: int = Field(default=0, ge=0)


class StrategyGenerationRunStatusUpdate(BaseModel):
    status: GenerationRunStatus
    generated_count: Optional[int] = Field(default=None, ge=0)
    accepted_count: Optional[int] = Field(default=None, ge=0)
    failed_count: Optional[int] = Field(default=None, ge=0)
    error_message: Optional[str] = None


class StrategyGenerationRunRead(BaseModel):
    id: int
    provider: str
    model: str
    prompt_hash: Optional[str]
    prompt_summary: Optional[str]
    params_snapshot: dict[str, Any]
    status: GenerationRunStatus
    requested_count: int
    generated_count: int
    accepted_count: int
    failed_count: int
    error_message: Optional[str]
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    created_at: datetime
    data_source: DataSourceTrace = Field(
        default_factory=lambda: unknown_source("unvalidated strategy generation run source")
    )

    model_config = {"from_attributes": True}

    @model_validator(mode="after")
    def attach_database_source(self) -> "StrategyGenerationRunRead":
        self.data_source = database_record_source(
            "strategy_generation_run",
            {"strategy_generation_run_id": self.id},
            freshness=self.created_at,
        )
        return self


class StrategyGenerationRequest(BaseModel):
    prompt_summary: str = Field(min_length=1, max_length=4000)
    requested_count: int = Field(default=1, ge=1, le=1)


class DeepSeekSingleGenerationRequest(BaseModel):
    prompt_summary: str = Field(min_length=1, max_length=4000)
    allow_real_call: bool = False


class StrategyGenerationApiResponse(BaseModel):
    run: StrategyGenerationRunRead
    strategies: list[StrategyRead]
    strategy_versions: list[StrategyVersionRead]
    data_source: DataSourceTrace = Field(
        default_factory=lambda: unknown_source("unvalidated strategy generation API response")
    )
    evidence: Optional[OperationEvidence] = None

    @model_validator(mode="after")
    def attach_api_aggregate_source(self) -> "StrategyGenerationApiResponse":
        database_ids = {"strategy_generation_run_id": self.run.id}
        if self.strategies:
            database_ids["strategy_id"] = self.strategies[0].id
            database_ids["first_strategy_id"] = self.strategies[0].id
        if self.strategy_versions:
            database_ids["strategy_version_id"] = self.strategy_versions[0].id
            database_ids["first_strategy_version_id"] = self.strategy_versions[0].id
        self.data_source = api_aggregate_source(
            "strategy_generation_api_response",
            database_ids,
            artifact_refs={
                f"strategy_file_path_{item.id}": item.file_path
                for item in self.strategy_versions
            },
            freshness=self.run.created_at,
        )
        self.evidence = OperationEvidence(
            status="SUCCESS",
            ids=dict(self.data_source.database_ids),
            artifact_refs=dict(self.data_source.artifact_refs),
            data_source=self.data_source,
            next_action="Refresh generation, strategy, and strategy-version APIs to reconcile persisted records.",
            acceptance_ready=True,
        )
        return self
