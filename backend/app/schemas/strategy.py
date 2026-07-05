from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from app.schemas.data_source import (
    DataSourceTrace,
    database_record_source,
    phase8_local_test_metadata_from_payload,
    phase8_local_test_metadata_from_tags,
    phase8_local_test_source,
    unknown_source,
)


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
    data_source: DataSourceTrace = Field(default_factory=lambda: unknown_source("unvalidated strategy source"))

    model_config = {"from_attributes": True}

    @model_validator(mode="after")
    def attach_database_source(self) -> "StrategyRead":
        local_test_source = phase8_local_test_source(
            "strategy",
            phase8_local_test_metadata_from_tags(self.tags),
            {"strategy_id": self.id},
            freshness=self.updated_at,
        )
        if local_test_source is not None:
            self.data_source = local_test_source
            return self

        self.data_source = database_record_source(
            "strategy",
            {"strategy_id": self.id},
            freshness=self.updated_at,
        )
        return self


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
    data_source: DataSourceTrace = Field(
        default_factory=lambda: unknown_source("unvalidated strategy version source")
    )

    model_config = {"from_attributes": True}

    @model_validator(mode="after")
    def attach_database_source(self) -> "StrategyVersionRead":
        database_ids = {
            "strategy_version_id": self.id,
            "strategy_id": self.strategy_id,
        }
        if self.generation_run_id is not None:
            database_ids["generation_run_id"] = self.generation_run_id
        local_test_source = phase8_local_test_source(
            "strategy_version",
            phase8_local_test_metadata_from_payload(self.blueprint, self.diff_snapshot),
            database_ids,
            artifact_refs={"strategy_file_path": self.file_path},
            freshness=self.created_at,
        )
        if local_test_source is not None:
            self.data_source = local_test_source
            return self

        self.data_source = database_record_source(
            "strategy_version",
            database_ids,
            artifact_refs={"strategy_file_path": self.file_path},
            freshness=self.created_at,
        )
        return self


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
