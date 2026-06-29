from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


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

    model_config = {"from_attributes": True}
