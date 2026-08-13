from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.core.strategy_platform_errors import StrategyPlatformReadError
from app.models.strategy_platform import ConfigurationVersion
from app.repositories.strategy_platform import StrategyPlatformConfigurationRepository
from app.schemas.strategy_platform import (
    ConfigurationAuditEventListRead,
    ConfigurationAuditEventRead,
    ConfigurationBundleSnapshotListRead,
    ConfigurationDependencyRead,
    ConfigurationDependencyWrite,
    ConfigurationDiffEntryRead,
    ConfigurationDraftCreateRequest,
    ConfigurationVersionActionRequest,
    ConfigurationVersionDetailRead,
    ConfigurationVersionDiffRead,
    ConfigurationVersionRead,
    ConfigurationWriteResult,
)
from app.services.configuration_resolver import (
    ConfigurationResolverService,
    validate_configuration_payload,
)

_MANAGEMENT_CONTRACT = "configuration-management-v1"
_GENERIC_HANDLER = "generic-json-v1"
_FORBIDDEN_SECRET_KEYS = {
    "api_key",
    "api_secret",
    "password",
    "secret",
    "secret_value",
    "passphrase",
    "private_key",
}
_FORBIDDEN_EXECUTABLE_KEYS = {
    "python_code",
    "callable_source",
    "executable_code",
}
_SCHEMA_KEYS = {
    "type",
    "title",
    "description",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
    "const",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
    "pattern",
    "minItems",
    "maxItems",
    "minProperties",
    "maxProperties",
    "default",
    "readOnly",
    "unit",
    "display_order",
}


