from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from app.core.config import Settings, get_settings


EnvironmentScope = Literal["current", "historical", "unknown"]


class EnvironmentEvidence(BaseModel):
    """Stable ownership contract for runtime artifact evidence."""

    scope: EnvironmentScope
    runnable: bool = False
    migration_verified: bool = False
    reason: str = Field(min_length=1, max_length=160)


@dataclass(frozen=True)
class EnvironmentIdentity:
    canonical_repo_root: Path
    artifact_roots: tuple[Path, ...]
    historical_roots: tuple[Path, ...]


def configured_environment_identity(settings: Optional[Settings] = None) -> EnvironmentIdentity:
    trusted = settings or get_settings()
    canonical_repo_root = _absolute_path(trusted.canonical_repo_root)
    artifact_roots = tuple(
        _resolve_trusted_path(path, canonical_repo_root)
        for path in (
            trusted.strategy_output_dir,
            trusted.backtest_result_dir,
        )
    )
    return EnvironmentIdentity(
        canonical_repo_root=canonical_repo_root,
        artifact_roots=artifact_roots,
        historical_roots=tuple(
            _absolute_path(path) for path in trusted.historical_read_only_roots
        ),
    )


def unknown_environment(reason: str = "证据环境归属无法确认。") -> EnvironmentEvidence:
    return EnvironmentEvidence(scope="unknown", reason=reason)


def classify_artifact_environment(
    artifact_refs: dict[str, str],
    *,
    identity: Optional[EnvironmentIdentity] = None,
    migration_verified: bool = False,
) -> EnvironmentEvidence:
    """Classify path evidence without mutating or copying any artifact."""

    trusted_identity = identity or configured_environment_identity()
    paths = [
        _resolve_artifact_path(value, trusted_identity.canonical_repo_root)
        for key, value in artifact_refs.items()
        if _is_path_reference(key, value)
    ]
    if not paths:
        return unknown_environment()

    roots = (trusted_identity.canonical_repo_root, *trusted_identity.artifact_roots)

    if any(
        any(_is_relative_to(path, root) for root in trusted_identity.historical_roots)
        for path in paths
    ):
        return EnvironmentEvidence(
            scope="historical",
            runnable=False,
            reason="证据属于历史环境，仅保留只读审计。",
        )
    if not all(any(_is_relative_to(path, root) for root in roots) for path in paths):
        return unknown_environment()

    runnable = all(path.exists() for path in paths)
    return EnvironmentEvidence(
        scope="current",
        runnable=runnable,
        migration_verified=migration_verified,
        reason=(
            "证据属于当前唯一环境。"
            if runnable
            else "证据属于当前环境，但 artifact 尚不可用。"
        ),
    )


def classify_strategy_environment(
    *,
    file_path: str,
    database_ids: dict[str, int],
    expected_checksum: Optional[str],
    migration_manifest: Optional[dict[str, Any]] = None,
    identity: Optional[EnvironmentIdentity] = None,
) -> EnvironmentEvidence:
    artifact_refs = {"strategy_file_path": file_path}
    base = classify_artifact_environment(
        artifact_refs,
        identity=identity,
    )
    if base.scope != "current" or migration_manifest is None:
        return base

    migration_verified = verify_environment_migration(
        migration_manifest=migration_manifest,
        current_path=_resolve_artifact_path(
            file_path,
            (identity or configured_environment_identity()).canonical_repo_root,
        ),
        database_ids=database_ids,
        expected_checksum=expected_checksum,
        identity=identity,
    )
    if migration_verified:
        return EnvironmentEvidence(
            scope="current",
            runnable=base.runnable,
            migration_verified=True,
            reason="迁移证据已完成文件、数据库 ID 与校验和对账。",
        )
    return EnvironmentEvidence(
        scope="current",
        runnable=False,
        migration_verified=False,
        reason="迁移 manifest 未通过文件、数据库 ID 与校验和对账。",
    )


def verify_environment_migration(
    *,
    migration_manifest: dict[str, Any],
    current_path: Path,
    database_ids: dict[str, int],
    expected_checksum: Optional[str],
    identity: Optional[EnvironmentIdentity] = None,
) -> bool:
    """Verify an already-performed migration; never performs migration itself."""

    source_path = migration_manifest.get("source_path")
    manifest_current_path = migration_manifest.get("current_path")
    manifest_database_ids = migration_manifest.get("database_ids")
    manifest_checksum = migration_manifest.get("artifact_checksum")
    if not all(
        (
            isinstance(source_path, str),
            isinstance(manifest_current_path, str),
            isinstance(manifest_database_ids, dict),
            isinstance(manifest_checksum, str),
            bool(expected_checksum),
        )
    ):
        return False

    trusted_identity = identity or configured_environment_identity()
    source_evidence = classify_artifact_environment(
        {"source_path": source_path},
        identity=trusted_identity,
    )
    resolved_manifest_path = _resolve_artifact_path(
        manifest_current_path,
        trusted_identity.canonical_repo_root,
    )
    if source_evidence.scope != "historical" or resolved_manifest_path != current_path:
        return False
    if manifest_database_ids != database_ids or manifest_checksum != expected_checksum:
        return False
    if not current_path.is_file():
        return False
    return hashlib.sha256(current_path.read_bytes()).hexdigest() == manifest_checksum


def _is_path_reference(key: str, value: str) -> bool:
    return bool(value) and (key == "path" or key.endswith("_path"))


def _absolute_path(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _resolve_trusted_path(path: Path, canonical_repo_root: Path) -> Path:
    expanded = path.expanduser()
    if expanded.is_absolute():
        return expanded.resolve(strict=False)
    return (canonical_repo_root / expanded).resolve(strict=False)


def _resolve_artifact_path(value: str, canonical_repo_root: Path) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate.resolve(strict=False)
    return (canonical_repo_root / candidate).resolve(strict=False)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
