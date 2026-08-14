"""Controlled canonical V1.3 strategy intake.

The caller supplies an already captured external archive snapshot.  This module never
opens a legacy database, walks a filesystem, imports submitted source, or executes it.
It only proves latest-version identity, performs the Phase 2 byte/envelope checks, and
persists one canonical strategy/version/receipt transaction.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from typing import Final
from uuid import UUID, uuid4

from sqlalchemy import Connection, select

from app.canonical_v13.genesis import verify_canonical_genesis
from app.canonical_v13.manifest import CANONICAL_BUSINESS_SCHEMA
from app.canonical_v13.models import (
    AUDIT_EVENTS_TABLE,
    IDEMPOTENCY_RECEIPTS_TABLE,
    STRATEGIES_TABLE,
    STRATEGY_ARTIFACTS_TABLE,
    STRATEGY_INTAKE_RECEIPTS_TABLE,
    STRATEGY_SUBMISSIONS_TABLE,
    STRATEGY_VERSIONS_TABLE,
)


DEFAULT_MAX_ARTIFACT_BYTES: Final = 1_000_000
INTAKE_SAFETY_CONTRACT: Final = "canonical-v13-intake-static-safety-v1"
_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_CONTROL_CHARACTER = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SECRET_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("PRIVATE_KEY", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("OPENAI_STYLE_TOKEN", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("AWS_ACCESS_KEY", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "ASSIGNED_SECRET",
        re.compile(
            r"(?i)\b(?:api[_-]?key|api[_-]?secret|access[_-]?token|password)\b"
            r"\s*[:=]\s*['\"][^'\"\r\n]{12,}['\"]"
        ),
    ),
)
_ALLOWED_IMPORTS: Final[frozenset[str]] = frozenset(
    {"freqtrade.strategy", "functools", "pandas", "talib.abstract"}
)
_ALLOWED_FROM_IMPORTS: Final[dict[str, frozenset[str]]] = {
    "freqtrade.strategy": frozenset({"IStrategy"}),
    "functools": frozenset({"reduce"}),
    "pandas": frozenset({"DataFrame"}),
}
_ALLOWED_DIRECT_IMPORTS: Final[dict[str, str]] = {"talib.abstract": "ta"}
_BANNED_CALL_NAMES: Final[frozenset[str]] = frozenset(
    {"__import__", "compile", "eval", "exec", "input", "open"}
)
_BANNED_ATTRIBUTE_NAMES: Final[frozenset[str]] = frozenset(
    {
        "connect",
        "create_connection",
        "fork",
        "popen",
        "remove",
        "rename",
        "replace",
        "rmdir",
        "socket",
        "spawn",
        "system",
        "unlink",
        "urlopen",
    }
)


class CanonicalIntakeBlocked(RuntimeError):
    """Stable fail-closed intake error; callers must treat it as a no-op."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class ExternalVersionSnapshot:
    source_strategy_key: str
    version_id: str
    version_number: int
    artifact_bytes: bytes


@dataclass(frozen=True)
class ExternalSourceEntrySnapshot:
    archive_snapshot_digest: str
    source_entry_key: str
    source_strategy_key: str
    current_version_id: str
    versions: tuple[ExternalVersionSnapshot, ...]


@dataclass(frozen=True)
class SelectedLatestArtifact:
    archive_snapshot_digest: str
    source_entry_key: str
    source_strategy_key: str
    version_id: str
    version_number: int
    artifact_bytes: bytes
    source_entry_digest: str


@dataclass(frozen=True)
class IntakeInspection:
    normalized_content: str
    normalized_bytes: bytes
    content_digest: str
    strategy_class: str
    checks: dict[str, object]


@dataclass(frozen=True)
class ControlledIntakeResult:
    submission_id: UUID
    artifact_id: UUID
    strategy_id: UUID
    strategy_version_id: UUID
    intake_receipt_id: UUID
    request_digest: str
    artifact_digest: str
    receipt_digest: str
    status: str
    catalog_status: str
    validation_status: str
    execution_authorized: bool
    idempotent_replay: bool


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _digest_json(value: object) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _require_digest(value: str, *, field: str) -> str:
    if not _HEX_DIGEST.fullmatch(value):
        raise CanonicalIntakeBlocked(
            "BLOCKED_INVALID_SOURCE_ENVELOPE", f"{field} must be lowercase SHA-256"
        )
    return value


