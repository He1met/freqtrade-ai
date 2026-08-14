"""Audited and idempotent command boundary for canonical P0 configuration."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from uuid import UUID, uuid4

from sqlalchemy import Connection, func, select

from app.canonical_v13.control_plane import (
    ConfigurationDependencyInput,
    canonical_digest,
    create_configuration_draft,
    validate_configuration_version,
)
from app.canonical_v13.models import (
    AUDIT_EVENTS_TABLE,
    CONFIGURATION_PROFILES_TABLE,
    CONFIGURATION_SNAPSHOTS_TABLE,
    CONFIGURATION_VERSIONS_TABLE,
    IDEMPOTENCY_RECEIPTS_TABLE,
)


_CONTRACT = "canonical-v13-audited-configuration-command-v1"


class CanonicalConfigurationGovernanceBlocked(RuntimeError):
    """Fail-closed configuration command boundary error."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _identity(value: object, *, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise CanonicalConfigurationGovernanceBlocked(
            "BLOCKED_INVALID_CONFIGURATION_AUTHORITY",
            f"{field} must be a non-empty bounded identity",
        )
    return value.strip()


def _existing_receipt(
    connection: Connection, *, actor_identity: str, idempotency_key: str
) -> Mapping[str, Any] | None:
    statement = select(IDEMPOTENCY_RECEIPTS_TABLE).where(
        IDEMPOTENCY_RECEIPTS_TABLE.c.actor_identity == actor_identity,
        IDEMPOTENCY_RECEIPTS_TABLE.c.idempotency_key == idempotency_key,
    )
    if connection.dialect.name != "sqlite":
        statement = statement.with_for_update()
    return connection.execute(statement).mappings().one_or_none()


def _assert_persisted_response(
    connection: Connection, *, command_type: str, response: Mapping[str, Any]
) -> None:
    version_id = UUID(str(response.get("version_id")))
    if command_type == "CONFIGURATION_DRAFT_CREATED":
        row = connection.execute(
            select(
                CONFIGURATION_VERSIONS_TABLE.c.id,
                CONFIGURATION_VERSIONS_TABLE.c.schema_digest,
                CONFIGURATION_VERSIONS_TABLE.c.payload_digest,
                CONFIGURATION_PROFILES_TABLE.c.configuration_kind,
            ).join(
                CONFIGURATION_PROFILES_TABLE,
                CONFIGURATION_PROFILES_TABLE.c.id
                == CONFIGURATION_VERSIONS_TABLE.c.profile_id,
            ).where(CONFIGURATION_VERSIONS_TABLE.c.id == version_id)
        ).mappings().one_or_none()
        if (
            row is None
            or row["configuration_kind"] != response.get("configuration_kind")
            or row["schema_digest"] != response.get("schema_digest")
            or row["payload_digest"] != response.get("payload_digest")
        ):
            raise CanonicalConfigurationGovernanceBlocked(
                "BLOCKED_CONFIGURATION_RECEIPT_DRIFT",
                "configuration draft receipt no longer matches canonical state",
            )
        return
    row = connection.execute(
        select(CONFIGURATION_SNAPSHOTS_TABLE).where(
            CONFIGURATION_SNAPSHOTS_TABLE.c.configuration_version_id == version_id
        )
    ).mappings().one_or_none()
    if (
        row is None
        or str(row["id"]) != str(response.get("snapshot_id"))
        or row["snapshot_digest"] != response.get("snapshot_digest")
        or row["dependency_digest"] != response.get("dependency_digest")
    ):
        raise CanonicalConfigurationGovernanceBlocked(
            "BLOCKED_CONFIGURATION_RECEIPT_DRIFT",
            "configuration validation receipt no longer matches canonical state",
        )


