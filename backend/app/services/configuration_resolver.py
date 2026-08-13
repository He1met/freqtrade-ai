from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from app.core.strategy_platform_errors import StrategyPlatformReadError
from app.models.strategy_platform import (
    ConfigurationBundleSnapshot,
    ConfigurationVersion,
)
from app.repositories.strategy_platform import StrategyPlatformConfigurationRepository
from app.schemas.strategy_platform import (
    ActiveConfigurationRead,
    ConfigurationBundleResolutionRead,
    ConfigurationBundleSnapshotRead,
    ConfigurationCatalogRead,
    ConfigurationDependencyRead,
    ConfigurationTypeRead,
    ConfigurationVersionListRead,
    ConfigurationVersionRead,
)

_FORBIDDEN_SECRET_KEYS = {
    "api_key",
    "api_secret",
    "secret_value",
    "passphrase",
    "private_key",
}
_FORBIDDEN_EXECUTABLE_KEYS = {"python_code", "callable_source", "executable_code"}
_SAFETY_CAPABILITY = {
    "demo_only": True,
    "allow_real_funds": False,
    "single_writer_required": True,
}


class ConfigurationResolverService:
    """Resolve and snapshot immutable configuration graphs on an owner DB session."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.repository = StrategyPlatformConfigurationRepository(db)

    def catalog(self) -> ConfigurationCatalogRead:
        self.repository.require_owner_connection()
        return ConfigurationCatalogRead(
            items=[self._type_read(row) for row in self.repository.list_types()]
        )

    def list_versions(
        self,
        *,
        config_type: str,
        scope_type: str,
        scope_key: str,
        limit: int,
    ) -> ConfigurationVersionListRead:
        self.repository.require_owner_connection()
        self._require_type(config_type, require_enabled=False)
        activation = self.repository.get_activation(
            config_type=config_type,
            scope_type=scope_type,
            scope_key=scope_key,
        )
        return ConfigurationVersionListRead(
            config_type=config_type,
            scope_type=scope_type,
            scope_key=scope_key,
            active_version_id=activation.version_id if activation is not None else None,
            items=[
                ConfigurationVersionRead.model_validate(row)
                for row in self.repository.list_versions(config_type, limit=limit)
            ],
        )

    def active_configuration(
        self,
        *,
        config_type: str,
        scope_type: str,
        scope_key: str,
    ) -> ActiveConfigurationRead:
        self.repository.require_owner_connection()
        type_row = self._require_type(config_type)
        activation = self._require_activation(
            config_type=config_type,
            scope_type=scope_type,
            scope_key=scope_key,
        )
        version = self._require_version(activation.version_id)
        self._validate_version(version, expected_type=config_type)
        return ActiveConfigurationRead(
            scope_type=scope_type,
            scope_key=scope_key,
            activated_at=activation.activated_at,
            activated_by=activation.activated_by,
            configuration_type=self._type_read(type_row),
            version=ConfigurationVersionRead.model_validate(version),
        )

    def resolve_active(
        self,
        *,
        workflow_kind: str,
        aggregate_config_type: str,
        scope_type: str,
        scope_key: str,
        lock_activation: bool = False,
    ) -> ConfigurationBundleResolutionRead:
        self.repository.require_owner_connection()
        self._require_non_empty_scope(workflow_kind, scope_type, scope_key)
        self._require_type(aggregate_config_type)
        activation = self._require_activation(
            config_type=aggregate_config_type,
            scope_type=scope_type,
            scope_key=scope_key,
            lock=lock_activation,
        )

        return self.resolve_version(
            workflow_kind=workflow_kind,
            aggregate_config_type=aggregate_config_type,
            scope_type=scope_type,
            scope_key=scope_key,
            aggregate_version_id=activation.version_id,
        )

    def resolve_version(
        self,
        *,
        workflow_kind: str,
        aggregate_config_type: str,
        scope_type: str,
        scope_key: str,
        aggregate_version_id: int,
    ) -> ConfigurationBundleResolutionRead:
        """Preview one exact validated graph without consulting an activation."""

        self.repository.require_owner_connection()
        self._require_non_empty_scope(workflow_kind, scope_type, scope_key)
        self._require_type(aggregate_config_type)

        resolved_by_id: dict[int, ConfigurationVersion] = {}
        resolved_by_type: dict[str, ConfigurationVersion] = {}
        dependency_reads: list[ConfigurationDependencyRead] = []
        visiting: set[int] = set()
        visited: set[int] = set()

        def visit(version_id: int) -> None:
            if version_id in visiting:
                raise StrategyPlatformReadError(
                    "CONFIGURATION_DEPENDENCY_CYCLE",
                    "Configuration dependency graph contains a cycle.",
                    context={"version_id": version_id},
                )
            if version_id in visited:
                return
            version = self._require_version(version_id)
            self._validate_version(version)
            existing = resolved_by_type.get(version.type_key)
            if existing is not None and existing.id != version.id:
                raise StrategyPlatformReadError(
                    "CONFIGURATION_DEPENDENCY_CONFLICT",
                    "Configuration graph resolves more than one version for a type.",
                    context={
                        "config_type": version.type_key,
                        "version_ids": sorted((existing.id, version.id)),
                    },
                )
            resolved_by_id[version.id] = version
            resolved_by_type[version.type_key] = version
            visiting.add(version.id)
            for dependency in self.repository.list_dependencies((version.id,)):
                child = self._require_version(dependency.depends_on_version_id)
                dependency_reads.append(
                    ConfigurationDependencyRead(
                        configuration_version_id=version.id,
                        configuration_type=version.type_key,
                        depends_on_version_id=child.id,
                        depends_on_type=child.type_key,
                        relation_key=dependency.relation_key,
                    )
                )
                visit(child.id)
            visiting.remove(version.id)
            visited.add(version.id)

        visit(aggregate_version_id)
        aggregate = resolved_by_id[aggregate_version_id]
        if aggregate.type_key != aggregate_config_type:
            raise StrategyPlatformReadError(
                "ACTIVE_CONFIGURATION_TYPE_MISMATCH",
                "Active aggregate version does not match the requested "
                "configuration type.",
                context={
                    "requested_type": aggregate_config_type,
                    "version_id": aggregate.id,
                    "version_type": aggregate.type_key,
                },
            )

        ordered_versions = sorted(
            resolved_by_id.values(), key=lambda row: (row.type_key, row.id)
        )
        ordered_dependencies = sorted(
            dependency_reads,
            key=lambda row: (
                row.configuration_type,
                row.configuration_version_id,
                row.relation_key,
                row.depends_on_type,
                row.depends_on_version_id,
            ),
        )
        resolved_versions_json = {row.type_key: row.id for row in ordered_versions}
        resolved_digests_json = {
            row.type_key: row.config_digest for row in ordered_versions
        }
        capability_snapshot = {
            **_SAFETY_CAPABILITY,
            "resolution_contract": "strategy-platform-owner-resolver-v1",
            "resolved_type_count": len(ordered_versions),
        }
        bundle_digest = _bundle_digest(
            workflow_kind=workflow_kind,
            scope_type=scope_type,
            scope_key=scope_key,
            aggregate_profile_version_id=aggregate.id,
            resolved_versions_json=resolved_versions_json,
            resolved_digests_json=resolved_digests_json,
            capability_snapshot=capability_snapshot,
        )
        return ConfigurationBundleResolutionRead(
            persisted=False,
            workflow_kind=workflow_kind,
            scope_type=scope_type,
            scope_key=scope_key,
            aggregate_profile_version_id=aggregate.id,
            resolved_versions=[
                ConfigurationVersionRead.model_validate(row) for row in ordered_versions
            ],
            dependencies=ordered_dependencies,
            resolved_versions_json=resolved_versions_json,
            resolved_digests_json=resolved_digests_json,
            bundle_digest=bundle_digest,
            capability_snapshot=capability_snapshot,
        )

    def materialize_bundle(
        self, resolution: ConfigurationBundleResolutionRead
    ) -> ConfigurationBundleSnapshotRead:
        """Flush an idempotent snapshot; the caller keeps transaction ownership."""

        self.repository.require_owner_connection()
        aggregate = self._require_version(resolution.aggregate_profile_version_id)
        locked_resolution = self.resolve_active(
            workflow_kind=resolution.workflow_kind,
            aggregate_config_type=aggregate.type_key,
            scope_type=resolution.scope_type,
            scope_key=resolution.scope_key,
            lock_activation=True,
        )
        if resolution.bundle_digest != locked_resolution.bundle_digest:
            raise StrategyPlatformReadError(
                "BUNDLE_RESOLUTION_STALE",
                "Configuration activation changed before the bundle snapshot "
                "was locked.",
                context={
                    "requested_bundle_digest": resolution.bundle_digest,
                    "active_bundle_digest": locked_resolution.bundle_digest,
                },
            )
        resolution = locked_resolution
        existing = self.repository.find_bundle(
            workflow_kind=resolution.workflow_kind,
            scope_type=resolution.scope_type,
            scope_key=resolution.scope_key,
            bundle_digest=resolution.bundle_digest,
        )
        snapshot = existing or self.repository.add_bundle(
            workflow_kind=resolution.workflow_kind,
            scope_type=resolution.scope_type,
            scope_key=resolution.scope_key,
            aggregate_profile_version_id=resolution.aggregate_profile_version_id,
            resolved_versions_json=resolution.resolved_versions_json,
            resolved_digests_json=resolution.resolved_digests_json,
            bundle_digest=resolution.bundle_digest,
            capability_snapshot=resolution.capability_snapshot,
        )
        self.db.refresh(snapshot)
        return self._snapshot_read(snapshot)

    def read_bundle(self, bundle_id: int) -> ConfigurationBundleSnapshotRead:
        self.repository.require_owner_connection()
        snapshot = self.repository.get_bundle(bundle_id)
        if snapshot is None:
            raise StrategyPlatformReadError(
                "CONFIGURATION_BUNDLE_NOT_FOUND",
                "Configuration bundle snapshot does not exist.",
                status_code=404,
                context={"bundle_id": bundle_id},
            )
        return self._snapshot_read(snapshot)

    def _snapshot_read(
        self, snapshot: ConfigurationBundleSnapshot
    ) -> ConfigurationBundleSnapshotRead:
        version_map = _integer_map(snapshot.resolved_versions_json, "resolved versions")
        digest_map = _string_map(snapshot.resolved_digests_json, "resolved digests")
        if set(version_map) != set(digest_map):
            raise StrategyPlatformReadError(
                "BUNDLE_VERSION_DIGEST_MAP_MISMATCH",
                "Configuration bundle version and digest maps do not have "
                "the same keys.",
                context={"bundle_id": snapshot.id},
            )
        versions = self.repository.get_versions(version_map.values())
        versions_by_id = {row.id: row for row in versions}
        if len(versions_by_id) != len(set(version_map.values())):
            raise StrategyPlatformReadError(
                "BUNDLE_CONFIGURATION_VERSION_MISSING",
                "Configuration bundle references a missing version.",
                context={"bundle_id": snapshot.id},
            )
        resolved_versions: list[ConfigurationVersion] = []
        for type_key, version_id in sorted(version_map.items()):
            version = versions_by_id[version_id]
            self._validate_version(
                version,
                expected_type=type_key,
                allow_historical=True,
            )
            if version.config_digest != digest_map[type_key]:
                raise StrategyPlatformReadError(
                    "BUNDLE_VERSION_DIGEST_MISMATCH",
                    "Configuration bundle references a version with a "
                    "different digest.",
                    context={"bundle_id": snapshot.id, "config_type": type_key},
                )
            resolved_versions.append(version)
        aggregate = versions_by_id.get(snapshot.aggregate_profile_version_id)
        if aggregate is None:
            raise StrategyPlatformReadError(
                "BUNDLE_AGGREGATE_VERSION_MISSING",
                "Configuration bundle aggregate version is absent from its "
                "resolved graph.",
                context={"bundle_id": snapshot.id},
            )
        expected_digest = _bundle_digest(
            workflow_kind=snapshot.workflow_kind,
            scope_type=snapshot.scope_type,
            scope_key=snapshot.scope_key,
            aggregate_profile_version_id=snapshot.aggregate_profile_version_id,
            resolved_versions_json=version_map,
            resolved_digests_json=digest_map,
            capability_snapshot=snapshot.capability_snapshot,
        )
        if snapshot.bundle_digest != expected_digest:
            raise StrategyPlatformReadError(
                "BUNDLE_DIGEST_MISMATCH",
                "Stored configuration bundle digest does not match its contents.",
                context={"bundle_id": snapshot.id},
            )
        validate_configuration_capability_snapshot(snapshot.capability_snapshot)
        dependencies = []
        allowed_ids = set(versions_by_id)
        for edge in self.repository.list_dependencies(allowed_ids):
            if edge.depends_on_version_id not in allowed_ids:
                continue
            parent = versions_by_id[edge.configuration_version_id]
            child = versions_by_id[edge.depends_on_version_id]
            dependencies.append(
                ConfigurationDependencyRead(
                    configuration_version_id=parent.id,
                    configuration_type=parent.type_key,
                    depends_on_version_id=child.id,
                    depends_on_type=child.type_key,
                    relation_key=edge.relation_key,
                )
            )
        return ConfigurationBundleSnapshotRead(
            snapshot_id=snapshot.id,
            workflow_kind=snapshot.workflow_kind,
            scope_type=snapshot.scope_type,
            scope_key=snapshot.scope_key,
            aggregate_profile_version_id=snapshot.aggregate_profile_version_id,
            resolved_versions=[
                ConfigurationVersionRead.model_validate(row)
                for row in sorted(
                    resolved_versions, key=lambda item: (item.type_key, item.id)
                )
            ],
            dependencies=dependencies,
            resolved_versions_json=version_map,
            resolved_digests_json=digest_map,
            bundle_digest=snapshot.bundle_digest,
            capability_snapshot=snapshot.capability_snapshot,
            created_at=snapshot.created_at,
        )

    def _require_type(self, type_key: str, *, require_enabled: bool = True):
        type_row = self.repository.get_type(type_key)
        if type_row is None:
            raise StrategyPlatformReadError(
                "CONFIGURATION_TYPE_NOT_FOUND",
                "Configuration type is not registered.",
                status_code=404,
                context={"config_type": type_key},
            )
        if require_enabled and type_row.enabled is not True:
            raise StrategyPlatformReadError(
                "CONFIGURATION_TYPE_DISABLED",
                "Configuration type is disabled.",
                context={"config_type": type_key},
            )
        return type_row

    def _require_activation(
        self,
        *,
        config_type: str,
        scope_type: str,
        scope_key: str,
        lock: bool = False,
    ):
        activation = self.repository.get_activation(
            config_type=config_type,
            scope_type=scope_type,
            scope_key=scope_key,
            lock=lock,
        )
        if activation is None:
            raise StrategyPlatformReadError(
                "ACTIVE_CONFIGURATION_NOT_FOUND",
                "No active configuration exists for the explicit scope.",
                context={
                    "config_type": config_type,
                    "scope_type": scope_type,
                    "scope_key": scope_key,
                },
            )
        return activation

    def _require_version(self, version_id: int) -> ConfigurationVersion:
        version = self.repository.get_version(version_id)
        if version is None:
            raise StrategyPlatformReadError(
                "CONFIGURATION_VERSION_NOT_FOUND",
                "Configuration version does not exist.",
                status_code=404,
                context={"version_id": version_id},
            )
        return version

    def _validate_version(
        self,
        version: ConfigurationVersion,
        *,
        expected_type: str | None = None,
        allow_historical: bool = False,
    ) -> None:
        type_row = self._require_type(
            version.type_key,
            require_enabled=not allow_historical,
        )
        if expected_type is not None and version.type_key != expected_type:
            raise StrategyPlatformReadError(
                "CONFIGURATION_VERSION_TYPE_MISMATCH",
                "Configuration version type does not match the resolved key.",
                context={
                    "expected_type": expected_type,
                    "version_id": version.id,
                    "actual_type": version.type_key,
                },
            )
        allowed_statuses = (
            {"VALIDATED", "RETIRED"} if allow_historical else {"VALIDATED"}
        )
        if version.lifecycle_status not in allowed_statuses:
            raise StrategyPlatformReadError(
                "CONFIGURATION_VERSION_NOT_VALIDATED",
                "Configuration resolution requires VALIDATED versions.",
                context={
                    "version_id": version.id,
                    "lifecycle_status": version.lifecycle_status,
                },
            )
        if not allow_historical and version.schema_version != type_row.schema_version:
            raise StrategyPlatformReadError(
                "CONFIGURATION_SCHEMA_INCOMPATIBLE",
                "Configuration version schema is incompatible with its "
                "registered type.",
                context={
                    "version_id": version.id,
                    "version_schema": version.schema_version,
                    "registered_schema": type_row.schema_version,
                },
            )
        validate_configuration_payload(version.payload_json, version_id=version.id)

    @staticmethod
    def _type_read(row) -> ConfigurationTypeRead:
        return ConfigurationTypeRead(
            type_key=row.type_key,
            name_zh=row.name_zh,
            description_zh=row.description_zh,
            schema_version=row.schema_version,
            handler_key=row.handler_key,
            editor_capability=row.editor_capability,
            enabled=row.enabled,
        )

    @staticmethod
    def _require_non_empty_scope(
        workflow_kind: str, scope_type: str, scope_key: str
    ) -> None:
        if not workflow_kind.strip() or not scope_type.strip() or not scope_key.strip():
            raise StrategyPlatformReadError(
                "EXPLICIT_SCOPE_REQUIRED",
                "Workflow kind, scope type, and scope key must be explicit.",
            )


def validate_configuration_payload(payload: Any, *, version_id: int) -> None:
    if not isinstance(payload, Mapping):
        raise StrategyPlatformReadError(
            "CONFIGURATION_PAYLOAD_INVALID",
            "Configuration payload must be a JSON object.",
            context={"version_id": version_id},
        )

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                normalized = str(key).lower()
                if normalized in _FORBIDDEN_SECRET_KEYS:
                    raise StrategyPlatformReadError(
                        "CONFIGURATION_SECRET_ISOLATION_VIOLATION",
                        "Configuration payload contains a forbidden secret field.",
                        context={"version_id": version_id, "field": normalized},
                    )
                if normalized in _FORBIDDEN_EXECUTABLE_KEYS:
                    raise StrategyPlatformReadError(
                        "CONFIGURATION_EXECUTABLE_CODE_FORBIDDEN",
                        "Configuration payload contains a forbidden "
                        "executable-code field.",
                        context={"version_id": version_id, "field": normalized},
                    )
                if normalized == "allow_real_funds" and child is True:
                    _raise_safety(version_id, normalized)
                if normalized == "demo_only" and child is False:
                    _raise_safety(version_id, normalized)
                if normalized == "single_writer_required" and child is False:
                    _raise_safety(version_id, normalized)
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)


def _raise_safety(version_id: int, field: str) -> None:
    raise StrategyPlatformReadError(
        "CONFIGURATION_SAFETY_INVARIANT_VIOLATION",
        "Configuration payload attempts to weaken a Demo-only safety invariant.",
        context={"version_id": version_id, "field": field},
    )


def validate_configuration_capability_snapshot(snapshot: Any) -> None:
    if not isinstance(snapshot, Mapping) or any(
        snapshot.get(key) != expected for key, expected in _SAFETY_CAPABILITY.items()
    ):
        raise StrategyPlatformReadError(
            "BUNDLE_SAFETY_CAPABILITY_INVALID",
            "Configuration bundle does not preserve Demo-only safety capabilities.",
        )


def _bundle_digest(
    *,
    workflow_kind: str,
    scope_type: str,
    scope_key: str,
    aggregate_profile_version_id: int,
    resolved_versions_json: Mapping[str, int],
    resolved_digests_json: Mapping[str, str],
    capability_snapshot: Mapping[str, Any],
) -> str:
    validate_configuration_capability_snapshot(capability_snapshot)
    payload = {
        "digest_contract": "configuration-bundle-digest-v1",
        "workflow_kind": workflow_kind,
        "scope_type": scope_type,
        "scope_key": scope_key,
        "aggregate_profile_version_id": aggregate_profile_version_id,
        "resolved_versions_json": dict(sorted(resolved_versions_json.items())),
        "resolved_digests_json": dict(sorted(resolved_digests_json.items())),
        "capability_snapshot": capability_snapshot,
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _integer_map(value: Any, label: str) -> dict[str, int]:
    if not isinstance(value, Mapping) or not value:
        raise StrategyPlatformReadError(
            "BUNDLE_MAP_INVALID", f"Configuration bundle {label} map is invalid."
        )
    result: dict[str, int] = {}
    for key, item in value.items():
        if (
            not isinstance(key, str)
            or isinstance(item, bool)
            or not isinstance(item, int)
        ):
            raise StrategyPlatformReadError(
                "BUNDLE_MAP_INVALID", f"Configuration bundle {label} map is invalid."
            )
        result[key] = item
    return result


def _string_map(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or not value:
        raise StrategyPlatformReadError(
            "BUNDLE_MAP_INVALID", f"Configuration bundle {label} map is invalid."
        )
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise StrategyPlatformReadError(
                "BUNDLE_MAP_INVALID", f"Configuration bundle {label} map is invalid."
            )
        result[key] = item
    return result
