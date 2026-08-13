from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from sqlalchemy import bindparam, text

from app.core.strategy_platform_errors import StrategyPlatformReadError
from app.schemas.strategy_platform import ConfigurationBundleSnapshotRead
from app.services.frozen_configuration_bundle import (
    VerifiedConfigurationBundle,
    validate_frozen_configuration_bundle,
)


_DESIGN_LAB_DATABASE = "freqtrade_ai_design_lab"
_DESIGN_LAB_SCHEMA_VERSION = "20260813_47"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class _MappingResult(Protocol):
    def one_or_none(self) -> Mapping[str, Any] | None: ...

    def all(self) -> Sequence[Mapping[str, Any]]: ...


class _Result(Protocol):
    def scalar_one(self) -> Any: ...

    def scalar_one_or_none(self) -> Any | None: ...

    def mappings(self) -> _MappingResult: ...


class ReadOnlyBundleConnection(Protocol):
    """Minimal SQLAlchemy-compatible surface required by the runtime reader."""

    def execute(
        self,
        statement: Any,
        parameters: Mapping[str, Any] | None = None,
    ) -> _Result: ...


class RuntimeConfigurationBundleReader:
    """Read one immutable bundle through the existing V1.3 SELECT-only ACL.

    The reader intentionally avoids ORM models and the owner-only resolver.  It
    selects a fixed field projection from the five relations already granted
    to the runtime role by Task1, recomputes the current adapter
    registry digest, and delegates all snapshot validation to the pure DTO
    validator.
    """

    def __init__(
        self,
        connection: ReadOnlyBundleConnection,
        *,
        installed_adapter_manifest_digest: str,
    ) -> None:
        if not isinstance(installed_adapter_manifest_digest, str) or not (
            _SHA256_PATTERN.fullmatch(installed_adapter_manifest_digest)
        ):
            _blocked(
                "INSTALLED_ADAPTER_MANIFEST_DIGEST_INVALID",
                "Installed adapter manifest digest must be a lowercase SHA-256 value.",
            )
        self._connection = connection
        self._installed_adapter_manifest_digest = installed_adapter_manifest_digest

    def read_validated(self, bundle_id: int) -> VerifiedConfigurationBundle:
        if (
            isinstance(bundle_id, bool)
            or not isinstance(bundle_id, int)
            or bundle_id <= 0
        ):
            _blocked(
                "CONFIGURATION_BUNDLE_ID_INVALID",
                "Frozen configuration bundle id must be a positive integer.",
            )
        database_name = self._connection.execute(text("SELECT current_database()"))
        if database_name.scalar_one() != _DESIGN_LAB_DATABASE:
            _blocked(
                "RUNTIME_CONFIGURATION_DATABASE_INVALID",
                "Runtime frozen-bundle reader is restricted to the V1.3 design lab.",
            )
        migration_version = self._connection.execute(
            text(
                "SELECT target_schema_version "
                "FROM strategy_platform_migration_runs "
                "WHERE execution_scope='DESIGN_LAB' AND status='SUCCEEDED' "
                "ORDER BY completed_at DESC,id DESC LIMIT 1"
            )
        )
        if migration_version.scalar_one_or_none() != _DESIGN_LAB_SCHEMA_VERSION:
            _blocked(
                "RUNTIME_CONFIGURATION_SCHEMA_VERSION_INVALID",
                "Runtime frozen-bundle reader requires the accepted V1.3 v47 schema.",
            )

        snapshot_row = (
            self._connection.execute(
                text(
                    "SELECT id,workflow_kind,scope_type,scope_key,"
                    "aggregate_profile_version_id,resolved_versions_json,"
                    "resolved_digests_json,bundle_digest,capability_snapshot,"
                    "created_at "
                    "FROM configuration_bundle_snapshots WHERE id=:bundle_id"
                ),
                {"bundle_id": bundle_id},
            )
            .mappings()
            .one_or_none()
        )
        if snapshot_row is None:
            _blocked(
                "CONFIGURATION_BUNDLE_NOT_FOUND",
                "Frozen configuration bundle snapshot does not exist.",
                bundle_id=bundle_id,
            )

        version_map = _positive_integer_map(snapshot_row["resolved_versions_json"])
        version_ids = sorted(set(version_map.values()))
        version_statement = text(
            "SELECT id,type_key,version_number,lifecycle_status,payload_json,"
            "schema_version,config_digest,change_summary,created_by,created_at,"
            "validated_at FROM configuration_versions WHERE id IN :version_ids "
            "ORDER BY type_key,id"
        ).bindparams(bindparam("version_ids", expanding=True))
        version_rows = list(
            self._connection.execute(
                version_statement,
                {"version_ids": version_ids},
            )
            .mappings()
            .all()
        )
        dependency_statement = text(
            "SELECT dependency.configuration_version_id,"
            "parent.type_key AS configuration_type,"
            "dependency.depends_on_version_id,"
            "child.type_key AS depends_on_type,dependency.relation_key "
            "FROM configuration_dependencies AS dependency "
            "JOIN configuration_versions AS parent "
            "ON parent.id=dependency.configuration_version_id "
            "JOIN configuration_versions AS child "
            "ON child.id=dependency.depends_on_version_id "
            "WHERE dependency.configuration_version_id IN :version_ids "
            "ORDER BY dependency.configuration_version_id,"
            "dependency.relation_key,dependency.depends_on_version_id"
        ).bindparams(bindparam("version_ids", expanding=True))
        dependency_rows = list(
            self._connection.execute(
                dependency_statement,
                {"version_ids": version_ids},
            )
            .mappings()
            .all()
        )

        adapter_rows = list(
            self._connection.execute(
                text(
                    "SELECT adapter_key,adapter_kind,implementation_version,"
                    "input_schema_version,output_schema_version,capabilities,"
                    "display_metadata,enabled,registry_metadata_only,"
                    "contains_secret_material,contains_executable_payload "
                    "FROM adapter_definitions ORDER BY adapter_key"
                )
            )
            .mappings()
            .all()
        )
        adapter_registry_digest = _adapter_registry_digest(adapter_rows)
        capability = _mapping(snapshot_row["capability_snapshot"], "capability")
        if capability.get("installed_adapter_manifest_digest") != (
            self._installed_adapter_manifest_digest
        ):
            _blocked(
                "INSTALLED_ADAPTER_MANIFEST_DIGEST_MISMATCH",
                "Frozen bundle does not match the installed adapter manifest.",
            )

        snapshot = ConfigurationBundleSnapshotRead.model_validate(
            {
                "persisted": True,
                "snapshot_id": snapshot_row["id"],
                "workflow_kind": snapshot_row["workflow_kind"],
                "scope_type": snapshot_row["scope_type"],
                "scope_key": snapshot_row["scope_key"],
                "aggregate_profile_version_id": snapshot_row[
                    "aggregate_profile_version_id"
                ],
                "resolved_versions": version_rows,
                "dependencies": dependency_rows,
                "resolved_versions_json": version_map,
                "resolved_digests_json": _mapping(
                    snapshot_row["resolved_digests_json"], "resolved digests"
                ),
                "bundle_digest": snapshot_row["bundle_digest"],
                "capability_snapshot": capability,
                "created_at": snapshot_row["created_at"],
            }
        )
        return validate_frozen_configuration_bundle(
            snapshot,
            expected_adapter_registry_digest=adapter_registry_digest,
        )