def _require_identity(value: str, *, field: str, max_length: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value) > max_length
        or _CONTROL_CHARACTER.search(value)
    ):
        raise CanonicalIntakeBlocked(
            "BLOCKED_INVALID_SOURCE_ENVELOPE", f"{field} is invalid"
        )
    return value


def _require_safe_source_entry_key(value: str) -> str:
    _require_identity(value, field="source_entry_key", max_length=500)
    if (
        value.startswith("/")
        or "\\" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise CanonicalIntakeBlocked(
            "BLOCKED_PATH_TRAVERSAL",
            "source_entry_key must be a normalized root-relative POSIX path",
        )
    return value


def select_latest_source_artifact(
    snapshot: ExternalSourceEntrySnapshot,
) -> SelectedLatestArtifact:
    """Prove current-version ownership and highest-version agreement.

    The snapshot is an input DTO, not an adapter: this function performs no reads.
    """

    archive_digest = _require_digest(
        snapshot.archive_snapshot_digest, field="archive_snapshot_digest"
    )
    source_entry_key = _require_safe_source_entry_key(snapshot.source_entry_key)
    source_strategy_key = _require_identity(
        snapshot.source_strategy_key, field="source_strategy_key", max_length=200
    )
    current_version_id = _require_identity(
        snapshot.current_version_id, field="current_version_id", max_length=200
    )
    if not snapshot.versions:
        raise CanonicalIntakeBlocked(
            "BLOCKED_AMBIGUOUS_LATEST_SOURCE", "source entry has no visible versions"
        )

    version_ids: set[str] = set()
    version_numbers: set[int] = set()
    current: ExternalVersionSnapshot | None = None
    for version in snapshot.versions:
        version_id = _require_identity(
            version.version_id, field="version_id", max_length=200
        )
        if version.source_strategy_key != source_strategy_key:
            raise CanonicalIntakeBlocked(
                "BLOCKED_CURRENT_VERSION_OWNERSHIP",
                "a visible version belongs to a different source strategy",
            )
        if version.version_number <= 0 or isinstance(version.version_number, bool):
            raise CanonicalIntakeBlocked(
                "BLOCKED_AMBIGUOUS_LATEST_SOURCE",
                "version numbers must be distinct positive integers",
            )
        if version_id in version_ids or version.version_number in version_numbers:
            raise CanonicalIntakeBlocked(
                "BLOCKED_AMBIGUOUS_LATEST_SOURCE",
                "duplicate version identity or version number",
            )
        if not isinstance(version.artifact_bytes, bytes):
            raise CanonicalIntakeBlocked(
                "BLOCKED_INVALID_SOURCE_ENVELOPE", "artifact must be captured bytes"
            )
        version_ids.add(version_id)
        version_numbers.add(version.version_number)
        if version_id == current_version_id:
            current = version

    if current is None:
        raise CanonicalIntakeBlocked(
            "BLOCKED_CURRENT_VERSION_OWNERSHIP",
            "current_version_id is not a visible version of the source entry",
        )
    if current.version_number != max(version_numbers):
        raise CanonicalIntakeBlocked(
            "BLOCKED_AMBIGUOUS_LATEST_SOURCE",
            "current_version_id does not identify the highest visible version",
        )
    source_entry_digest = _digest_json(
        {
            "archive_snapshot_digest": archive_digest,
            "source_entry_key": source_entry_key,
            "source_strategy_key": source_strategy_key,
            "current_version_id": current.version_id,
            "current_version_number": current.version_number,
            "visible_versions": sorted(
                (version.version_id, version.version_number)
                for version in snapshot.versions
            ),
        }
    )
    return SelectedLatestArtifact(
        archive_snapshot_digest=archive_digest,
        source_entry_key=source_entry_key,
        source_strategy_key=source_strategy_key,
        version_id=current.version_id,
        version_number=current.version_number,
        artifact_bytes=current.artifact_bytes,
        source_entry_digest=source_entry_digest,
    )


def inspect_intake_artifact(
    artifact_bytes: bytes,
    *,
    expected_strategy_class: str | None = None,
    max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
) -> IntakeInspection:
    """Perform byte and AST-only checks without importing or executing source."""

    if max_artifact_bytes <= 0 or isinstance(max_artifact_bytes, bool):
        raise ValueError("max_artifact_bytes must be a positive integer")
    if not artifact_bytes:
        raise CanonicalIntakeBlocked(
            "REJECTED_EMPTY_ARTIFACT", "strategy artifact is empty"
        )
    if len(artifact_bytes) > max_artifact_bytes:
        raise CanonicalIntakeBlocked(
            "REJECTED_ARTIFACT_TOO_LARGE",
            f"artifact exceeds the {max_artifact_bytes}-byte safety envelope",
        )
    try:
        content = artifact_bytes.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise CanonicalIntakeBlocked(
            "REJECTED_INVALID_UTF8", "strategy artifact is not strict UTF-8"
        ) from exc
    if "\x00" in content or _CONTROL_CHARACTER.search(content):
        raise CanonicalIntakeBlocked(
            "REJECTED_CONTROL_CHARACTER", "strategy artifact contains control bytes"
        )
    normalized_content = content.replace("\r\n", "\n").replace("\r", "\n")
    normalized_bytes = normalized_content.encode("utf-8")
    for reason, pattern in _SECRET_PATTERNS:
        if pattern.search(normalized_content):
            raise CanonicalIntakeBlocked(
                "REJECTED_SECRET_SHAPED_CONTENT",
                f"strategy artifact matched secret safety rule {reason}",
            )
    try:
        tree = ast.parse(normalized_content, filename="<canonical-intake>", mode="exec")
    except (SyntaxError, ValueError) as exc:
        raise CanonicalIntakeBlocked(
            "REJECTED_INVALID_PYTHON_AST", "strategy artifact is not valid Python AST"
        ) from exc

    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    if len(classes) != 1:
        raise CanonicalIntakeBlocked(
            "REJECTED_STRATEGY_CLASS_SHAPE",
            "artifact must define exactly one top-level strategy class",
        )
    strategy_class = classes[0]
    if expected_strategy_class is not None:
        expected_strategy_class = _require_identity(
            expected_strategy_class,
            field="expected_strategy_class",
            max_length=200,
        )
        if strategy_class.name != expected_strategy_class:
            raise CanonicalIntakeBlocked(
                "REJECTED_STRATEGY_CLASS_MISMATCH",
                "selected artifact class does not match the latest-only manifest",
            )
    if strategy_class.decorator_list:
        raise CanonicalIntakeBlocked(
            "REJECTED_DYNAMIC_STRATEGY_SHAPE",
            "strategy class decorators are not allowed at intake",
        )
    base_names = {
        base.id
        if isinstance(base, ast.Name)
        else base.attr
        if isinstance(base, ast.Attribute)
        else ""
        for base in strategy_class.bases
    }
    if "IStrategy" not in base_names:
        raise CanonicalIntakeBlocked(
            "REJECTED_STRATEGY_BASE",
            "top-level strategy class must inherit IStrategy",
        )

    imported_modules: set[str] = set()
    import_bindings: set[str] = set()
    imported_istrategy = False
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _ALLOWED_DIRECT_IMPORTS.get(alias.name) != alias.asname:
                    raise CanonicalIntakeBlocked(
                        "REJECTED_IMPORT_NOT_ALLOWED",
                        "direct import binding is outside the canonical allowlist",
                    )
                imported_modules.add(alias.name)
                import_bindings.add(f"import:{alias.name}:as:{alias.asname}")
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                raise CanonicalIntakeBlocked(
                    "REJECTED_IMPORT_NOT_ALLOWED", "relative imports are not allowed"
                )
            allowed_names = _ALLOWED_FROM_IMPORTS.get(node.module, frozenset())
            for alias in node.names:
                if alias.name not in allowed_names or alias.asname is not None:
                    raise CanonicalIntakeBlocked(
                        "REJECTED_IMPORT_NOT_ALLOWED",
                        "from-import binding is outside the canonical allowlist",
                    )
                import_bindings.add(f"from:{node.module}:import:{alias.name}")
                imported_istrategy = imported_istrategy or (
                    node.module == "freqtrade.strategy" and alias.name == "IStrategy"
                )
            imported_modules.add(node.module)
        elif isinstance(node, ast.ClassDef):
            continue
        elif (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            continue
        else:
            raise CanonicalIntakeBlocked(
                "REJECTED_MODULE_LEVEL_EXECUTION",
                "module scope may contain only imports, a docstring, and the strategy class",
            )
    disallowed_imports = sorted(imported_modules - _ALLOWED_IMPORTS)
    if disallowed_imports:
        raise CanonicalIntakeBlocked(
            "REJECTED_IMPORT_NOT_ALLOWED",
            "strategy artifact imports a module outside the canonical allowlist",
        )
    if not imported_istrategy:
        raise CanonicalIntakeBlocked(
            "REJECTED_STRATEGY_BASE",
            "IStrategy must be imported from freqtrade.strategy",
        )
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.decorator_list:
            raise CanonicalIntakeBlocked(
                "REJECTED_DYNAMIC_STRATEGY_SHAPE",
                "function decorators are not allowed at intake",
            )
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in _BANNED_CALL_NAMES:
                raise CanonicalIntakeBlocked(
                    "REJECTED_DANGEROUS_CALL", "strategy artifact contains a banned call"
                )
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr.lower() in _BANNED_ATTRIBUTE_NAMES
            ):
                raise CanonicalIntakeBlocked(
                    "REJECTED_DANGEROUS_CALL", "strategy artifact contains a banned call"
                )
    content_digest = sha256(normalized_bytes).hexdigest()
    return IntakeInspection(
        normalized_content=normalized_content,
        normalized_bytes=normalized_bytes,
        content_digest=content_digest,
        strategy_class=strategy_class.name,
        checks={
            "contract": INTAKE_SAFETY_CONTRACT,
            "envelope": "PASSED",
            "size": "PASSED",
            "encoding": "UTF-8",
            "digest": "SHA-256",
            "secret_scan": "PASSED",
            "path_traversal": "PASSED",
            "ast_parse": "PASSED",
            "strategy_class": strategy_class.name,
            "strategy_base": "IStrategy",
            "import_allowlist": sorted(imported_modules),
            "import_bindings": sorted(import_bindings),
            "module_level_execution": "ABSENT",
            "dangerous_calls": "ABSENT",
            "static_validation": "PASSED",
            "lookahead_validation": "NOT_RUN",
            "backtest": "NOT_RUN",
            "execution": "NOT_AUTHORIZED",
        },
    )


def _effective_connection(connection: Connection) -> Connection:
    if connection.dialect.name == "sqlite":
        return connection.execution_options(
            schema_translate_map={CANONICAL_BUSINESS_SCHEMA: None}
        )
    return connection


def _row_by_id(connection: Connection, table, row_id: UUID) -> dict[str, object]:
    row = connection.execute(select(table).where(table.c.id == row_id)).one()
    return dict(row._mapping)


def _accepted_result(
    connection: Connection,
    submission: dict[str, object],
    *,
    idempotent_replay: bool,
) -> ControlledIntakeResult:
    submission_id = submission["id"]
    strategy = dict(
        connection.execute(
            select(STRATEGIES_TABLE).where(
                STRATEGIES_TABLE.c.source_submission_id == submission_id
            )
        ).one()._mapping
    )
    version = dict(
        connection.execute(
            select(STRATEGY_VERSIONS_TABLE).where(
                STRATEGY_VERSIONS_TABLE.c.strategy_id == strategy["id"]
            )
        ).one()._mapping
    )
    receipt = dict(
        connection.execute(
            select(STRATEGY_INTAKE_RECEIPTS_TABLE).where(
                STRATEGY_INTAKE_RECEIPTS_TABLE.c.submission_id == submission_id
            )
        ).one()._mapping
    )
    if (
        submission["status"] != "INTAKE_ACCEPTED"
        or strategy["catalog_status"] != "DRAFT"
        or version["validation_status"] != "UNVALIDATED"
        or version["execution_authorized"] is not False
        or receipt["status"] != "INTAKE_ACCEPTED"
        or receipt["checks_json"].get("contract") != INTAKE_SAFETY_CONTRACT
        or receipt["checks_json"].get("static_validation") != "PASSED"
    ):
        raise CanonicalIntakeBlocked(
            "BLOCKED_INTAKE_RECEIPT_DRIFT", "persisted intake is not canonical"
        )
    return ControlledIntakeResult(
        submission_id=submission_id,
        artifact_id=submission["artifact_id"],
        strategy_id=strategy["id"],
        strategy_version_id=version["id"],
        intake_receipt_id=receipt["id"],
        request_digest=submission["request_digest"],
        artifact_digest=receipt["artifact_digest"],
        receipt_digest=receipt["receipt_digest"],
        status="INTAKE_ACCEPTED",
        catalog_status="DRAFT",
        validation_status="UNVALIDATED",
        execution_authorized=False,
        idempotent_replay=idempotent_replay,
    )


def controlled_submit_latest(
    connection: Connection,
    *,
    caller_identity: str,
    idempotency_key: str,
    display_name: str,
    snapshot: ExternalSourceEntrySnapshot,
    max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
) -> ControlledIntakeResult:
    """Persist one accepted intake in the caller-owned transaction.

    All fail-closed validation happens before the first INSERT.  The caller must roll
    back the transaction if an unexpected database exception occurs.
    """

    caller_identity = _require_identity(
        caller_identity, field="caller_identity", max_length=160
    )
    idempotency_key = _require_identity(
        idempotency_key, field="idempotency_key", max_length=200
    )
    display_name = _require_identity(display_name, field="display_name", max_length=240)
    selected = select_latest_source_artifact(snapshot)
    inspection = inspect_intake_artifact(
        selected.artifact_bytes,
        max_artifact_bytes=max_artifact_bytes,
    )
    request_payload = {
        "contract": "canonical-v13-controlled-submission-v1",
        "archive_snapshot_digest": selected.archive_snapshot_digest,
        "source_entry_key": selected.source_entry_key,
        "source_entry_digest": selected.source_entry_digest,
        "source_strategy_key": selected.source_strategy_key,
        "selected_source_version_id": selected.version_id,
        "selected_source_version_number": selected.version_number,
        "artifact_digest": inspection.content_digest,
        "intake_safety_contract": INTAKE_SAFETY_CONTRACT,
        "strategy_class": inspection.strategy_class,
        "display_name": display_name,
    }
    request_digest = _digest_json(request_payload)

    effective = _effective_connection(connection)
    verification = verify_canonical_genesis(effective)
    if not verification.accepted:
        raise CanonicalIntakeBlocked(
            "BLOCKED_WRONG_CANONICAL_DATABASE", "; ".join(verification.problems)
        )

    existing_by_key = effective.execute(
        select(STRATEGY_SUBMISSIONS_TABLE).where(
            STRATEGY_SUBMISSIONS_TABLE.c.caller_identity == caller_identity,
            STRATEGY_SUBMISSIONS_TABLE.c.idempotency_key == idempotency_key,
        )
    ).mappings().one_or_none()
    if existing_by_key is not None:
        existing = dict(existing_by_key)
        if existing["request_digest"] != request_digest:
            raise CanonicalIntakeBlocked(
                "BLOCKED_IDEMPOTENCY_KEY_REUSE",
                "idempotency key is bound to another request digest",
            )
        return _accepted_result(effective, existing, idempotent_replay=True)

    existing_by_source = effective.execute(
        select(STRATEGY_SUBMISSIONS_TABLE).where(
            STRATEGY_SUBMISSIONS_TABLE.c.source_archive_digest
            == selected.archive_snapshot_digest,
            STRATEGY_SUBMISSIONS_TABLE.c.source_entry_key
            == selected.source_entry_key,
        )
    ).mappings().one_or_none()
    if existing_by_source is not None:
        existing = dict(existing_by_source)
        if existing["request_digest"] != request_digest:
            raise CanonicalIntakeBlocked(
                "BLOCKED_SOURCE_ENTRY_DRIFT",
                "source entry is already bound to another semantic request",
            )
        return _accepted_result(effective, existing, idempotent_replay=True)

    now = datetime.now(timezone.utc)
    artifact_row = effective.execute(
        select(STRATEGY_ARTIFACTS_TABLE).where(
            STRATEGY_ARTIFACTS_TABLE.c.content_digest == inspection.content_digest
        )
    ).mappings().one_or_none()
    if artifact_row is None:
        artifact_id = uuid4()
        effective.execute(
            STRATEGY_ARTIFACTS_TABLE.insert().values(
                id=artifact_id,
                content_digest=inspection.content_digest,
                encoding="UTF-8",
                size_bytes=len(inspection.normalized_bytes),
                normalized_content=inspection.normalized_content,
                created_at=now,
            )
        )
    else:
        artifact = dict(artifact_row)
        artifact_id = artifact["id"]
        if (
            artifact["normalized_content"] != inspection.normalized_content
            or artifact["size_bytes"] != len(inspection.normalized_bytes)
            or artifact["encoding"] != "UTF-8"
        ):
            raise CanonicalIntakeBlocked(
                "BLOCKED_ARTIFACT_DIGEST_COLLISION",
                "existing artifact bytes disagree with the content digest",
            )

    submission_id = uuid4()
    strategy_id = uuid4()
    version_id = uuid4()
    receipt_id = uuid4()
    receipt_payload = {
        **request_payload,
        "submission_id": str(submission_id),
        "artifact_id": str(artifact_id),
        "strategy_id": str(strategy_id),
        "strategy_version_id": str(version_id),
        "outcome": "INTAKE_ACCEPTED",
        "catalog_status": "DRAFT",
        "validation_status": "UNVALIDATED",
        "execution_authorized": False,
        "checks": inspection.checks,
    }
    receipt_digest = _digest_json(receipt_payload)
    effective.execute(
        STRATEGY_SUBMISSIONS_TABLE.insert().values(
            id=submission_id,
            caller_identity=caller_identity,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            source_archive_digest=selected.archive_snapshot_digest,
            source_entry_key=selected.source_entry_key,
            artifact_id=artifact_id,
            status="INTAKE_ACCEPTED",
            reason_code=None,
            received_at=now,
        )
    )
    effective.execute(
        STRATEGIES_TABLE.insert().values(
            id=strategy_id,
            source_submission_id=submission_id,
            catalog_status="DRAFT",
            display_name=display_name,
            created_at=now,
        )
    )
    effective.execute(
        STRATEGY_VERSIONS_TABLE.insert().values(
            id=version_id,
            strategy_id=strategy_id,
            artifact_id=artifact_id,
            version_number=1,
            validation_status="UNVALIDATED",
            execution_authorized=False,
            created_at=now,
        )
    )
    effective.execute(
        STRATEGY_INTAKE_RECEIPTS_TABLE.insert().values(
            id=receipt_id,
            submission_id=submission_id,
            archive_snapshot_digest=selected.archive_snapshot_digest,
            source_entry_digest=selected.source_entry_digest,
            artifact_digest=inspection.content_digest,
            submission_digest=request_digest,
            receipt_digest=receipt_digest,
            status="INTAKE_ACCEPTED",
            checks_json=inspection.checks,
            created_at=now,
        )
    )
    effective.execute(
        IDEMPOTENCY_RECEIPTS_TABLE.insert().values(
            id=uuid4(),
            actor_identity=caller_identity,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            receipt_digest=receipt_digest,
            outcome="INTAKE_ACCEPTED",
            evidence_json={
                "submission_id": str(submission_id),
                "strategy_id": str(strategy_id),
                "strategy_version_id": str(version_id),
            },
            created_at=now,
        )
    )
    effective.execute(
        AUDIT_EVENTS_TABLE.insert().values(
            id=uuid4(),
            event_type="STRATEGY_INTAKE_ACCEPTED",
            aggregate_type="strategy_submission",
            aggregate_id=str(submission_id),
            actor_identity=caller_identity,
            request_digest=request_digest,
            receipt_digest=receipt_digest,
            evidence_json=receipt_payload,
            created_at=now,
        )
    )
    return ControlledIntakeResult(
        submission_id=submission_id,
        artifact_id=artifact_id,
        strategy_id=strategy_id,
        strategy_version_id=version_id,
        intake_receipt_id=receipt_id,
        request_digest=request_digest,
        artifact_digest=inspection.content_digest,
        receipt_digest=receipt_digest,
        status="INTAKE_ACCEPTED",
        catalog_status="DRAFT",
        validation_status="UNVALIDATED",
        execution_authorized=False,
        idempotent_replay=False,
    )


__all__ = [
    "CanonicalIntakeBlocked",
    "ControlledIntakeResult",
    "DEFAULT_MAX_ARTIFACT_BYTES",
    "ExternalSourceEntrySnapshot",
    "ExternalVersionSnapshot",
    "INTAKE_SAFETY_CONTRACT",
    "IntakeInspection",
    "SelectedLatestArtifact",
    "controlled_submit_latest",
    "inspect_intake_artifact",
    "select_latest_source_artifact",
]
