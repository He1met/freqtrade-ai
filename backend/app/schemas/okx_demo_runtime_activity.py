from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class OkxDemoActiveDeploymentRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deployment_id: int = Field(gt=0)
    status: Literal["ACTIVE"]
    active_slot: int = Field(ge=1, le=9)
    instrument_id: str
    timeframe: str
    strategy_id: int = Field(gt=0)
    strategy_name: str
    strategy_version_id: int = Field(gt=0)
    strategy_version_number: int = Field(gt=0)
    candidate_approval_id: int = Field(gt=0)
    candidate_approval_status: str
    created_at: datetime


class OkxDemoSignalEvaluationRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evaluation_id: int = Field(gt=0)
    deployment_id: int = Field(gt=0)
    instrument_id: str
    timeframe: str
    closed_candle_at: datetime
    status: Literal["PENDING", "LEASED", "NO_ACTION", "ACTIONABLE", "BLOCKED", "FAILED"]
    completed_at: Optional[datetime] = None
    error_code: Optional[str] = None
    created_at: datetime


class OkxDemoProjectionWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    returned_count: int = Field(ge=0)
    limit: int = Field(gt=0)
    has_more: bool


class OkxDemoRuntimeActivityRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["okx-demo-runtime-activity-v1"]
    as_of: datetime
    source_type: Literal["database"]
    core_data: Literal[True]
    execution_target: Literal["OKX_DEMO"]
    allow_real_funds: Literal[False]
    real_orders: Literal[False]
    active_deployments: list[OkxDemoActiveDeploymentRead]
    recent_signal_evaluations: list[OkxDemoSignalEvaluationRead]
    signal_window: OkxDemoProjectionWindow
