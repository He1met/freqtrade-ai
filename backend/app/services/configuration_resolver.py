from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core.strategy_platform_errors import StrategyPlatformReadError
from app.models.strategy_platform import (
    ConfigurationBundleSnapshot,
    ConfigurationVersion,
)
from app.models.strategy_platform_extensions import AdapterDefinition
from app.repositories.strategy_platform import StrategyPlatformConfigurationRepository
from app.services.strategy_platform_adapter_registry import (
    installed_adapter_manifest_digest,
    validate_declared_adapter_coverage,
)
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
            resolved_by_id[version.id] = version
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
        resolved_versions_json = {
            _bundle_map_key(row): row.id for row in ordered_versions
        }
        resolved_digests_json = {
            _bundle_map_key(row): row.config_digest for row in ordered_versions
        }
        capability_snapshot = {
            **_SAFETY_CAPABILITY,
            "resolution_contract": "strategy-platform-owner-resolver-v1",
            "resolved_type_count": len(ordered_versions),
            **_adapter_registry_capability(self.db),
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
        for map_key, version_id in sorted(version_map.items()):
            version = versions_by_id[version_id]
            expected_type = _bundle_map_expected_type(map_key, version_id)
            self._validate_version(
                version,
                expected_type=expected_type,
                allow_historical=True,
            )
            if version.config_digest != digest_map[map_key]:
                raise StrategyPlatformReadError(
                    "BUNDLE_VERSION_DIGEST_MISMATCH",
                    "Configuration bundle references a version with a "
                    "different digest.",
                    context={"bundle_id": snapshot.id, "config_type": expected_type},
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
        validate_configuration_payload_for_type(
            type_row,
            version.payload_json,
            schema_version=version.schema_version,
            version_id=version.id,
        )

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


def validate_configuration_type_schema_registry(type_row: Any) -> None:
    """Validate the auditable schema registry declared by one config type.

    Existing single-schema types remain an exact one-version registry.  Once a
    type declares ``schema_versions``, its legacy ``json_schema`` field remains
    the current-schema projection and must be structurally equivalent to the
    registry entry named by ``ConfigurationType.schema_version``.
    """

    schemas = _configuration_schema_versions(type_row)
    # Import locally because the lifecycle service depends on this resolver.
    # Calls happen only after both modules have completed initialization.
    from app.services.configuration_management import _validate_schema_definition

    for schema_version, schema in sorted(schemas.items()):
        _validate_schema_definition(
            schema,
            path=f"$schema_versions.{schema_version}",
        )


def validate_configuration_payload_for_type(
    type_row: Any,
    payload: Any,
    *,
    schema_version: str,
    version_id: int,
) -> None:
    """Validate a version against its own exact, registered schema."""

    schemas = _configuration_schema_versions(type_row)
    schema = schemas.get(schema_version)
    if schema is None:
        raise StrategyPlatformReadError(
            "CONFIGURATION_SCHEMA_UNKNOWN",
            "Configuration version references an unregistered schema version.",
            context={
                "config_type": type_row.type_key,
                "version_id": version_id,
                "version_schema": schema_version,
                "registered_schema_versions": sorted(schemas),
            },
        )
    validate_configuration_payload(payload, version_id=version_id)
    # See the module-cycle note in validate_configuration_type_schema_registry.
    from app.services.configuration_management import (
        _validate_json_value,
        _validate_schema_definition,
    )

    _validate_schema_definition(
        schema,
        path=f"$schema_versions.{schema_version}",
    )
    _validate_json_value(payload, schema, path="$", version_id=version_id)
def _configuration_schema_versions(type_row: Any) -> dict[str, Mapping[str, Any]]:
    capability = type_row.editor_capability
    if not isinstance(capability, Mapping):
        raise StrategyPlatformReadError(
            "CONFIGURATION_SCHEMA_UNAVAILABLE",
            "Configuration type has no auditable schema capability.",
            context={"config_type": type_row.type_key},
        )
    current_schema = capability.get("json_schema")
    if not isinstance(current_schema, Mapping):
        raise StrategyPlatformReadError(
            "CONFIGURATION_SCHEMA_UNAVAILABLE",
            "Configuration type has no strict current JSON schema.",
            context={"config_type": type_row.type_key},
        )
    declared = capability.get("schema_versions")
    if declared is None:
        # Backward-compatible representation for types not yet evolved: the
        # sole known schema is still exact and unknown historical versions fail.
        return {type_row.schema_version: current_schema}
    if not isinstance(declared, Mapping) or not declared:
        raise StrategyPlatformReadError(
            "CONFIGURATION_SCHEMA_REGISTRY_INVALID",
            "Configuration schema_versions must be a non-empty object.",
            context={"config_type": type_row.type_key},
        )
    schemas: dict[str, Mapping[str, Any]] = {}
    for schema_version, schema in declared.items():
        if (
            not isinstance(schema_version, str)
            or not schema_version.strip()
            or schema_version != schema_version.strip()
            or not isinstance(schema, Mapping)
        ):
            raise StrategyPlatformReadError(
                "CONFIGURATION_SCHEMA_REGISTRY_INVALID",
                "Configuration schema_versions contains an invalid entry.",
                context={"config_type": type_row.type_key},
            )
        schemas[schema_version] = schema
    registered_current = schemas.get(type_row.schema_version)
    if registered_current is None:
        raise StrategyPlatformReadError(
            "CONFIGURATION_SCHEMA_CURRENT_VERSION_MISSING",
            "Current configuration schema is absent from schema_versions.",
            context={
                "config_type": type_row.type_key,
                "current_schema_version": type_row.schema_version,
                "registered_schema_versions": sorted(schemas),
            },
        )
    if dict(current_schema) != dict(registered_current):
        raise StrategyPlatformReadError(
            "CONFIGURATION_SCHEMA_DRIFT",
            "Current json_schema does not match its schema_versions entry.",
            context={
                "config_type": type_row.type_key,
                "current_schema_version": type_row.schema_version,
            },
        )
    return schemas


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


def _adapter_registry_capability(db: Session) -> dict[str, Any]:
    rows = db.query(AdapterDefinition).order_by(AdapterDefinition.adapter_key).all()
    # Read compatibility for pre-V1.3 owner databases: their configuration
    # graphs remain resolvable with an explicit empty-registry snapshot.  The
    # Task 1 seeder itself fails closed unless it installs the full registry.
    if any(row.contains_secret_material for row in rows):
        raise StrategyPlatformReadError(
            "ADAPTER_REGISTRY_SECRET_MATERIAL",
            "Persisted adapter registry must remain metadata-only.",
        )
    payload = [
        {
            "adapter_key": row.adapter_key,
            "adapter_kind": row.adapter_kind,
            "implementation_version": row.implementation_version,
            "input_schema_version": row.input_schema_version,
            "output_schema_version": row.output_schema_version,
            "capabilities": row.capabilities,
            "display_metadata": row.display_metadata,
            "enabled": row.enabled,
            "registry_metadata_only": row.registry_metadata_only,
            "contains_secret_material": False,
            "contains_executable_payload": row.contains_executable_payload,
        }
        for row in rows
    ]
    manifest_digest: str | None = None
    if payload:
        installed = validate_declared_adapter_coverage(
            Path(__file__).resolve().parents[3]
        )
        manifest_digest = installed_adapter_manifest_digest(installed)
        expected = [
            {
                "adapter_key": adapter.adapter_key,
                "adapter_kind": adapter.adapter_kind,
                "implementation_version": adapter.implementation_version,
                "input_schema_version": adapter.input_schema_version,
                "output_schema_version": adapter.output_schema_version,
                "capabilities": dict(adapter.capabilities),
                "display_metadata": {
                    "input_schema": adapter.input_schema,
                    "output_schema": adapter.output_schema,
                    "source_ref": adapter.source_ref,
                    "source_sha256": adapter.source_sha256,
                    "installed_manifest_digest": manifest_digest,
                },
                "enabled": True,
                "registry_metadata_only": True,
                "contains_secret_material": False,
                "contains_executable_payload": False,
            }
            for adapter in installed
        ]
        if payload != expected:
            raise StrategyPlatformReadError(
                "ADAPTER_REGISTRY_DRIFT",
                "Persisted adapter registry does not match installed implementations.",
            )
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    capability = {
        "adapter_registry_contract": "strategy-platform-adapter-registry-v1",
        "adapter_registry_digest": hashlib.sha256(
            serialized.encode("utf-8")
        ).hexdigest(),
        "adapter_registry_keys": [row["adapter_key"] for row in payload],
        "resolved_adapter_count": len(payload),
    }
    if manifest_digest is not None:
        capability["installed_adapter_manifest_digest"] = manifest_digest
    return capability


def _bundle_map_key(version: ConfigurationVersion) -> str:
    """Allow registry graphs to carry multiple independently versioned members."""

    return f"{version.type_key}:{version.id}"


def _bundle_map_expected_type(map_key: str, version_id: int) -> str:
    suffix = f":{version_id}"
    if map_key.endswith(suffix) and len(map_key) > len(suffix):
        return map_key[: -len(suffix)]
    # Read compatibility for pre-V1.3 snapshots whose key was just type_key.
    return map_key


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