def _replay_or_execute(
    connection: Connection,
    *,
    actor_identity: str,
    idempotency_key: str,
    command_type: str,
    aggregate_type: str,
    request_payload: Mapping[str, Any],
    execute: Callable[[], Mapping[str, Any]],
) -> dict[str, Any]:
    request_digest = canonical_digest(request_payload)
    existing = _existing_receipt(
        connection,
        actor_identity=actor_identity,
        idempotency_key=idempotency_key,
    )
    if existing is not None:
        if existing["request_digest"] != request_digest:
            raise CanonicalConfigurationGovernanceBlocked(
                "BLOCKED_IDEMPOTENCY_KEY_REUSE",
                "idempotency key is bound to another configuration command",
            )
        evidence = existing["evidence_json"]
        if not isinstance(evidence, Mapping):
            raise CanonicalConfigurationGovernanceBlocked(
                "BLOCKED_CONFIGURATION_RECEIPT_DRIFT",
                "configuration receipt evidence is not an object",
            )
        response = evidence.get("response")
        receipt_payload = evidence.get("receipt_payload")
        if (
            existing["outcome"] != command_type
            or evidence.get("contract") != _CONTRACT
            or evidence.get("command_type") != command_type
            or str(evidence.get("receipt_id")) != str(existing["id"])
            or not isinstance(response, Mapping)
            or not isinstance(receipt_payload, Mapping)
            or canonical_digest(receipt_payload) != existing["receipt_digest"]
        ):
            raise CanonicalConfigurationGovernanceBlocked(
                "BLOCKED_CONFIGURATION_RECEIPT_DRIFT",
                "configuration idempotency receipt failed integrity checks",
            )
        _assert_persisted_response(
            connection, command_type=command_type, response=response
        )
        audit_count = int(
            connection.execute(
                select(func.count()).select_from(AUDIT_EVENTS_TABLE).where(
                    AUDIT_EVENTS_TABLE.c.event_type == command_type,
                    AUDIT_EVENTS_TABLE.c.actor_identity == actor_identity,
                    AUDIT_EVENTS_TABLE.c.request_digest == request_digest,
                    AUDIT_EVENTS_TABLE.c.receipt_digest == existing["receipt_digest"],
                )
            ).scalar_one()
        )
        if audit_count != 1:
            raise CanonicalConfigurationGovernanceBlocked(
                "BLOCKED_CONFIGURATION_AUDIT_DRIFT",
                "configuration receipt requires exactly one audit event",
            )
        return {
            **dict(response),
            "idempotency_receipt_id": existing["id"],
            "receipt_digest": existing["receipt_digest"],
            "idempotent_replay": True,
        }

    response = dict(execute())
    receipt_id = uuid4()
    now = datetime.now(timezone.utc)
    receipt_payload = {
        "contract": _CONTRACT,
        "command_type": command_type,
        "actor_identity": actor_identity,
        "idempotency_key": idempotency_key,
        "request_digest": request_digest,
        "response": response,
    }
    receipt_digest = canonical_digest(receipt_payload)
    evidence = {
        "contract": _CONTRACT,
        "command_type": command_type,
        "receipt_id": str(receipt_id),
        "response": response,
        "receipt_payload": receipt_payload,
    }
    connection.execute(
        IDEMPOTENCY_RECEIPTS_TABLE.insert().values(
            id=receipt_id,
            actor_identity=actor_identity,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            receipt_digest=receipt_digest,
            outcome=command_type,
            evidence_json=evidence,
            created_at=now,
        )
    )
    connection.execute(
        AUDIT_EVENTS_TABLE.insert().values(
            id=uuid4(),
            event_type=command_type,
            aggregate_type=aggregate_type,
            aggregate_id=str(response["version_id"]),
            actor_identity=actor_identity,
            request_digest=request_digest,
            receipt_digest=receipt_digest,
            evidence_json=evidence,
            created_at=now,
        )
    )
    return {
        **response,
        "idempotency_receipt_id": receipt_id,
        "receipt_digest": receipt_digest,
        "idempotent_replay": False,
    }


