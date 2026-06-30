from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


StrategyFailureStage = Literal["generation", "validation", "static_check", "backtest_probe"]
StrategyFailureReasonType = Literal[
    "blueprint_schema_error",
    "validation_error",
    "render_error",
    "static_policy_violation",
    "backtest_probe_failed",
    "unknown",
]
StrategyFailureSeverity = Literal["info", "warning", "error"]


class StrategyFailureReasonCreate(BaseModel):
    strategy_id: int = Field(gt=0)
    strategy_version_id: int = Field(gt=0)
    stage: StrategyFailureStage
    reason_type: StrategyFailureReasonType
    severity: StrategyFailureSeverity = "error"
    message: str = Field(min_length=1, max_length=2000)
    details: dict[str, Any] = Field(default_factory=dict)


class StrategyFailureReasonFilter(BaseModel):
    strategy_id: Optional[int] = Field(default=None, gt=0)
    strategy_version_id: Optional[int] = Field(default=None, gt=0)
    stage: Optional[StrategyFailureStage] = None
    reason_type: Optional[StrategyFailureReasonType] = None


class StrategyFailureReasonRead(BaseModel):
    id: int
    strategy_id: int
    strategy_version_id: int
    stage: StrategyFailureStage
    reason_type: StrategyFailureReasonType
    severity: StrategyFailureSeverity
    message: str
    details: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}
