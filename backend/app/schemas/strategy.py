from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


StrategyStatus = Literal["draft", "active", "archived"]
StrategySource = Literal["ai_generated", "imported", "manual"]
StrategyValidationStatus = Literal["pending", "passed", "failed"]


class StrategyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    slug: str = Field(min_length=1, max_length=180)
    description: Optional[str] = None
    status: StrategyStatus = "draft"
    source: StrategySource = "ai_generated"
    tags: list[str] = Field(default_factory=list)


class StrategyVersionCreate(BaseModel):
    strategy_id: int = Field(gt=0)
    generation_run_id: Optional[int] = Field(default=None, gt=0)
    parent_version_id: Optional[int] = Field(default=None, gt=0)
    version_number: Optional[int] = Field(default=None, gt=0)
    blueprint: dict[str, Any]
    generated_code: str = Field(min_length=1)
    code_hash: Optional[str] = Field(default=None, max_length=128)
    file_path: str = Field(min_length=1)
    validation_status: StrategyValidationStatus = "pending"
    validation_errors: list[dict[str, Any]] = Field(default_factory=list)
    change_summary: Optional[str] = None
    diff_snapshot: dict[str, Any] = Field(default_factory=dict)


class StrategyRead(BaseModel):
    id: int
    name: str
    slug: str
    description: Optional[str]
    status: StrategyStatus
    source: StrategySource
    tags: list[str]
    current_version_id: Optional[int]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class StrategyVersionRead(BaseModel):
    id: int
    strategy_id: int
    generation_run_id: Optional[int]
    parent_version_id: Optional[int]
    version_number: int
    blueprint: dict[str, Any]
    generated_code: str
    code_hash: Optional[str]
    file_path: str
    validation_status: StrategyValidationStatus
    validation_errors: list[dict[str, Any]]
    change_summary: Optional[str]
    diff_snapshot: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class StrategyVersionLineageEntry(BaseModel):
    id: int
    strategy_id: int
    parent_version_id: Optional[int]
    version_number: int
    change_summary: Optional[str]
    diff_snapshot: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class StrategyVersionDiffRead(BaseModel):
    id: int
    strategy_id: int
    parent_version_id: Optional[int]
    version_number: int
    change_summary: Optional[str]
    diff_snapshot: dict[str, Any]
    has_parent: bool