def create_audited_configuration_draft(
    connection: Connection,
    *,
    actor_identity: str,
    idempotency_key: str,
    profile_key: str,
    configuration_kind: str,
    scope_key: str,
    workflow_key: str,
    schema_json: Mapping[str, Any],
    payload_json: Mapping[str, Any],
    adapter_identity: str,
    adapter_digest: str,
    dependencies: tuple[ConfigurationDependencyInput, ...],
) -> dict[str, Any]:
    """Create one configuration draft with atomic receipt and audit evidence."""

    actor = _identity(actor_identity, field="actor_identity", maximum=160)
    key = _identity(idempotency_key, field="idempotency_key", maximum=200)
    request_payload = {
        "contract": _CONTRACT,
        "command": "CREATE_DRAFT",
        "actor_identity": actor,
        "configuration_kind": configuration_kind,
        "profile_key": profile_key,
        "scope_key": scope_key,
        "workflow_key": workflow_key,
        "schema_json": schema_json,
        "payload_json": payload_json,
        "adapter_identity": adapter_identity,
        "adapter_digest": adapter_digest,
        "dependencies": [
            {
                "version_id": str(item.version_id),
                "expected_kind": item.expected_kind,
                "relation_key": item.relation_key,
            }
            for item in dependencies
        ],
    }

    def execute() -> Mapping[str, Any]:
        result = create_configuration_draft(
            connection,
            profile_key=profile_key,
            configuration_kind=configuration_kind,
            scope_key=scope_key,
            workflow_key=workflow_key,
            schema_json=schema_json,
            payload_json=payload_json,
            adapter_identity=adapter_identity,
            adapter_digest=adapter_digest,
            dependencies=dependencies,
        )
        return {
            "profile_id": str(result.profile_id),
            "version_id": str(result.version_id),
            "version_number": result.version_number,
            "configuration_kind": result.configuration_kind,
            "lifecycle_status": result.lifecycle_status,
            "schema_digest": result.schema_digest,
            "payload_digest": result.payload_digest,
        }

    return _replay_or_execute(
        connection,
        actor_identity=actor,
        idempotency_key=key,
        command_type="CONFIGURATION_DRAFT_CREATED",
        aggregate_type="configuration_version",
        request_payload=request_payload,
        execute=execute,
    )


def validate_audited_configuration_version(
    connection: Connection,
    *,
    actor_identity: str,
    idempotency_key: str,
    configuration_kind: str,
    version_id: UUID,
    adapter_manifest_digest: str,
) -> dict[str, Any]:
    """Freeze one configuration snapshot with atomic receipt and audit evidence."""

    actor = _identity(actor_identity, field="actor_identity", maximum=160)
    key = _identity(idempotency_key, field="idempotency_key", maximum=200)
    request_payload = {
        "contract": _CONTRACT,
        "command": "VALIDATE_VERSION",
        "actor_identity": actor,
        "configuration_kind": configuration_kind,
        "version_id": str(version_id),
        "adapter_manifest_digest": adapter_manifest_digest,
    }

    def execute() -> Mapping[str, Any]:
        result = validate_configuration_version(
            connection,
            version_id=version_id,
            adapter_manifest_digest=adapter_manifest_digest,
        )
        if result.configuration_kind != configuration_kind:
            raise CanonicalConfigurationGovernanceBlocked(
                "BLOCKED_CONFIGURATION_KIND_MISMATCH",
                "route kind differs from the configuration version",
            )
        return {
            "snapshot_id": str(result.snapshot_id),
            "version_id": str(result.version_id),
            "configuration_kind": result.configuration_kind,
            "lifecycle_status": "VALIDATED",
            "snapshot_digest": result.snapshot_digest,
            "dependency_digest": result.dependency_digest,
            "member_count": result.member_count,
            "target_count": result.target_count,
            "total_candidate_count": result.total_candidate_count,
            "repeat_noop": result.repeat_noop,
        }

    return _replay_or_execute(
        connection,
        actor_identity=actor,
        idempotency_key=key,
        command_type="CONFIGURATION_VERSION_VALIDATED",
        aggregate_type="configuration_snapshot",
        request_payload=request_payload,
        execute=execute,
    )


__all__ = [
    "CanonicalConfigurationGovernanceBlocked",
    "create_audited_configuration_draft",
    "validate_audited_configuration_version",
]
