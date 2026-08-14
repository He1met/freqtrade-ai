"""Read-only filesystem adapter for canonical latest-per-class intake planning."""

from __future__ import annotations

import ast
import base64
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Final

from app.canonical_v13.intake import (
    CanonicalIntakeBlocked,
    ExternalSourceEntrySnapshot,
    ExternalVersionSnapshot,
    IntakeInspection,
    inspect_intake_artifact,
)


LATEST_MANIFEST_CONTRACT: Final = "canonical-v13-latest-per-class-manifest-v1"
_RUN_FILE = re.compile(r"^(?P<stem>.+)_run_(?P<run>[1-9][0-9]*)_1\.py$")


@dataclass(frozen=True)
class VisibleStrategyVersion:
    strategy_class: str
    relative_path: str
    run_number: int
    raw_digest: str
    version_id: str
    artifact_bytes: bytes


@dataclass(frozen=True)
class LatestStrategyManifestEntry:
    strategy_class: str
    selected_path: str
    selected_run: int
    selected_code_digest: str
    inspection: IntakeInspection
    snapshot: ExternalSourceEntrySnapshot

    def api_command(self, *, caller_identity: str) -> dict[str, object]:
        idempotency_key = "latest-intake:" + _canonical_digest(
            {
                "archive_snapshot_digest": self.snapshot.archive_snapshot_digest,
                "strategy_class": self.strategy_class,
            }
        )
        return {
            "caller_identity": caller_identity,
            "idempotency_key": idempotency_key,
            "display_name": self.strategy_class,
            "archive_snapshot_digest": self.snapshot.archive_snapshot_digest,
            "source_entry_key": self.snapshot.source_entry_key,
            "source_strategy_key": self.strategy_class,
            "current_version_id": self.snapshot.current_version_id,
            "versions": [
                {
                    "source_strategy_key": item.source_strategy_key,
                    "version_id": item.version_id,
                    "version_number": item.version_number,
                    "artifact_base64": base64.b64encode(item.artifact_bytes).decode(
                        "ascii"
                    ),
                }
                for item in self.snapshot.versions
            ],
        }

    def evidence(self) -> dict[str, object]:
        return {
            "strategy_class": self.strategy_class,
            "selected_path": self.selected_path,
            "selected_run": self.selected_run,
            "selected_code_digest": self.selected_code_digest,
            "safety_result": "PASSED",
            "safety_contract": self.inspection.checks["contract"],
            "checks": self.inspection.checks,
        }


@dataclass(frozen=True)
class LatestStrategyManifest:
    archive_snapshot_digest: str
    entries: tuple[LatestStrategyManifestEntry, ...]
    visible_file_count: int

    def evidence(self) -> dict[str, object]:
        return {
            "contract": LATEST_MANIFEST_CONTRACT,
            "source_identity": "explicit-read-only-strategy-filesystem",
            "archive_snapshot_digest": self.archive_snapshot_digest,
            "visible_file_count": self.visible_file_count,
            "selected_strategy_count": len(self.entries),
            "entries": [entry.evidence() for entry in self.entries],
            "legacy_database_access": "NONE",
            "legacy_identity_migration": "NONE",
            "backtest": "NOT_RUN",
            "research": "NOT_RUN",
            "qualification": "NOT_RUN",
            "optimization": "NOT_RUN",
            "runtime": "NOT_RUN",
            "orders": "NOT_RUN",
        }


