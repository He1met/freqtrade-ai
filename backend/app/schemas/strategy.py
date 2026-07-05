import ast
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from app.core.config import get_settings
from app.core.paths import resolve_repo_path
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
StrategyFileRuntimeStatus = Literal["READY", "BLOCKED", "FAILED"]


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


class StrategyVersionFileState(BaseModel):
    status: StrategyFileRuntimeStatus
    path: str
    exists: bool
    is_file: bool
    checksum: Optional[str] = None
    checksum_matches: Optional[bool] = None
    class_name: Optional[str] = None
    blocked_reason: Optional[str] = None
    validation_errors: list[dict[str, Any]] = Field(default_factory=list)


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
    file_state: StrategyVersionFileState = Field(
        default_factory=lambda: StrategyVersionFileState(
            status="BLOCKED",
            path="",
            exists=False,
            is_file=False,
            blocked_reason="strategy file state was not inspected",
        )
    )
    created_at: datetime
    data_source: DataSourceTrace = Field(
        default_factory=lambda: unknown_source("unvalidated strategy version source")
    )

    model_config = {"from_attributes": True}

    @model_validator(mode="after")
    def attach_database_source(self) -> "StrategyVersionRead":
        self.file_state = _inspect_strategy_file_state(
            file_path=self.file_path,
            code_hash=self.code_hash,
            blueprint=self.blueprint,
            diff_snapshot=self.diff_snapshot,
        )
        database_ids = {
            "strategy_version_id": self.id,
            "strategy_id": self.strategy_id,
        }
        if self.generation_run_id is not None:
            database_ids["generation_run_id"] = self.generation_run_id
        artifact_refs = {
            "strategy_file_path": self.file_path,
            "strategy_file_state": self.file_state.status,
        }
        if self.code_hash:
            artifact_refs["strategy_file_checksum"] = self.code_hash
        local_test_source = phase8_local_test_source(
            "strategy_version",
            phase8_local_test_metadata_from_payload(self.blueprint, self.diff_snapshot),
            database_ids,
            artifact_refs=artifact_refs,
            freshness=self.created_at,
        )
        if local_test_source is not None:
            self.data_source = local_test_source
            return self

        if self.file_state.status != "READY":
            self.data_source = DataSourceTrace(
                source_type="database",
                source_detail="strategy_version record loaded from database, but strategy file artifact is not runnable",
                core_data=False,
                database_ids=database_ids,
                artifact_refs=artifact_refs,
                freshness=self.created_at,
                blocked_reason=self.file_state.blocked_reason,
            )
            return self

        self.data_source = database_record_source(
            "strategy_version",
            database_ids,
            artifact_refs=artifact_refs,
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


def _inspect_strategy_file_state(
    *,
    file_path: str,
    code_hash: Optional[str],
    blueprint: dict[str, Any],
    diff_snapshot: dict[str, Any],
) -> StrategyVersionFileState:
    resolved_path = resolve_repo_path(file_path).resolve(strict=False)
    class_name = blueprint.get("class_name") if isinstance(blueprint, dict) else None
    approved_root = _approved_root_from_snapshot(diff_snapshot)
    if approved_root is None:
        approved_root = resolve_repo_path(get_settings().strategy_output_dir).resolve(strict=False)

    if not _is_relative_to(resolved_path, approved_root):
        return _strategy_file_state(
            status="BLOCKED",
            path=str(resolved_path),
            class_name=class_name if isinstance(class_name, str) else None,
            blocked_reason="strategy file path is outside approved local runnable directories",
        )
    if resolved_path.suffix != ".py":
        return _strategy_file_state(
            status="BLOCKED",
            path=str(resolved_path),
            class_name=class_name if isinstance(class_name, str) else None,
            blocked_reason="strategy file path must end with .py",
        )
    if not resolved_path.exists():
        return _strategy_file_state(
            status="BLOCKED",
            path=str(resolved_path),
            class_name=class_name if isinstance(class_name, str) else None,
            blocked_reason=f"strategy file does not exist: {resolved_path}",
        )
    if not resolved_path.is_file():
        return _strategy_file_state(
            status="BLOCKED",
            path=str(resolved_path),
            exists=True,
            class_name=class_name if isinstance(class_name, str) else None,
            blocked_reason=f"strategy file path is not a file: {resolved_path}",
        )

    try:
        code = resolved_path.read_text(encoding="utf-8")
        tree = ast.parse(code, filename=str(resolved_path))
    except (OSError, SyntaxError) as exc:
        return _strategy_file_state(
            status="FAILED",
            path=str(resolved_path),
            exists=True,
            is_file=True,
            class_name=class_name if isinstance(class_name, str) else None,
            blocked_reason=f"strategy file cannot be read or parsed: {exc.__class__.__name__}",
            validation_errors=[
                _strategy_file_error(
                    "strategy_file.parse",
                    f"Strategy file cannot be read or parsed: {exc.__class__.__name__}.",
                )
            ],
        )

    if isinstance(class_name, str) and class_name:
        if not any(isinstance(node, ast.ClassDef) and node.name == class_name for node in tree.body):
            return _strategy_file_state(
                status="FAILED",
                path=str(resolved_path),
                exists=True,
                is_file=True,
                class_name=class_name,
                blocked_reason=f"strategy file does not define class {class_name}",
                validation_errors=[
                    _strategy_file_error(
                        "strategy_file.class_missing",
                        f"Strategy file does not define class {class_name}.",
                    )
                ],
            )

    checksum = hashlib.sha256(resolved_path.read_bytes()).hexdigest()
    checksum_matches = checksum == code_hash if code_hash else None
    if checksum_matches is False:
        return _strategy_file_state(
            status="FAILED",
            path=str(resolved_path),
            exists=True,
            is_file=True,
            checksum=checksum,
            checksum_matches=False,
            class_name=class_name if isinstance(class_name, str) else None,
            blocked_reason="strategy file checksum does not match strategy version code_hash",
            validation_errors=[
                _strategy_file_error(
                    "strategy_file.checksum_mismatch",
                    "Strategy file checksum does not match strategy version code_hash.",
                )
            ],
        )

    return _strategy_file_state(
        status="READY",
        path=str(resolved_path),
        exists=True,
        is_file=True,
        checksum=checksum,
        checksum_matches=checksum_matches,
        class_name=class_name if isinstance(class_name, str) else None,
    )


def _approved_root_from_snapshot(diff_snapshot: dict[str, Any]) -> Optional[Path]:
    strategy_file_validation = diff_snapshot.get("strategy_file_validation") if isinstance(diff_snapshot, dict) else None
    approved_root = (
        strategy_file_validation.get("approved_root")
        if isinstance(strategy_file_validation, dict)
        else None
    )
    return resolve_repo_path(approved_root).resolve(strict=False) if isinstance(approved_root, str) and approved_root else None


def _strategy_file_state(
    *,
    status: StrategyFileRuntimeStatus,
    path: str,
    exists: bool = False,
    is_file: bool = False,
    checksum: Optional[str] = None,
    checksum_matches: Optional[bool] = None,
    class_name: Optional[str] = None,
    blocked_reason: Optional[str] = None,
    validation_errors: Optional[list[dict[str, Any]]] = None,
) -> StrategyVersionFileState:
    return StrategyVersionFileState(
        status=status,
        path=path,
        exists=exists,
        is_file=is_file,
        checksum=checksum,
        checksum_matches=checksum_matches,
        class_name=class_name,
        blocked_reason=blocked_reason,
        validation_errors=validation_errors or [],
    )


def _strategy_file_error(rule_id: str, message: str) -> dict[str, Any]:
    return {
        "rule_id": rule_id,
        "category": "strategy_file_runtime_validation",
        "severity": "error",
        "message": message,
        "details": {},
    }


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
