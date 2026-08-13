from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.strategy_platform_errors import StrategyPlatformReadError
from app.models.strategy_platform import (
    ConfigurationActivation,
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
    "configuration_bundle_snapshots",
)


class StrategyPlatformConfigurationRepository:
    """Owner-bound persistence access for Strategy Platform configuration reads."""

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
                "Strategy Platform configuration reads require the database "
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
