from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.core.strategy_platform_errors import StrategyPlatformReadError
from app.schemas.strategy_platform import (
    ConfigurationBundleSnapshotRead,
    ConfigurationVersionRead,
)


_BUNDLE_DIGEST_CONTRACT = "configuration-bundle-digest-v1"
_VERSION_DIGEST_CONTRACT = "configuration-version-digest-v1"
_RESOLUTION_CONTRACT = "strategy-platform-owner-resolver-v1"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED_CAPABILITIES = {
    "demo_only": True,
    "allow_real_funds": False,
    "single_writer_required": True,
    "resolution_contract": _RESOLUTION_CONTRACT,
}
_FORBIDDEN_SECRET_KEYS = {
    "api_key",
    "api_secret",
    "secret_value",
    "passphrase",
    "private_key",
}
_FORBIDDEN_EXECUTABLE_KEYS = {"python_code", "callable_source", "executable_code"}


@dataclass(frozen=True)
class VerifiedConfigurationBundle:
    """A verified in-memory view of one already-fetched frozen snapshot.

    This object deliberately has no database or owner-resolver dependency.  A
    least-privilege reader must fetch the narrow DTO before runtime code can
    use this validator.
    """

    snapshot: ConfigurationBundleSnapshotRead
    versions_by_type: Mapping[str, tuple[ConfigurationVersionRead, ...]]

    def require_single_version(self, type_key: str) -> ConfigurationVersionRead:
        versions = self.versions_by_type.get(type_key, ())
        if len(versions) != 1:
            raise StrategyPlatformReadError(
                "BUNDLE_CONFIGURATION_CARDINALITY_INVALID",
                "Configuration bundle does not contain exactly one requested profile.",
                context={"config_type": type_key, "resolved_count": len(versions)},
            )
        return versions[0]


def validate_frozen_configuration_bundle(
    snapshot: ConfigurationBundleSnapshotRead | None,
    *,
    expected_adapter_registry_digest: str,
) -> VerifiedConfigurationBundle:
    """Validate a persisted V1.3 DTO without reading owner-controlled tables.

    Only formal ``<type_key>:<configuration_version_id>`` map keys are accepted.
    Bare legacy keys remain an owner-side historical read concern and cannot be
    silently promoted into the production-path contract.
    """

    if not isinstance(snapshot, ConfigurationBundleSnapshotRead):
        _blocked(
            "PERSISTED_CONFIGURATION_BUNDLE_REQUIRED",
            "Production configuration requires a persisted frozen bundle snapshot.",
        )
    if snapshot.persisted is not True or snapshot.snapshot_id <= 0:
        _blocked(
            "PERSISTED_CONFIGURATION_BUNDLE_REQUIRED",
            "Production configuration requires a persisted frozen bundle snapshot.",
        )
    _require_non_empty(snapshot.workflow_kind, "workflow_kind")
    _require_non_empty(snapshot.scope_type, "scope_type")
    _require_non_empty(snapshot.scope_key, "scope_key")
    _validate_capability_snapshot(
        snapshot.capability_snapshot,
        expected_adapter_registry_digest=expected_adapter_registry_digest,
    )

    version_map = _integer_map(snapshot.resolved_versions_json)
    digest_map = _string_map(snapshot.resolved_digests_json)
    if set(version_map) != set(digest_map):
        _blocked(
            "BUNDLE_VERSION_DIGEST_MAP_MISMATCH",
            "Configuration bundle version and digest maps must use identical keys.",
        )

    versions_by_id: dict[int, ConfigurationVersionRead] = {}
    versions_by_type: dict[str, list[ConfigurationVersionRead]] = {}
    for version in snapshot.resolved_versions:
        if version.id <= 0:
            _blocked(
                "BUNDLE_CONFIGURATION_VERSION_ID_INVALID",
                "Configuration bundle version ids must be positive.",
                version_id=version.id,
            )
        _require_non_empty(version.type_key, "version.type_key")
        _require_non_empty(version.schema_version, "version.schema_version")
        if version.id in versions_by_id:
            _blocked(
                "BUNDLE_CONFIGURATION_VERSION_DUPLICATED",
                "Configuration bundle contains a duplicated version DTO.",
                version_id=version.id,
            )
        if version.lifecycle_status != "VALIDATED":
            _blocked(
                "BUNDLE_CONFIGURATION_VERSION_NOT_VALIDATED",
                "Production configuration bundle contains a non-VALIDATED version.",
                version_id=version.id,
                lifecycle_status=version.lifecycle_status,
            )
        _validate_payload(version.payload_json, version_id=version.id)
        expected_version_digest = _configuration_version_digest(version)
        if version.config_digest != expected_version_digest:
            _blocked(
                "BUNDLE_CONFIGURATION_VERSION_DIGEST_INVALID",
                "Configuration version digest does not match its canonical payload.",
                version_id=version.id,
            )
        versions_by_id[version.id] = version
        versions_by_type.setdefault(version.type_key, []).append(version)

    if set(version_map.values()) != set(versions_by_id):
        _blocked(
            "BUNDLE_CONFIGURATION_VERSION_SET_MISMATCH",
            "Configuration bundle map and version DTOs do not identify the same graph.",
        )
    if len(version_map) != len(versions_by_id):
        _blocked(
            "BUNDLE_CONFIGURATION_VERSION_MAP_DUPLICATED",
            "Configuration bundle maps more than one key to the same version.",
        )

    for map_key, version_id in sorted(version_map.items()):
        version = versions_by_id[version_id]
        expected_key = f"{version.type_key}:{version.id}"
        if map_key != expected_key:
            _blocked(
                "BUNDLE_CONFIGURATION_MAP_KEY_INVALID",
                "Production bundle keys must include configuration type and "
                "version id.",
                map_key=map_key,
                expected_key=expected_key,
            )
        mapped_digest = digest_map[map_key]
        if mapped_digest != version.config_digest:
            _blocked(
                "BUNDLE_VERSION_DIGEST_MISMATCH",
                "Configuration bundle digest map does not match its version DTO.",
                map_key=map_key,
            )

    aggregate = versions_by_id.get(snapshot.aggregate_profile_version_id)
    if aggregate is None:
        _blocked(
            "BUNDLE_AGGREGATE_VERSION_MISSING",
            "Configuration bundle aggregate version is absent from its graph.",
            aggregate_profile_version_id=snapshot.aggregate_profile_version_id,
        )
    _validate_dependency_graph(snapshot, versions_by_id)

    expected_bundle_digest = _configuration_bundle_digest(snapshot)
    if snapshot.bundle_digest != expected_bundle_digest:
        _blocked(
            "BUNDLE_DIGEST_MISMATCH",
            "Configuration bundle digest does not match its canonical snapshot.",
            snapshot_id=snapshot.snapshot_id,
        )

    return VerifiedConfigurationBundle(
        snapshot=snapshot,
        versions_by_type={
            type_key: tuple(sorted(items, key=lambda item: item.id))
            for type_key, items in sorted(versions_by_type.items())
        },
    )