class ConfigurationManagementService:
    """Owner-only, forward-only lifecycle management for V1.3 configuration."""

    def __init__(self, db: Session, *, actor: str = "local-owner") -> None:
        self.db = db
        self.actor = actor
        self.repository = StrategyPlatformConfigurationRepository(db)
        self.resolver = ConfigurationResolverService(db)

    def version_detail(
        self, *, config_type: str, version_id: int
    ) -> ConfigurationVersionDetailRead:
        self.repository.require_owner_connection()
        version = self._require_version(version_id, expected_type=config_type)
        return ConfigurationVersionDetailRead(
            version=ConfigurationVersionRead.model_validate(version),
            dependencies=self._dependency_reads(version.id),
        )

    def audit_history(
        self,
        *,
        config_type: str,
        scope_type: str,
        scope_key: str,
        limit: int,
    ) -> ConfigurationAuditEventListRead:
        self.repository.require_owner_connection()
        self._require_type(config_type, require_manageable=False)
        return ConfigurationAuditEventListRead(
            config_type=config_type,
            scope_type=scope_type,
            scope_key=scope_key,
            items=[
                ConfigurationAuditEventRead.model_validate(row)
                for row in self.repository.list_audit_events(
                    config_type=config_type,
                    scope_type=scope_type,
                    scope_key=scope_key,
                    limit=limit,
                )
            ],
        )

    def bundle_history(
        self, *, scope_type: str, scope_key: str, limit: int
    ) -> ConfigurationBundleSnapshotListRead:
        self.repository.require_owner_connection()
        snapshots = self.repository.list_bundles(
            scope_type=scope_type,
            scope_key=scope_key,
            limit=limit,
        )
        return ConfigurationBundleSnapshotListRead(
            scope_type=scope_type,
            scope_key=scope_key,
            items=[self.resolver.read_bundle(row.id) for row in snapshots],
        )

    def diff_versions(
        self,
        *,
        config_type: str,
        version_id: int,
        against_version_id: int | None,
        scope_type: str,
        scope_key: str,
    ) -> ConfigurationVersionDiffRead:
        self.repository.require_owner_connection()
        target = self._require_version(version_id, expected_type=config_type)
        if against_version_id is None:
            activation = self.repository.get_activation(
                config_type=config_type,
                scope_type=scope_type,
                scope_key=scope_key,
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
            against_version_id = activation.version_id
        source = self._require_version(
            against_version_id,
            expected_type=config_type,
        )
        source_value = self._version_comparison_value(source)
        target_value = self._version_comparison_value(target)
        items: list[ConfigurationDiffEntryRead] = []
        _collect_diff(source_value, target_value, path="$", items=items)
        return ConfigurationVersionDiffRead(
            config_type=config_type,
            scope_type=scope_type,
            scope_key=scope_key,
            from_version_id=source.id,
            to_version_id=target.id,
            items=items,
        )

    def create_draft(
        self,
        *,
        config_type: str,
        request: ConfigurationDraftCreateRequest,
        request_id: str,
    ) -> ConfigurationWriteResult:
        operation = "configuration.create_draft"
        request_payload = {"config_type": config_type, **request.model_dump(mode="json")}
        replay = self._begin_write(
            operation=operation,
            request_id=request_id,
            request_payload=request_payload,
        )
        if replay is not None:
            return replay

        type_row = self._require_type(config_type, require_manageable=True)
        self.repository.acquire_configuration_lock(config_type)
        source = self._draft_source(
            config_type=config_type,
            request=request,
        )
        payload = copy.deepcopy(
            request.payload_json
            if request.payload_json is not None
            else source.payload_json if source is not None else None
        )
        if payload is None:
            raise StrategyPlatformReadError(
                "CONFIGURATION_DRAFT_SOURCE_REQUIRED",
                "A source version or explicit payload is required for the first draft.",
                context={"config_type": config_type},
            )
        self._validate_payload_contract(type_row, payload, version_id=source.id if source else 0)

        dependency_inputs = self._draft_dependencies(request=request, source=source)
        dependency_versions = self._validate_dependency_inputs(dependency_inputs)
        version = self.repository.add_version(
            type_key=config_type,
            version_number=self.repository.next_version_number(config_type),
            payload_json=payload,
            schema_version=type_row.schema_version,
            config_digest=_configuration_digest(
                config_type=config_type,
                schema_version=type_row.schema_version,
                payload=payload,
            ),
            change_summary=request.change_summary.strip(),
            created_by=self.actor,
        )
        for dependency, dependency_version in zip(
            dependency_inputs,
            dependency_versions,
        ):
            if dependency_version.id == version.id:
                raise StrategyPlatformReadError(
                    "CONFIGURATION_DEPENDENCY_SELF_REFERENCE",
                    "A configuration version cannot depend on itself.",
                    context={"version_id": version.id},
                )
            self.repository.add_dependency(
                configuration_version_id=version.id,
                depends_on_version_id=dependency.depends_on_version_id,
                relation_key=dependency.relation_key.strip(),
            )
        self.db.flush()
        result = ConfigurationWriteResult(
            request_id=request_id,
            operation=operation,
            scope_type=request.scope_type,
            scope_key=request.scope_key,
            version=ConfigurationVersionRead.model_validate(version),
            dependencies=self._dependency_reads(version.id),
        )
        self._complete_write(
            event_type="DRAFT_CREATED",
            result=result,
            request_id=request_id,
            request_payload=request_payload,
            reason=request.change_summary,
        )
        return result

    def validate_version(
        self,
        *,
        config_type: str,
        version_id: int,
        request: ConfigurationVersionActionRequest,
        request_id: str,
    ) -> ConfigurationWriteResult:
        operation = "configuration.validate"
        request_payload = {
            "config_type": config_type,
            "version_id": version_id,
            **request.model_dump(mode="json"),
        }
        replay = self._begin_write(
            operation=operation,
            request_id=request_id,
            request_payload=request_payload,
        )
        if replay is not None:
            return replay
        self._require_type(config_type, require_manageable=True)
        version = self._require_version_for_update(
            version_id,
            expected_type=config_type,
        )
        if version.lifecycle_status != "DRAFT":
            self._raise_transition(version, expected="DRAFT", target="VALIDATED")
        self._validate_graph(root=version, allow_draft_root=True)
        version.lifecycle_status = "VALIDATED"
        version.validated_at = datetime.now(timezone.utc)
        self.db.flush()
        resolution = self.resolver.resolve_version(
            workflow_kind="configuration-validation",
            aggregate_config_type=config_type,
            scope_type=request.scope_type,
            scope_key=request.scope_key,
            aggregate_version_id=version.id,
        )
        result = ConfigurationWriteResult(
            request_id=request_id,
            operation=operation,
            scope_type=request.scope_type,
            scope_key=request.scope_key,
            version=ConfigurationVersionRead.model_validate(version),
            dependencies=self._dependency_reads(version.id),
            validation_bundle=resolution,
        )
        self._complete_write(
            event_type="VALIDATED",
            result=result,
            request_id=request_id,
            request_payload=request_payload,
            reason=request.reason,
        )
        return result

    def activate_version(
        self,
        *,
        config_type: str,
        version_id: int,
        request: ConfigurationVersionActionRequest,
        request_id: str,
    ) -> ConfigurationWriteResult:
        operation = "configuration.activate"
        request_payload = {
            "config_type": config_type,
            "version_id": version_id,
            **request.model_dump(mode="json"),
        }
        replay = self._begin_write(
            operation=operation,
            request_id=request_id,
            request_payload=request_payload,
        )
        if replay is not None:
            return replay
        self._require_type(config_type, require_manageable=True)
        version = self._require_version_for_update(
            version_id,
            expected_type=config_type,
        )
        if version.lifecycle_status != "VALIDATED":
            raise StrategyPlatformReadError(
                "CONFIGURATION_VERSION_NOT_VALIDATED",
                "Only a VALIDATED configuration version can be activated.",
                context={
                    "version_id": version.id,
                    "lifecycle_status": version.lifecycle_status,
                },
            )
        self.repository.acquire_activation_lock(
            config_type=config_type,
            scope_type=request.scope_type,
            scope_key=request.scope_key,
        )
        graph = self._validate_graph(root=version, allow_draft_root=False)
        resolution = self.resolver.resolve_version(
            workflow_kind="configuration-activation",
            aggregate_config_type=config_type,
            scope_type=request.scope_type,
            scope_key=request.scope_key,
            aggregate_version_id=version.id,
        )
        self._require_scope_compatibility(
            graph=graph,
            root_version_id=version.id,
            scope_type=request.scope_type,
            scope_key=request.scope_key,
        )
        activation = self.repository.get_activation(
            config_type=config_type,
            scope_type=request.scope_type,
            scope_key=request.scope_key,
            lock=True,
        )
        previous_version_id = activation.version_id if activation is not None else None
        now = datetime.now(timezone.utc)
        if activation is None:
            activation = self.repository.add_activation(
                config_type=config_type,
                scope_type=request.scope_type,
                scope_key=request.scope_key,
                version_id=version.id,
                activated_by=self.actor,
            )
        else:
            activation.version_id = version.id
            activation.activated_at = now
            activation.activated_by = self.actor
        self.db.flush()
        result = ConfigurationWriteResult(
            request_id=request_id,
            operation=operation,
            scope_type=request.scope_type,
            scope_key=request.scope_key,
            version=ConfigurationVersionRead.model_validate(version),
            dependencies=self._dependency_reads(version.id),
            previous_active_version_id=previous_version_id,
            active_version_id=version.id,
            validation_bundle=resolution,
        )
        self._complete_write(
            event_type="ACTIVATED",
            result=result,
            request_id=request_id,
            request_payload=request_payload,
            reason=request.reason,
        )
        return result

    def retire_version(
        self,
        *,
        config_type: str,
        version_id: int,
        request: ConfigurationVersionActionRequest,
        request_id: str,
    ) -> ConfigurationWriteResult:
        operation = "configuration.retire"
        request_payload = {
            "config_type": config_type,
            "version_id": version_id,
            **request.model_dump(mode="json"),
        }
        replay = self._begin_write(
            operation=operation,
            request_id=request_id,
            request_payload=request_payload,
        )
        if replay is not None:
            return replay
        self._require_type(config_type, require_manageable=True)
        version = self._require_version_for_update(
            version_id,
            expected_type=config_type,
        )
        if version.lifecycle_status != "VALIDATED":
            self._raise_transition(version, expected="VALIDATED", target="RETIRED")
        activations = self.repository.list_activations_for_version(version.id)
        if activations:
            raise StrategyPlatformReadError(
                "ACTIVE_CONFIGURATION_CANNOT_BE_RETIRED",
                "An active configuration version must be switched before retirement.",
                context={
                    "version_id": version.id,
                    "active_scopes": [
                        {
                            "scope_type": row.scope_type,
                            "scope_key": row.scope_key,
                        }
                        for row in activations
                    ],
                },
            )
        version.lifecycle_status = "RETIRED"
        self.db.flush()
        result = ConfigurationWriteResult(
            request_id=request_id,
            operation=operation,
            scope_type=request.scope_type,
            scope_key=request.scope_key,
            version=ConfigurationVersionRead.model_validate(version),
            dependencies=self._dependency_reads(version.id),
        )
        self._complete_write(
            event_type="RETIRED",
            result=result,
            request_id=request_id,
            request_payload=request_payload,
            reason=request.reason,
        )
        return result

    def record_failed_write(
        self,
        *,
        operation: str,
        event_type: str,
        config_type: str,
        version_id: int,
        scope_type: str,
        scope_key: str,
        request_id: str,
        request_payload: Mapping[str, Any],
        error: StrategyPlatformReadError,
    ) -> None:
        """Persist supported lifecycle failures after the mutation transaction rolls back."""

        if event_type not in {"VALIDATION_FAILED", "ACTIVATION_FAILED"}:
            raise ValueError("unsupported configuration failure audit type")
        self.repository.require_owner_connection()
        self.repository.acquire_idempotency_lock(request_id)
        if self.repository.get_audit_by_request_id(request_id) is not None:
            return
        version = self.repository.get_version(version_id)
        if version is None or version.type_key != config_type:
            return
        self.repository.add_audit_event(
            configuration_version_id=version.id,
            event_type=event_type,
            actor=self.actor,
            request_id=request_id,
            scope_type=scope_type,
            scope_key=scope_key,
            reason=error.code,
            event_snapshot={
                "contract": _MANAGEMENT_CONTRACT,
                "operation": operation,
                "request_digest": _request_digest(request_payload),
                "error": error.detail(),
                "error_status_code": error.status_code,
                "credential_values_recorded": False,
            },
        )
        self.db.flush()

    def _begin_write(
        self,
        *,
        operation: str,
        request_id: str,
        request_payload: Mapping[str, Any],
    ) -> ConfigurationWriteResult | None:
        self.repository.require_owner_connection()
        self.repository.acquire_idempotency_lock(request_id)
        existing = self.repository.get_audit_by_request_id(request_id)
        if existing is None:
            return None
        snapshot = existing.event_snapshot
        expected_digest = _request_digest(request_payload)
        if (
            not isinstance(snapshot, Mapping)
            or snapshot.get("contract") != _MANAGEMENT_CONTRACT
            or snapshot.get("operation") != operation
            or snapshot.get("request_digest") != expected_digest
        ):
            raise StrategyPlatformReadError(
                "CONFIGURATION_IDEMPOTENCY_CONFLICT",
                "Idempotency-Key was already used for a different configuration write.",
                context={"request_id": request_id},
            )
        result = snapshot.get("result")
        error = snapshot.get("error")
        if isinstance(error, Mapping):
            raise StrategyPlatformReadError(
                str(error.get("code") or "CONFIGURATION_WRITE_BLOCKED"),
                str(error.get("message") or "Configuration write was blocked."),
                status_code=int(snapshot.get("error_status_code") or 409),
                context=dict(error.get("context"))
                if isinstance(error.get("context"), Mapping)
                else {},
            )
        if not isinstance(result, Mapping):
            raise StrategyPlatformReadError(
                "CONFIGURATION_IDEMPOTENCY_EVIDENCE_INVALID",
                "Stored configuration idempotency evidence is incomplete.",
                context={"request_id": request_id},
            )
        return ConfigurationWriteResult.model_validate(result).model_copy(
            update={"idempotent_replay": True}
        )

    def _complete_write(
        self,
        *,
        event_type: str,
        result: ConfigurationWriteResult,
        request_id: str,
        request_payload: Mapping[str, Any],
        reason: str | None,
    ) -> None:
        self.repository.add_audit_event(
            configuration_version_id=result.version.id,
            event_type=event_type,
            actor=self.actor,
            request_id=request_id,
            scope_type=result.scope_type,
            scope_key=result.scope_key,
            reason=reason,
            event_snapshot={
                "contract": _MANAGEMENT_CONTRACT,
                "operation": result.operation,
                "request_digest": _request_digest(request_payload),
                "result": result.model_dump(mode="json"),
                "credential_values_recorded": False,
            },
        )
        self.db.flush()

    def _require_type(self, type_key: str, *, require_manageable: bool):
        type_row = self.repository.get_type(type_key)
        if type_row is None:
            raise StrategyPlatformReadError(
                "CONFIGURATION_TYPE_NOT_FOUND",
                "Configuration type is not registered.",
                status_code=404,
                context={"config_type": type_key},
            )
        if type_row.enabled is not True:
            raise StrategyPlatformReadError(
                "CONFIGURATION_TYPE_DISABLED",
                "Configuration type is disabled.",
                context={"config_type": type_key},
            )
        if require_manageable:
            capability = type_row.editor_capability
            if (
                type_row.handler_key != _GENERIC_HANDLER
                or not isinstance(capability, Mapping)
                or capability.get("write_enabled") is not True
                or capability.get("read_only") is True
            ):
                raise StrategyPlatformReadError(
                    "CONFIGURATION_HANDLER_UNAVAILABLE",
                    "Configuration type has no installed owner-write handler.",
                    context={
                        "config_type": type_key,
                        "handler_key": type_row.handler_key,
                    },
                )
            schema = capability.get("json_schema")
            if not isinstance(schema, Mapping):
                raise StrategyPlatformReadError(
                    "CONFIGURATION_SCHEMA_UNAVAILABLE",
                    "Writable configuration type has no strict JSON schema.",
                    context={"config_type": type_key},
                )
            _validate_schema_definition(schema, path="$schema")
        return type_row

    def _require_version(
        self, version_id: int, *, expected_type: str | None = None
    ) -> ConfigurationVersion:
        version = self.repository.get_version(version_id)
        if version is None:
            raise StrategyPlatformReadError(
                "CONFIGURATION_VERSION_NOT_FOUND",
                "Configuration version does not exist.",
                status_code=404,
                context={"version_id": version_id},
            )
        if expected_type is not None and version.type_key != expected_type:
            raise StrategyPlatformReadError(
                "CONFIGURATION_VERSION_TYPE_MISMATCH",
                "Configuration version does not belong to the requested type.",
                context={
                    "version_id": version.id,
                    "expected_type": expected_type,
                    "actual_type": version.type_key,
                },
            )
        return version

    def _require_version_for_update(
        self, version_id: int, *, expected_type: str
    ) -> ConfigurationVersion:
        version = self.repository.get_version_for_update(version_id)
        if version is None:
            raise StrategyPlatformReadError(
                "CONFIGURATION_VERSION_NOT_FOUND",
                "Configuration version does not exist.",
                status_code=404,
                context={"version_id": version_id},
            )
        if version.type_key != expected_type:
            raise StrategyPlatformReadError(
                "CONFIGURATION_VERSION_TYPE_MISMATCH",
                "Configuration version does not belong to the requested type.",
                context={
                    "version_id": version.id,
                    "expected_type": expected_type,
                    "actual_type": version.type_key,
                },
            )
        return version

    def _draft_source(
        self,
        *,
        config_type: str,
        request: ConfigurationDraftCreateRequest,
    ) -> ConfigurationVersion | None:
        if request.source_version_id is not None:
            source = self._require_version(
                request.source_version_id,
                expected_type=config_type,
            )
        else:
            activation = self.repository.get_activation(
                config_type=config_type,
                scope_type=request.scope_type,
                scope_key=request.scope_key,
            )
            source = (
                self._require_version(activation.version_id, expected_type=config_type)
                if activation is not None
                else None
            )
        if source is not None and source.lifecycle_status not in {"VALIDATED", "RETIRED"}:
            raise StrategyPlatformReadError(
                "CONFIGURATION_DRAFT_SOURCE_INVALID",
                "A draft can only copy immutable VALIDATED or RETIRED history.",
                context={
                    "source_version_id": source.id,
                    "lifecycle_status": source.lifecycle_status,
                },
            )
        return source

    def _draft_dependencies(
        self,
        *,
        request: ConfigurationDraftCreateRequest,
        source: ConfigurationVersion | None,
    ) -> list[ConfigurationDependencyWrite]:
        if request.dependencies is not None:
            return [
                ConfigurationDependencyWrite(
                    depends_on_version_id=row.depends_on_version_id,
                    relation_key=row.relation_key.strip(),
                )
                for row in request.dependencies
            ]
        if source is None:
            return []
        return [
            ConfigurationDependencyWrite(
                depends_on_version_id=row.depends_on_version_id,
                relation_key=row.relation_key,
            )
            for row in self.repository.list_dependencies((source.id,))
        ]

    def _validate_dependency_inputs(
        self, dependencies: list[ConfigurationDependencyWrite]
    ) -> list[ConfigurationVersion]:
        identities: set[tuple[int, str]] = set()
        version_ids: list[int] = []
        for dependency in dependencies:
            relation_key = dependency.relation_key.strip()
            identity = (dependency.depends_on_version_id, relation_key)
            if identity in identities:
                raise StrategyPlatformReadError(
                    "CONFIGURATION_DEPENDENCY_DUPLICATE",
                    "Draft dependency list contains a duplicate exact edge.",
                    context={
                        "depends_on_version_id": dependency.depends_on_version_id,
                        "relation_key": relation_key,
                    },
                )
            identities.add(identity)
            version_ids.append(dependency.depends_on_version_id)
        versions = self.repository.get_versions(version_ids)
        by_id = {row.id: row for row in versions}
        missing = sorted(set(version_ids) - set(by_id))
        if missing:
            raise StrategyPlatformReadError(
                "CONFIGURATION_DEPENDENCY_VERSION_NOT_FOUND",
                "Draft dependency references a missing configuration version.",
                status_code=404,
                context={"version_ids": missing},
            )
        return [by_id[version_id] for version_id in version_ids]

    def _validate_graph(
        self,
        *,
        root: ConfigurationVersion,
        allow_draft_root: bool,
    ) -> dict[int, ConfigurationVersion]:
        resolved_by_id: dict[int, ConfigurationVersion] = {}
        resolved_by_type: dict[str, ConfigurationVersion] = {}
        visiting: set[int] = set()

        def visit(version: ConfigurationVersion, *, is_root: bool = False) -> None:
            if version.id in visiting:
                raise StrategyPlatformReadError(
                    "CONFIGURATION_DEPENDENCY_CYCLE",
                    "Configuration dependency graph contains a cycle.",
                    context={"version_id": version.id},
                )
            if version.id in resolved_by_id:
                return
            allowed = {"VALIDATED"}
            if is_root and allow_draft_root:
                allowed.add("DRAFT")
            if version.lifecycle_status not in allowed:
                raise StrategyPlatformReadError(
                    "CONFIGURATION_DEPENDENCY_NOT_VALIDATED",
                    "Configuration validation requires a VALIDATED dependency closure.",
                    context={
                        "version_id": version.id,
                        "lifecycle_status": version.lifecycle_status,
                    },
                )
            type_row = self._require_type(version.type_key, require_manageable=True)
            if version.schema_version != type_row.schema_version:
                raise StrategyPlatformReadError(
                    "CONFIGURATION_SCHEMA_INCOMPATIBLE",
                    "Configuration version schema is incompatible with its registered type.",
                    context={
                        "version_id": version.id,
                        "version_schema": version.schema_version,
                        "registered_schema": type_row.schema_version,
                    },
                )
            self._validate_payload_contract(type_row, version.payload_json, version_id=version.id)
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
            for edge in self.repository.list_dependencies((version.id,)):
                child = self._require_version(edge.depends_on_version_id)
                visit(child)
            visiting.remove(version.id)

        visit(root, is_root=True)
        return resolved_by_id

    def _validate_payload_contract(
        self, type_row, payload: Any, *, version_id: int
    ) -> None:
        validate_configuration_payload(payload, version_id=version_id)
        schema = type_row.editor_capability.get("json_schema")
        if not isinstance(schema, Mapping):
            raise StrategyPlatformReadError(
                "CONFIGURATION_SCHEMA_UNAVAILABLE",
                "Writable configuration type has no strict JSON schema.",
                context={"config_type": type_row.type_key},
            )
        _validate_json_value(payload, schema, path="$", version_id=version_id)

    def _require_scope_compatibility(
        self,
        *,
        graph: Mapping[int, ConfigurationVersion],
        root_version_id: int,
        scope_type: str,
        scope_key: str,
    ) -> None:
        conflicts = []
        for version in sorted(graph.values(), key=lambda item: (item.type_key, item.id)):
            if version.id == root_version_id:
                continue
            activation = self.repository.get_activation(
                config_type=version.type_key,
                scope_type=scope_type,
                scope_key=scope_key,
                lock=True,
            )
            if activation is not None and activation.version_id != version.id:
                conflicts.append(
                    {
                        "config_type": version.type_key,
                        "required_version_id": version.id,
                        "active_version_id": activation.version_id,
                    }
                )
        if conflicts:
            raise StrategyPlatformReadError(
                "CONFIGURATION_SCOPE_CONFLICT",
                "Configuration dependency closure conflicts with active scope bindings.",
                context={
                    "scope_type": scope_type,
                    "scope_key": scope_key,
                    "conflicts": conflicts,
                },
            )

    def _dependency_reads(self, version_id: int) -> list[ConfigurationDependencyRead]:
        edges = self.repository.list_dependencies((version_id,))
        versions = self.repository.get_versions(
            row.depends_on_version_id for row in edges
        )
        by_id = {row.id: row for row in versions}
        parent = self._require_version(version_id)
        return [
            ConfigurationDependencyRead(
                configuration_version_id=parent.id,
                configuration_type=parent.type_key,
                depends_on_version_id=edge.depends_on_version_id,
                depends_on_type=by_id[edge.depends_on_version_id].type_key,
                relation_key=edge.relation_key,
            )
            for edge in edges
            if edge.depends_on_version_id in by_id
        ]

    def _version_comparison_value(self, version: ConfigurationVersion) -> dict[str, Any]:
        return {
            "payload_json": version.payload_json,
            "dependencies": [
                {
                    "relation_key": row.relation_key,
                    "depends_on_type": row.depends_on_type,
                    "depends_on_version_id": row.depends_on_version_id,
                }
                for row in self._dependency_reads(version.id)
            ],
        }

    @staticmethod
    def _raise_transition(
        version: ConfigurationVersion, *, expected: str, target: str
    ) -> None:
        raise StrategyPlatformReadError(
            "CONFIGURATION_LIFECYCLE_TRANSITION_INVALID",
            "Configuration lifecycle transition is not legal.",
            context={
                "version_id": version.id,
                "current_status": version.lifecycle_status,
                "expected_status": expected,
                "target_status": target,
            },
        )


def _configuration_digest(
    *, config_type: str, schema_version: str, payload: Mapping[str, Any]
) -> str:
    return _sha256(
        {
            "contract": "configuration-version-digest-v1",
            "config_type": config_type,
            "schema_version": schema_version,
            "payload_json": payload,
        }
    )


def _request_digest(payload: Mapping[str, Any]) -> str:
    return _sha256({"contract": _MANAGEMENT_CONTRACT, "request": payload})


def _sha256(value: Any) -> str:
    serialized = json.dumps(
        value,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _validate_schema_definition(schema: Mapping[str, Any], *, path: str) -> None:
    unknown = sorted(set(schema) - _SCHEMA_KEYS)
    if unknown:
        raise StrategyPlatformReadError(
            "CONFIGURATION_SCHEMA_KEYWORD_UNSUPPORTED",
            "Configuration schema contains unsupported keywords.",
            context={"path": path, "keywords": unknown},
        )
    schema_type = schema.get("type")
    if schema_type not in {"object", "array", "string", "integer", "number", "boolean", "null"}:
        raise StrategyPlatformReadError(
            "CONFIGURATION_SCHEMA_INVALID",
            "Configuration schema must declare one supported JSON type.",
            context={"path": path, "type": schema_type},
        )
    if "enum" in schema and (
        not isinstance(schema["enum"], list) or not schema["enum"]
    ):
        raise StrategyPlatformReadError(
            "CONFIGURATION_SCHEMA_INVALID",
            "Configuration schema enum must be a non-empty list.",
            context={"path": path},
        )
    for keyword in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
        if keyword in schema and (
            isinstance(schema[keyword], bool)
            or not isinstance(schema[keyword], (int, float))
        ):
            raise StrategyPlatformReadError(
                "CONFIGURATION_SCHEMA_INVALID",
                "Configuration schema numeric limit is invalid.",
                context={"path": path, "keyword": keyword},
            )
    for keyword in (
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "minProperties",
        "maxProperties",
        "display_order",
    ):
        if keyword in schema and (
            isinstance(schema[keyword], bool)
            or not isinstance(schema[keyword], int)
            or schema[keyword] < 0
        ):
            raise StrategyPlatformReadError(
                "CONFIGURATION_SCHEMA_INVALID",
                "Configuration schema size or display limit is invalid.",
                context={"path": path, "keyword": keyword},
            )
    for minimum_key, maximum_key in (
        ("minimum", "maximum"),
        ("minLength", "maxLength"),
        ("minItems", "maxItems"),
        ("minProperties", "maxProperties"),
    ):
        if (
            minimum_key in schema
            and maximum_key in schema
            and schema[minimum_key] > schema[maximum_key]
        ):
            raise StrategyPlatformReadError(
                "CONFIGURATION_SCHEMA_INVALID",
                "Configuration schema minimum exceeds its maximum.",
                context={"path": path, "minimum": minimum_key, "maximum": maximum_key},
            )
    if "pattern" in schema:
        if not isinstance(schema["pattern"], str):
            raise StrategyPlatformReadError(
                "CONFIGURATION_SCHEMA_INVALID",
                "Configuration schema pattern must be a string.",
                context={"path": path},
            )
        try:
            re.compile(schema["pattern"])
        except re.error as exc:
            raise StrategyPlatformReadError(
                "CONFIGURATION_SCHEMA_INVALID",
                "Configuration schema contains an invalid pattern.",
                context={"path": path},
            ) from exc
    if schema_type == "object":
        if schema.get("additionalProperties") is not False:
            raise StrategyPlatformReadError(
                "CONFIGURATION_SCHEMA_NOT_STRICT",
                "Every object schema must reject unknown fields.",
                context={"path": path},
            )
        properties = schema.get("properties")
        required = schema.get("required", [])
        if not isinstance(properties, Mapping) or not isinstance(required, list):
            raise StrategyPlatformReadError(
                "CONFIGURATION_SCHEMA_INVALID",
                "Object schema properties and required fields are invalid.",
                context={"path": path},
            )
        if (
            any(not isinstance(item, str) for item in required)
            or len(set(required)) != len(required)
            or not set(required).issubset(properties)
        ):
            raise StrategyPlatformReadError(
                "CONFIGURATION_SCHEMA_INVALID",
                "Object schema required fields must exist in properties.",
                context={"path": path},
            )
        for key, child_schema in properties.items():
            normalized = str(key).lower()
            if normalized in _FORBIDDEN_SECRET_KEYS or normalized in _FORBIDDEN_EXECUTABLE_KEYS:
                raise StrategyPlatformReadError(
                    "CONFIGURATION_SCHEMA_SAFETY_VIOLATION",
                    "Configuration schema exposes a forbidden secret or executable field.",
                    context={"path": f"{path}.{key}"},
                )
            if not isinstance(key, str) or not isinstance(child_schema, Mapping):
                raise StrategyPlatformReadError(
                    "CONFIGURATION_SCHEMA_INVALID",
                    "Object schema properties are invalid.",
                    context={"path": path},
                )
            _validate_schema_definition(child_schema, path=f"{path}.{key}")
    if schema_type == "array":
        items = schema.get("items")
        if not isinstance(items, Mapping):
            raise StrategyPlatformReadError(
                "CONFIGURATION_SCHEMA_INVALID",
                "Array schema must declare one item schema.",
                context={"path": path},
            )
        _validate_schema_definition(items, path=f"{path}[]")


def _validate_json_value(
    value: Any,
    schema: Mapping[str, Any],
    *,
    path: str,
    version_id: int,
) -> None:
    schema_type = schema["type"]
    valid_type = {
        "object": isinstance(value, Mapping),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }[schema_type]
    if not valid_type:
        _schema_value_error(version_id, path, f"expected {schema_type}")
    if "enum" in schema and value not in schema["enum"]:
        _schema_value_error(version_id, path, "value is not in the allowed enum")
    if "const" in schema and value != schema["const"]:
        _schema_value_error(version_id, path, "value does not match the required constant")
    if schema_type == "object":
        properties = schema["properties"]
        missing = sorted(set(schema.get("required", [])) - set(value))
        unknown = sorted(set(value) - set(properties))
        if missing:
            _schema_value_error(version_id, path, f"missing required fields: {missing}")
        if unknown:
            _schema_value_error(version_id, path, f"unknown fields: {unknown}")
        _validate_size(value, schema, path=path, version_id=version_id, prefix="Properties")
        for key, child in value.items():
            _validate_json_value(
                child,
                properties[key],
                path=f"{path}.{key}",
                version_id=version_id,
            )
    elif schema_type == "array":
        _validate_size(value, schema, path=path, version_id=version_id, prefix="Items")
        for index, child in enumerate(value):
            _validate_json_value(
                child,
                schema["items"],
                path=f"{path}[{index}]",
                version_id=version_id,
            )
    elif schema_type == "string":
        _validate_size(value, schema, path=path, version_id=version_id, prefix="Length")
        pattern = schema.get("pattern")
        if pattern is not None:
            try:
                matches = re.search(str(pattern), value) is not None
            except re.error as exc:
                raise StrategyPlatformReadError(
                    "CONFIGURATION_SCHEMA_INVALID",
                    "Configuration schema contains an invalid pattern.",
                    context={"path": path},
                ) from exc
            if not matches:
                _schema_value_error(version_id, path, "string does not match pattern")
    elif schema_type in {"integer", "number"}:
        if not math.isfinite(value):
            _schema_value_error(version_id, path, "number must be finite")
        for keyword, operator in (
            ("minimum", lambda left, right: left >= right),
            ("maximum", lambda left, right: left <= right),
            ("exclusiveMinimum", lambda left, right: left > right),
            ("exclusiveMaximum", lambda left, right: left < right),
        ):
            if keyword in schema and not operator(value, schema[keyword]):
                _schema_value_error(version_id, path, f"number violates {keyword}")


def _validate_size(
    value: Any,
    schema: Mapping[str, Any],
    *,
    path: str,
    version_id: int,
    prefix: str,
) -> None:
    suffix = {"Length": "Length", "Items": "Items", "Properties": "Properties"}[prefix]
    minimum = schema.get(f"min{suffix}")
    maximum = schema.get(f"max{suffix}")
    if minimum is not None and len(value) < minimum:
        _schema_value_error(version_id, path, f"size is below min{suffix}")
    if maximum is not None and len(value) > maximum:
        _schema_value_error(version_id, path, f"size exceeds max{suffix}")


def _schema_value_error(version_id: int, path: str, reason: str) -> None:
    raise StrategyPlatformReadError(
        "CONFIGURATION_PAYLOAD_SCHEMA_INVALID",
        "Configuration payload does not match its strict JSON schema.",
        context={"version_id": version_id, "path": path, "reason": reason},
    )


def _collect_diff(
    before: Any,
    after: Any,
    *,
    path: str,
    items: list[ConfigurationDiffEntryRead],
) -> None:
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        for key in sorted(set(before) | set(after)):
            child_path = f"{path}.{key}"
            if key not in before:
                items.append(ConfigurationDiffEntryRead(path=child_path, after=after[key]))
            elif key not in after:
                items.append(ConfigurationDiffEntryRead(path=child_path, before=before[key]))
            else:
                _collect_diff(before[key], after[key], path=child_path, items=items)
        return
    if isinstance(before, list) and isinstance(after, list):
        limit = max(len(before), len(after))
        for index in range(limit):
            child_path = f"{path}[{index}]"
            if index >= len(before):
                items.append(ConfigurationDiffEntryRead(path=child_path, after=after[index]))
            elif index >= len(after):
                items.append(ConfigurationDiffEntryRead(path=child_path, before=before[index]))
            else:
                _collect_diff(before[index], after[index], path=child_path, items=items)
        return
    if before != after:
        items.append(ConfigurationDiffEntryRead(path=path, before=before, after=after))