def _adapter_registry_digest(rows: Sequence[Mapping[str, Any]]) -> str:
    payload: list[dict[str, Any]] = []
    for row in rows:
        if row["contains_secret_material"] is not False:
            _blocked(
                "ADAPTER_REGISTRY_SECRET_MATERIAL",
                "Runtime adapter registry must remain metadata-only.",
            )
        if row["contains_executable_payload"] is not False:
            _blocked(
                "ADAPTER_REGISTRY_EXECUTABLE_PAYLOAD",
                "Runtime adapter registry must not contain executable payloads.",
            )
        payload.append(
            {
                "adapter_key": row["adapter_key"],
                "adapter_kind": row["adapter_kind"],
                "implementation_version": row["implementation_version"],
                "input_schema_version": row["input_schema_version"],
                "output_schema_version": row["output_schema_version"],
                "capabilities": _json_value(row["capabilities"]),
                "display_metadata": _json_value(row["display_metadata"]),
                "enabled": row["enabled"],
                "registry_metadata_only": row["registry_metadata_only"],
                "contains_secret_material": (row["contains_secret_material"]),
                "contains_executable_payload": row["contains_executable_payload"],
            }
        )
    if not payload:
        _blocked(
            "ADAPTER_REGISTRY_EMPTY",
            "V1.3 runtime requires a non-empty installed adapter registry.",
        )
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _positive_integer_map(value: Any) -> dict[str, int]:
    mapping = _mapping(value, "resolved versions")
    result: dict[str, int] = {}
    for key, item in mapping.items():
        if (
            not isinstance(key, str)
            or not key
            or isinstance(item, bool)
            or not isinstance(item, int)
            or item <= 0
        ):
            _blocked("BUNDLE_MAP_INVALID", "Resolved version map is invalid.")
        result[key] = item
    if not result:
        _blocked("BUNDLE_MAP_INVALID", "Resolved version map cannot be empty.")
    return result


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    parsed = _json_value(value)
    if not isinstance(parsed, Mapping):
        _blocked("BUNDLE_MAP_INVALID", f"Configuration bundle {label} is invalid.")
    return parsed


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise StrategyPlatformReadError(
                "BUNDLE_JSON_INVALID",
                "Runtime configuration projection contains invalid JSON.",
            ) from exc
    return value


def _blocked(code: str, message: str, **context: Any) -> None:
    raise StrategyPlatformReadError(code, message, context=context)