def _validate_capability_snapshot(
    capability: Any,
    *,
    expected_adapter_registry_digest: str,
) -> None:
    if not isinstance(capability, Mapping):
        _blocked(
            "BUNDLE_CAPABILITY_INVALID",
            "Configuration bundle capability snapshot must be an object.",
        )
    for key, expected in _REQUIRED_CAPABILITIES.items():
        if capability.get(key) != expected:
            _blocked(
                "BUNDLE_CAPABILITY_INVALID",
                "Configuration bundle is missing a required fail-closed capability.",
                capability=key,
            )
    if (
        not isinstance(expected_adapter_registry_digest, str)
        or not _SHA256_PATTERN.fullmatch(expected_adapter_registry_digest)
    ):
        _blocked(
            "EXPECTED_ADAPTER_REGISTRY_DIGEST_INVALID",
            "Trusted adapter registry digest must be a lowercase SHA-256 value.",
        )
    actual_registry_digest = capability.get("adapter_registry_digest")
    if actual_registry_digest != expected_adapter_registry_digest:
        _blocked(
            "ADAPTER_REGISTRY_DIGEST_MISMATCH",
            "Configuration bundle adapter registry digest does not match the "
            "trusted value.",
        )


def _validate_dependency_graph(
    snapshot: ConfigurationBundleSnapshotRead,
    versions_by_id: Mapping[int, ConfigurationVersionRead],
) -> None:
    adjacency: dict[int, set[int]] = {
        version_id: set() for version_id in versions_by_id
    }
    edge_keys: set[tuple[int, int, str]] = set()
    for dependency in snapshot.dependencies:
        _require_non_empty(dependency.relation_key, "dependency.relation_key")
        parent = versions_by_id.get(dependency.configuration_version_id)
        child = versions_by_id.get(dependency.depends_on_version_id)
        if parent is None or child is None:
            _blocked(
                "BUNDLE_DEPENDENCY_VERSION_MISSING",
                "Configuration dependency references a version outside the "
                "frozen graph.",
            )
        if (
            parent.type_key != dependency.configuration_type
            or child.type_key != dependency.depends_on_type
        ):
            _blocked(
                "BUNDLE_DEPENDENCY_TYPE_MISMATCH",
                "Configuration dependency type metadata does not match its versions.",
            )
        edge_key = (
            dependency.configuration_version_id,
            dependency.depends_on_version_id,
            dependency.relation_key,
        )
        if edge_key in edge_keys:
            _blocked(
                "BUNDLE_DEPENDENCY_DUPLICATED",
                "Configuration bundle contains a duplicated dependency edge.",
            )
        edge_keys.add(edge_key)
        adjacency[parent.id].add(child.id)

    visiting: set[int] = set()
    visited: set[int] = set()

    def visit(version_id: int) -> None:
        if version_id in visiting:
            _blocked(
                "CONFIGURATION_DEPENDENCY_CYCLE",
                "Configuration dependency graph contains a cycle.",
                version_id=version_id,
            )
        if version_id in visited:
            return
        visiting.add(version_id)
        for child_id in adjacency[version_id]:
            visit(child_id)
        visiting.remove(version_id)
        visited.add(version_id)

    visit(snapshot.aggregate_profile_version_id)
    if visited != set(versions_by_id):
        _blocked(
            "BUNDLE_CONFIGURATION_VERSION_ORPHANED",
            "Configuration bundle contains versions outside the aggregate "
            "dependency graph.",
            orphaned_version_ids=sorted(set(versions_by_id) - visited),
        )


