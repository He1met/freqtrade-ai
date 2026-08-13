from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.strategy_platform_errors import StrategyPlatformReadError
from app.models.strategy_platform import (
    ConfigurationActivation,
    ConfigurationAuditEvent,
    ConfigurationBundleSnapshot,
    ConfigurationDependency,
    ConfigurationType,
    ConfigurationVersion,
)

_OWNER_TABLES = (
    "configuration_types",
    "configuration_versions",
    "configuration_dependencies",
    "configuration_activations",
    "configuration_audit_events",
    "configuration_bundle_snapshots",
)


class StrategyPlatformConfigurationRepository:
    """Owner-bound persistence access for Strategy Platform configuration state."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def require_owner_connection(self) -> None:
        bind = self.db.get_bind()
        if bind.dialect.name != "postgresql":
            # SQLite is used only by isolated contract tests and has no role ACL model.
            return
        ownership = (
            self.db.execute(
                text(
                    "SELECT current_user AS current_user, COUNT(*) AS table_count, "
                    "COALESCE(bool_and(pg_get_userbyid(table_class.relowner) = "
                    "current_user), false) AS owns_all "
                    "FROM pg_class table_class "
                    "JOIN pg_namespace namespace "
                    "ON namespace.oid = table_class.relnamespace "
                    "WHERE namespace.nspname = current_schema() "
                    "AND table_class.relname = ANY(:table_names)"
                ),
                {"table_names": list(_OWNER_TABLES)},
            )
            .mappings()
            .one()
        )
        if (
            ownership["table_count"] != len(_OWNER_TABLES)
            or ownership["owns_all"] is not True
        ):
            raise StrategyPlatformReadError(
                "OWNER_DATABASE_REQUIRED",
                "Strategy Platform configuration access requires the database "
                "table owner.",
                status_code=403,
                context={
                    "required_table_count": len(_OWNER_TABLES),
                    "observed_table_count": ownership["table_count"],
                    "database_role": ownership["current_user"],
                },
            )

    def list_types(self) -> list[ConfigurationType]:
        return list(
            self.db.scalars(
                select(ConfigurationType).order_by(ConfigurationType.type_key)
            ).all()
        )

    def get_type(self, type_key: str) -> ConfigurationType | None:
        return self.db.get(ConfigurationType, type_key)

    def list_versions(self, type_key: str, *, limit: int) -> list[ConfigurationVersion]:
        return list(
            self.db.scalars(
                select(ConfigurationVersion)
                .where(ConfigurationVersion.type_key == type_key)
                .order_by(
                    ConfigurationVersion.version_number.desc(),
                    ConfigurationVersion.id.desc(),
                )
                .limit(limit)
            ).all()
        )

    def get_version(self, version_id: int) -> ConfigurationVersion | None:
        return self.db.get(ConfigurationVersion, version_id)

    def get_version_for_update(self, version_id: int) -> ConfigurationVersion | None:
        statement = select(ConfigurationVersion).where(
            ConfigurationVersion.id == version_id
        )
        if self.db.get_bind().dialect.name == "postgresql":
            statement = statement.with_for_update()
        return self.db.scalars(statement).first()

    def next_version_number(self, type_key: str) -> int:
        value = self.db.scalar(
            select(func.coalesce(func.max(ConfigurationVersion.version_number), 0)).where(
                ConfigurationVersion.type_key == type_key
            )
        )
        return int(value or 0) + 1

    def add_version(
        self,
        *,
        type_key: str,
        version_number: int,
        payload_json: dict,
        schema_version: str,
        config_digest: str,
        change_summary: str,
        created_by: str,
    ) -> ConfigurationVersion:
        version = ConfigurationVersion(
            type_key=type_key,
            version_number=version_number,
            lifecycle_status="DRAFT",
            payload_json=payload_json,
            schema_version=schema_version,
            config_digest=config_digest,
            change_summary=change_summary,
            created_by=created_by,
        )
        self.db.add(version)
        self.db.flush()
        return version

    def get_versions(self, version_ids: Iterable[int]) -> list[ConfigurationVersion]:
        ids = tuple(set(version_ids))
        if not ids:
            return []
        return list(
            self.db.scalars(
                select(ConfigurationVersion).where(ConfigurationVersion.id.in_(ids))
            ).all()
        )

    def get_activation(
        self,
        *,
        config_type: str,
        scope_type: str,
        scope_key: str,
        lock: bool = False,
    ) -> ConfigurationActivation | None:
        statement = select(ConfigurationActivation).where(
            ConfigurationActivation.config_type == config_type,
            ConfigurationActivation.scope_type == scope_type,
            ConfigurationActivation.scope_key == scope_key,
        )
        if lock and self.db.get_bind().dialect.name == "postgresql":
            statement = statement.with_for_update()
        return self.db.scalars(statement).first()

    def list_dependencies(
        self, version_ids: Iterable[int]
    ) -> list[ConfigurationDependency]:
        ids = tuple(set(version_ids))
        if not ids:
            return []
        return list(
            self.db.scalars(
                select(ConfigurationDependency)
                .where(ConfigurationDependency.configuration_version_id.in_(ids))
                .order_by(
                    ConfigurationDependency.configuration_version_id,
                    ConfigurationDependency.relation_key,
                    ConfigurationDependency.depends_on_version_id,
                )
            ).all()
        )

    def add_dependency(
        self,
        *,
        configuration_version_id: int,
        depends_on_version_id: int,
        relation_key: str,
    ) -> ConfigurationDependency:
        dependency = ConfigurationDependency(
            configuration_version_id=configuration_version_id,
            depends_on_version_id=depends_on_version_id,
            relation_key=relation_key,
        )
        self.db.add(dependency)
        self.db.flush()
        return dependency

    def list_activations_for_version(
        self, version_id: int
    ) -> list[ConfigurationActivation]:
        return list(
            self.db.scalars(
                select(ConfigurationActivation)
                .where(ConfigurationActivation.version_id == version_id)
                .order_by(
                    ConfigurationActivation.config_type,
                    ConfigurationActivation.scope_type,
                    ConfigurationActivation.scope_key,
                )
            ).all()
        )

    def add_activation(
        self,
        *,
        config_type: str,
        scope_type: str,
        scope_key: str,
        version_id: int,
        activated_by: str,
    ) -> ConfigurationActivation:
        activation = ConfigurationActivation(
            config_type=config_type,
            scope_type=scope_type,
            scope_key=scope_key,
            version_id=version_id,
            activated_by=activated_by,
        )
        self.db.add(activation)
        self.db.flush()
        return activation

    def get_audit_by_request_id(
        self, request_id: str
    ) -> ConfigurationAuditEvent | None:
        return self.db.scalars(
            select(ConfigurationAuditEvent)
            .where(ConfigurationAuditEvent.request_id == request_id)
            .order_by(ConfigurationAuditEvent.id)
            .limit(1)
        ).first()

    def add_audit_event(
        self,
        *,
        configuration_version_id: int,
        event_type: str,
        actor: str,
        request_id: str,
        scope_type: str,
        scope_key: str,
        reason: str | None,
        event_snapshot: dict,
    ) -> ConfigurationAuditEvent:
        event = ConfigurationAuditEvent(
            configuration_version_id=configuration_version_id,
            event_type=event_type,
            actor=actor,
            request_id=request_id,
            scope_type=scope_type,
            scope_key=scope_key,
            reason=reason,
            event_snapshot=event_snapshot,
        )
        self.db.add(event)
        self.db.flush()
        return event

    def list_audit_events(
        self,
        *,
        config_type: str,
        scope_type: str,
        scope_key: str,
        limit: int,
    ) -> list[ConfigurationAuditEvent]:
        return list(
            self.db.scalars(
                select(ConfigurationAuditEvent)
                .join(
                    ConfigurationVersion,
                    ConfigurationVersion.id
                    == ConfigurationAuditEvent.configuration_version_id,
                )
                .where(
                    ConfigurationVersion.type_key == config_type,
                    ConfigurationAuditEvent.scope_type == scope_type,
                    ConfigurationAuditEvent.scope_key == scope_key,
                )
                .order_by(
                    ConfigurationAuditEvent.created_at.desc(),
                    ConfigurationAuditEvent.id.desc(),
                )
                .limit(limit)
            ).all()
        )

    def list_bundles(
        self, *, scope_type: str, scope_key: str, limit: int
    ) -> list[ConfigurationBundleSnapshot]:
        return list(
            self.db.scalars(
                select(ConfigurationBundleSnapshot)
                .where(
                    ConfigurationBundleSnapshot.scope_type == scope_type,
                    ConfigurationBundleSnapshot.scope_key == scope_key,
                )
                .order_by(
                    ConfigurationBundleSnapshot.created_at.desc(),
                    ConfigurationBundleSnapshot.id.desc(),
                )
                .limit(limit)
            ).all()
        )

    def acquire_idempotency_lock(self, request_id: str) -> None:
        if self.db.get_bind().dialect.name != "postgresql":
            return
        self.db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:request_id, 0))"),
            {"request_id": f"strategy-platform-configuration:{request_id}"},
        )

    def acquire_configuration_lock(self, config_type: str) -> None:
        if self.db.get_bind().dialect.name != "postgresql":
            return
        self.db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:config_type, 0))"),
            {"config_type": f"strategy-platform-type:{config_type}"},
        )

    def acquire_activation_lock(
        self, *, config_type: str, scope_type: str, scope_key: str
    ) -> None:
        if self.db.get_bind().dialect.name != "postgresql":
            return
        self.db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:activation_key, 0))"),
            {
                "activation_key": (
                    "strategy-platform-activation:"
                    f"{config_type}:{scope_type}:{scope_key}"
                )
            },
        )

    def get_bundle(self, bundle_id: int) -> ConfigurationBundleSnapshot | None:
        return self.db.get(ConfigurationBundleSnapshot, bundle_id)

    def find_bundle(
        self,
        *,
        workflow_kind: str,
        scope_type: str,
        scope_key: str,
        bundle_digest: str,
    ) -> ConfigurationBundleSnapshot | None:
        return self.db.scalars(
            select(ConfigurationBundleSnapshot).where(
                ConfigurationBundleSnapshot.workflow_kind == workflow_kind,
                ConfigurationBundleSnapshot.scope_type == scope_type,
                ConfigurationBundleSnapshot.scope_key == scope_key,
                ConfigurationBundleSnapshot.bundle_digest == bundle_digest,
            )
        ).first()

    def add_bundle(
        self,
        *,
        workflow_kind: str,
        scope_type: str,
        scope_key: str,
        aggregate_profile_version_id: int,
        resolved_versions_json: dict[str, int],
        resolved_digests_json: dict[str, str],
        bundle_digest: str,
        capability_snapshot: dict,
    ) -> ConfigurationBundleSnapshot:
        snapshot = ConfigurationBundleSnapshot(
            workflow_kind=workflow_kind,
            scope_type=scope_type,
            scope_key=scope_key,
            aggregate_profile_version_id=aggregate_profile_version_id,
            resolved_versions_json=resolved_versions_json,
            resolved_digests_json=resolved_digests_json,
            bundle_digest=bundle_digest,
            capability_snapshot=capability_snapshot,
        )
        self.db.add(snapshot)
        self.db.flush()
        return snapshot