def _canonical_digest(value: object) -> str:
    return sha256(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _class_name(tree: ast.Module, *, relative_path: str) -> str:
    classes = [node.name for node in tree.body if isinstance(node, ast.ClassDef)]
    if len(classes) != 1:
        raise CanonicalIntakeBlocked(
            "BLOCKED_AMBIGUOUS_SOURCE_CLASS",
            f"{relative_path} must define exactly one top-level class",
        )
    return classes[0]


def build_latest_strategy_manifest(source_root: Path) -> LatestStrategyManifest:
    """Capture all visible run files and select one highest run per class."""

    if not source_root.is_absolute() or not source_root.is_dir():
        raise CanonicalIntakeBlocked(
            "BLOCKED_INVALID_SOURCE_ROOT",
            "source_root must be an existing explicit absolute directory",
        )
    resolved_root = source_root.resolve(strict=True)
    visible: list[VisibleStrategyVersion] = []
    for path in sorted(resolved_root.rglob("*.py")):
        if path.is_symlink() or not path.is_file():
            raise CanonicalIntakeBlocked(
                "BLOCKED_UNSAFE_SOURCE_FILE", "symlinked source files are not allowed"
            )
        resolved_path = path.resolve(strict=True)
        try:
            resolved_relative = resolved_path.relative_to(resolved_root)
        except ValueError as exc:
            raise CanonicalIntakeBlocked(
                "BLOCKED_PATH_TRAVERSAL",
                "source file resolves outside the explicit source root",
            ) from exc
        if resolved_path != path:
            raise CanonicalIntakeBlocked(
                "BLOCKED_UNSAFE_SOURCE_FILE",
                "source files beneath symlinked directories are not allowed",
            )
        relative_path = resolved_relative.as_posix()
        match = _RUN_FILE.fullmatch(path.name)
        if match is None:
            raise CanonicalIntakeBlocked(
                "BLOCKED_AMBIGUOUS_SOURCE_VERSION",
                f"{relative_path} does not have an exact run version",
            )
        artifact_bytes = path.read_bytes()
        raw_digest = sha256(artifact_bytes).hexdigest()
        try:
            tree = ast.parse(
                artifact_bytes.decode("utf-8-sig", errors="strict"),
                filename=relative_path,
                mode="exec",
            )
        except (SyntaxError, UnicodeDecodeError, ValueError) as exc:
            raise CanonicalIntakeBlocked(
                "BLOCKED_UNREADABLE_SOURCE_AST",
                f"{relative_path} cannot be classified without execution",
            ) from exc
        strategy_class = _class_name(tree, relative_path=relative_path)
        run_number = int(match.group("run"))
        version_id = _canonical_digest(
            {
                "strategy_class": strategy_class,
                "relative_path": relative_path,
                "run_number": run_number,
                "raw_digest": raw_digest,
            }
        )
        visible.append(
            VisibleStrategyVersion(
                strategy_class=strategy_class,
                relative_path=relative_path,
                run_number=run_number,
                raw_digest=raw_digest,
                version_id=version_id,
                artifact_bytes=artifact_bytes,
            )
        )
    if not visible:
        raise CanonicalIntakeBlocked(
            "BLOCKED_EMPTY_SOURCE_ARCHIVE", "no versioned strategy source was found"
        )

    archive_snapshot_digest = _canonical_digest(
        {
            "contract": LATEST_MANIFEST_CONTRACT,
            "visible_versions": [
                {
                    "strategy_class": item.strategy_class,
                    "relative_path": item.relative_path,
                    "run_number": item.run_number,
                    "raw_digest": item.raw_digest,
                }
                for item in visible
            ],
        }
    )
    grouped: dict[str, list[VisibleStrategyVersion]] = {}
    for item in visible:
        grouped.setdefault(item.strategy_class, []).append(item)

    entries: list[LatestStrategyManifestEntry] = []
    for strategy_class, versions in sorted(grouped.items()):
        runs = [item.run_number for item in versions]
        if len(runs) != len(set(runs)):
            raise CanonicalIntakeBlocked(
                "BLOCKED_AMBIGUOUS_LATEST_SOURCE",
                f"{strategy_class} has duplicate run numbers",
            )
        selected = max(versions, key=lambda item: item.run_number)
        inspection = inspect_intake_artifact(
            selected.artifact_bytes, expected_strategy_class=strategy_class
        )
        snapshot = ExternalSourceEntrySnapshot(
            archive_snapshot_digest=archive_snapshot_digest,
            source_entry_key=selected.relative_path,
            source_strategy_key=strategy_class,
            current_version_id=selected.version_id,
            versions=tuple(
                ExternalVersionSnapshot(
                    source_strategy_key=strategy_class,
                    version_id=item.version_id,
                    version_number=item.run_number,
                    artifact_bytes=item.artifact_bytes,
                )
                for item in sorted(versions, key=lambda item: item.run_number)
            ),
        )
        entries.append(
            LatestStrategyManifestEntry(
                strategy_class=strategy_class,
                selected_path=selected.relative_path,
                selected_run=selected.run_number,
                selected_code_digest=inspection.content_digest,
                inspection=inspection,
                snapshot=snapshot,
            )
        )
    return LatestStrategyManifest(
        archive_snapshot_digest=archive_snapshot_digest,
        entries=tuple(entries),
        visible_file_count=len(visible),
    )


__all__ = [
    "LATEST_MANIFEST_CONTRACT",
    "LatestStrategyManifest",
    "LatestStrategyManifestEntry",
    "VisibleStrategyVersion",
    "build_latest_strategy_manifest",
]