def _validate_payload(payload: Any, *, version_id: int) -> None:
    if not isinstance(payload, Mapping):
        _blocked(
            "CONFIGURATION_PAYLOAD_INVALID",
            "Configuration payload must be a JSON object.",
            version_id=version_id,
        )

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            for raw_key, child in value.items():
                key = str(raw_key).lower()
                if key in _FORBIDDEN_SECRET_KEYS:
                    _blocked(
                        "CONFIGURATION_SECRET_ISOLATION_VIOLATION",
                        "Configuration payload contains a forbidden secret field.",
                        version_id=version_id,
                        field=key,
                    )
                if key in _FORBIDDEN_EXECUTABLE_KEYS:
                    _blocked(
                        "CONFIGURATION_EXECUTABLE_CODE_FORBIDDEN",
                        "Configuration payload contains executable code.",
                        version_id=version_id,
                        field=key,
                    )
                if key == "allow_real_funds" and child is not False:
                    _safety_block(version_id, key)
                if key == "demo_only" and child is not True:
                    _safety_block(version_id, key)
                if key == "single_writer_required" and child is not True:
                    _safety_block(version_id, key)
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)


def _safety_block(version_id: int, field: str) -> None:
    _blocked(
        "CONFIGURATION_SAFETY_INVARIANT_VIOLATION",
        "Configuration payload attempts to weaken a Demo-only safety invariant.",
        version_id=version_id,
        field=field,
    )


def _configuration_version_digest(version: ConfigurationVersionRead) -> str:
    return _sha256(
        {
            "contract": _VERSION_DIGEST_CONTRACT,
            "config_type": version.type_key,
            "schema_version": version.schema_version,
            "payload_json": version.payload_json,
        }
    )


def _configuration_bundle_digest(snapshot: ConfigurationBundleSnapshotRead) -> str:
    return _sha256(
        {
            "digest_contract": _BUNDLE_DIGEST_CONTRACT,
            "workflow_kind": snapshot.workflow_kind,
            "scope_type": snapshot.scope_type,
            "scope_key": snapshot.scope_key,
            "aggregate_profile_version_id": snapshot.aggregate_profile_version_id,
            "resolved_versions_json": dict(
                sorted(snapshot.resolved_versions_json.items())
            ),
            "resolved_digests_json": dict(
                sorted(snapshot.resolved_digests_json.items())
            ),
            "capability_snapshot": snapshot.capability_snapshot,
        }
    )


def _sha256(payload: Mapping[str, Any]) -> str:
    try:
        serialized = json.dumps(
            payload,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise StrategyPlatformReadError(
            "CONFIGURATION_CANONICAL_JSON_INVALID",
            "Configuration payload cannot be represented as canonical JSON.",
        ) from exc
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _integer_map(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping) or not value:
        _blocked("BUNDLE_MAP_INVALID", "Configuration bundle version map is invalid.")
    result: dict[str, int] = {}
    for key, item in value.items():
        if (
            not isinstance(key, str)
            or not key
            or isinstance(item, bool)
            or not isinstance(item, int)
            or item <= 0
        ):
            _blocked(
                "BUNDLE_MAP_INVALID", "Configuration bundle version map is invalid."
            )
        result[key] = item
    return result


def _string_map(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping) or not value:
        _blocked("BUNDLE_MAP_INVALID", "Configuration bundle digest map is invalid.")
    result: dict[str, str] = {}
    for key, item in value.items():
        if (
            not isinstance(key, str)
            or not key
            or not isinstance(item, str)
            or _SHA256_PATTERN.fullmatch(item) is None
        ):
            _blocked(
                "BUNDLE_MAP_INVALID", "Configuration bundle digest map is invalid."
            )
        result[key] = item
    return result


def _require_non_empty(value: str, field: str) -> None:
    if not value.strip():
        _blocked(
            "EXPLICIT_BUNDLE_IDENTITY_REQUIRED",
            "Frozen configuration bundle identity fields must be explicit.",
            field=field,
        )


def _blocked(code: str, message: str, **context: Any) -> None:
    raise StrategyPlatformReadError(code, message, context=context)
