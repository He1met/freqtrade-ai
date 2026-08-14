"""Shared read-only helpers for capability-separated execution services."""

from __future__ import annotations

from hashlib import sha256
import json

from sqlalchemy import Connection

from app.canonical_v13.genesis import verify_canonical_genesis
from app.canonical_v13.manifest import CANONICAL_BUSINESS_SCHEMA


class CanonicalExecutionChainBlocked(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def canonical_execution_digest(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_NON_CANONICAL_EXECUTION_EVIDENCE",
            "execution evidence must be finite canonical JSON",
        ) from exc
    return sha256(encoded).hexdigest()


def require_canonical_execution(connection: Connection) -> Connection:
    effective = connection
    if connection.dialect.name == "sqlite":
        effective = connection.execution_options(
            schema_translate_map={CANONICAL_BUSINESS_SCHEMA: None}
        )
    verification = verify_canonical_genesis(effective)
    if not verification.accepted:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_WRONG_CANONICAL_DATABASE", "; ".join(verification.problems)
        )
    return effective


def require_identity(value: str, *, field: str, maximum: int = 200) -> str:
    if not value or value != value.strip() or len(value) > maximum:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_EXECUTION_IDENTITY", f"{field} is invalid"
        )
    return value


def require_digest(value: str, *, field: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_EXECUTION_DIGEST", f"{field} is not lowercase SHA-256"
        )
    return value


__all__ = [
    "CanonicalExecutionChainBlocked",
    "canonical_execution_digest",
    "require_canonical_execution",
    "require_digest",
    "require_identity",
]
