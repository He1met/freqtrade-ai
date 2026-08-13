"""PostgreSQL owner persistence for the reviewed V1.3 research activation.

The caller owns the transaction.  This module deliberately never commits and
is restricted to the accepted ``freqtrade_ai_design_lab`` v47 database.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.strategy_platform_errors import StrategyPlatformReadError
from app.models.strategy_platform import (
    ConfigurationVersion,
    MetricDefinition,
    MetricDefinitionVersion,
    QualityGateProfile,
    QualityGateProfileVersion,
    QualityGateRule,
)
from app.models.strategy_platform_extensions import (
    AdapterDefinition,
    DiversityProfileVersion,
    DiversityRule,
    GenerationProfileFamily,
    GenerationProfileVersion,
    ResearchProfileVersion,
    ScoringProfileVersion,
    ScoringRule,
    StrategyFamilyDefinitionVersion,
)
from app.schemas.strategy_platform import (
    ConfigurationDependencyWrite,
    ConfigurationDraftCreateRequest,
    ConfigurationVersionActionRequest,
)
from app.services.configuration_management import ConfigurationManagementService
from app.services.configuration_resolver import ConfigurationResolverService
from app.services.frozen_configuration_bundle import (
    validate_frozen_configuration_bundle,
)
from app.services.owner_research_activation import ConfigurationPlan
from app.services.profile_bound_adapters import (
    diversity_profile,
    generation_profile,
    scoring_profile,
)
from app.services.strategy_platform_adapter_registry import (
    InstalledAdapter,
    installed_adapter_manifest_digest as current_adapter_manifest_digest,
)


_DATABASE_NAME = "freqtrade_ai_design_lab"
_SCHEMA_VERSION = "20260813_47"
_MIGRATION_KEY = "strategy-platform-v13-task1-real-data-v1"
_SCOPE_TYPE = "WORKFLOW"
_SCOPE_KEY = "production-research-v13"
_PROFILE_TYPES = {
    "generation-profile",
    "diversity-profile",
    "quality-gate-profile",
    "scoring-profile",
    "research-profile",
}
_QUALITY_PROFILE_KEY = "profile-bound-score-v2-quality"
_QUALITY_PROFILE_NAME = "Profile-bound score v2 quality"
_RULE_OPERATOR = {"eq": "==", "gte": ">=", "gt": ">", "lte": "<=", "lt": "<"}
_OWNER_WRITE_TABLES = (
    "adapter_definitions",
    "configuration_types",
    "configuration_versions",
    "configuration_dependencies",
    "configuration_activations",
    "configuration_audit_events",
    "configuration_bundle_snapshots",
    "generation_profile_versions",
    "generation_profile_families",
    "diversity_profile_versions",
    "diversity_rules",
    "quality_gate_profiles",
    "quality_gate_profile_versions",
    "quality_gate_rules",
    "scoring_profile_versions",
    "scoring_rules",
    "research_profile_versions",
)


class PostgreSQLOwnerResearchActivationPort:
    """Persist one activation plan on an owner-only PostgreSQL session."""

    def __init__(self, db: Session, *, actor: str = "owner:v13-activation") -> None:
        self.db = db
        self.management = ConfigurationManagementService(db, actor=actor)
        self.resolver = ConfigurationResolverService(db)

    def register_adapter(self, adapter: InstalledAdapter) -> None:
        self._preflight()
        expected = self._adapter_values(adapter)
        row = self.db.get(AdapterDefinition, adapter.adapter_key)
        if row is None:
            self.db.add(AdapterDefinition(**expected))
            self.db.flush()
            return
        self._require_exact_model(
            row,
            expected,
            code="ACTIVATION_ADAPTER_REGISTRY_CONFLICT",
            identity={"adapter_key": adapter.adapter_key},
        )

    def create_draft(
        self,
        configuration: ConfigurationPlan,
        *,
        resolved_payload: Mapping[str, Any],
        resolved_dependencies: Sequence[tuple[str, int]],
        resolved_specialized_rows: Sequence[Mapping[str, Any]],
        request_id: str,
    ) -> int:
        self._preflight()
        if len(resolved_specialized_rows) != 1:
            _blocked(
                "ACTIVATION_SPECIALIZED_CARDINALITY_INVALID",
                "Each V1.3 activation profile requires one specialized root row.",
                plan_key=configuration.plan_key,
            )
        self._require_dependencies(configuration, resolved_dependencies)
        result = self.management.create_draft(
            config_type=configuration.type_key,
            request=ConfigurationDraftCreateRequest(
                scope_type=_SCOPE_TYPE,
                scope_key=_SCOPE_KEY,
                change_summary=(
                    f"Issue 707 deterministic {configuration.schema_version} activation"
                ),
                payload_json=dict(resolved_payload),
                dependencies=[
                    ConfigurationDependencyWrite(
                        relation_key=relation,
                        depends_on_version_id=version_id,
                    )
                    for relation, version_id in resolved_dependencies
                ],
            ),
            request_id=request_id,
        )
        version = result.version
        if version.schema_version != configuration.schema_version:
            _blocked(
                "ACTIVATION_CONFIGURATION_SCHEMA_VERSION_INVALID",
                "The registered configuration type does not expose the reviewed v2 schema.",
                type_key=configuration.type_key,
                expected=configuration.schema_version,
                observed=version.schema_version,
            )
        self._require_result_dependencies(result.dependencies, resolved_dependencies)
        self._persist_specialized(
            configuration,
            version_id=version.id,
            payload=resolved_payload,
            root=resolved_specialized_rows[0],
            dependencies=resolved_dependencies,
            replay=result.idempotent_replay,
        )
        self.db.flush()
        return version.id

    def validate_version(
        self, *, type_key: str, version_id: int, request_id: str
    ) -> None:
        self._preflight()
        self.management.validate_version(
            config_type=type_key,
            version_id=version_id,
            request=ConfigurationVersionActionRequest(
                scope_type=_SCOPE_TYPE,
                scope_key=_SCOPE_KEY,
                reason="Issue 707 reviewed V1.3 profile validation",
            ),
            request_id=request_id,
        )

    def activate_version(
        self,
        *,
        type_key: str,
        version_id: int,
        scope_type: str,
        scope_key: str,
        request_id: str,
    ) -> None:
        self._preflight()
        if scope_type != _SCOPE_TYPE or scope_key != _SCOPE_KEY:
            _blocked(
                "ACTIVATION_SCOPE_INVALID",
                "Owner activation is restricted to the reviewed production research scope.",
            )
        self.management.activate_version(
            config_type=type_key,
            version_id=version_id,
            request=ConfigurationVersionActionRequest(
                scope_type=scope_type,
                scope_key=scope_key,
                reason="Issue 707 explicit V1.3 scope activation",
            ),
            request_id=request_id,
        )

    def materialize_bundle(
        self,
        *,
        workflow_kind: str,
        scope_type: str,
        scope_key: str,
        aggregate_version_id: int,
        installed_adapter_manifest_digest: str,
        request_id: str,
    ) -> tuple[int, str, bool]:
        self._preflight()
        if (
            workflow_kind != "RESEARCH"
            or scope_type != _SCOPE_TYPE
            or scope_key != _SCOPE_KEY
        ):
            _blocked(
                "ACTIVATION_BUNDLE_SCOPE_INVALID",
                "The immutable bundle must use the reviewed research workflow scope.",
            )
        if installed_adapter_manifest_digest != current_adapter_manifest_digest():
            _blocked(
                "ACTIVATION_ADAPTER_MANIFEST_DIGEST_INVALID",
                "The activation plan adapter manifest no longer matches this release.",
            )
        self.management.repository.acquire_idempotency_lock(request_id)
        resolution = self.resolver.resolve_active(
            workflow_kind=workflow_kind,
            aggregate_config_type="research-profile",
            scope_type=scope_type,
            scope_key=scope_key,
            lock_activation=True,
        )
        if resolution.aggregate_profile_version_id != aggregate_version_id:
            _blocked(
                "ACTIVATION_AGGREGATE_VERSION_MISMATCH",
                "The active research profile is not the reviewed aggregate version.",
            )
        existing = self.management.repository.find_bundle(
            workflow_kind=workflow_kind,
            scope_type=scope_type,
            scope_key=scope_key,
            bundle_digest=resolution.bundle_digest,
        )
        persisted = self.resolver.materialize_bundle(resolution)
        snapshot = self.resolver.read_bundle(persisted.snapshot_id)
        registry_digest = snapshot.capability_snapshot.get("adapter_registry_digest")
        verified = validate_frozen_configuration_bundle(
            snapshot,
            expected_adapter_registry_digest=registry_digest,
        )
        observed_profiles = {
            type_key: verified.require_single_version(type_key).id
            for type_key in _PROFILE_TYPES
        }
        if observed_profiles["research-profile"] != aggregate_version_id:
            _blocked(
                "ACTIVATION_BUNDLE_PROFILE_SET_INVALID",
                "The frozen bundle does not bind the reviewed research aggregate.",
            )
        generation = generation_profile(verified)
        diversity = diversity_profile(verified)
        scoring = scoring_profile(verified)
        quality_payload = verified.require_single_version(
            "quality-gate-profile"
        ).payload_json
        if (
            quality_payload.get("profile_key") != _QUALITY_PROFILE_KEY
            or quality_payload.get("quality_components")
            != list(scoring.quality_components)
            or quality_payload.get("elimination_rules")
            != list(scoring.elimination_rules)
            or quality_payload.get("warning_rules") != list(scoring.warning_rules)
        ):
            _blocked(
                "ACTIVATION_BUNDLE_QUALITY_BINDING_INVALID",
                "The quality and scoring profiles do not persist one exact rule contract.",
            )
        research_payload = verified.require_single_version(
            "research-profile"
        ).payload_json
        expected_links = {
            "generation_profile_version_id": observed_profiles["generation-profile"],
            "diversity_profile_version_id": observed_profiles["diversity-profile"],
            "quality_gate_profile_version_id": observed_profiles[
                "quality-gate-profile"
            ],
            "scoring_profile_version_id": observed_profiles["scoring-profile"],
        }
        if any(research_payload.get(key) != value for key, value in expected_links.items()):
            _blocked(
                "ACTIVATION_BUNDLE_PROFILE_BINDING_INVALID",
                "The research aggregate does not bind the five resolved profiles.",
            )
        if (
            diversity.generation_profile_version_id != generation.version_id
            or set(diversity.required_family_version_ids)
            != set(generation.family_version_ids)
        ):
            _blocked(
                "ACTIVATION_BUNDLE_PROFILE_BINDING_INVALID",
                "The diversity profile does not bind the resolved generation contract.",
            )
        if snapshot.capability_snapshot.get("installed_adapter_manifest_digest") != (
            installed_adapter_manifest_digest
        ):
            _blocked(
                "ACTIVATION_BUNDLE_MANIFEST_DIGEST_MISMATCH",
                "The frozen bundle does not bind the installed adapter manifest.",
            )
        return persisted.snapshot_id, persisted.bundle_digest, existing is not None

    def _preflight(self) -> None:
        bind = self.db.get_bind()
        if bind.dialect.name != "postgresql":
            _blocked(
                "ACTIVATION_POSTGRESQL_REQUIRED",
                "Owner activation requires PostgreSQL.",
            )
        database_name = self.db.execute(text("SELECT current_database()")).scalar_one()
        if database_name != _DATABASE_NAME:
            _blocked(
                "ACTIVATION_DATABASE_INVALID",
                "Owner activation is restricted to the isolated V1.3 database.",
                observed_database=database_name,
            )
        self.management.repository.require_owner_connection()
        ownership = (
            self.db.execute(
                text(
                    "SELECT current_user AS current_user,current_schema() AS schema_name,"
                    "COUNT(*) AS table_count,COALESCE(bool_and("
                    "pg_get_userbyid(table_class.relowner)=current_user),false) AS owns_all "
                    "FROM pg_class AS table_class JOIN pg_namespace AS namespace "
                    "ON namespace.oid=table_class.relnamespace "
                    "WHERE namespace.nspname=current_schema() "
                    "AND table_class.relname=ANY(:table_names)"
                ),
                {"table_names": list(_OWNER_WRITE_TABLES)},
            )
            .mappings()
            .one()
        )
        if (
            ownership["schema_name"] != "public"
            or ownership["table_count"] != len(_OWNER_WRITE_TABLES)
            or ownership["owns_all"] is not True
        ):
            _blocked(
                "ACTIVATION_OWNER_TABLE_OWNERSHIP_INVALID",
                "The owner session must own every table in the activation write set.",
                database_role=ownership["current_user"],
                observed_table_count=ownership["table_count"],
            )
        migration = (
            self.db.execute(
                text(
                    "SELECT COUNT(*) FILTER (WHERE status IN "
                    "('PLANNED','RUNNING','RECONCILING')) AS nonterminal_count,"
                    "COUNT(*) FILTER (WHERE status='SUCCEEDED') AS succeeded_count,"
                    "MAX(target_schema_version) FILTER (WHERE status='SUCCEEDED') "
                    "AS target_schema_version,"
                    "MAX(destructive_write_count) FILTER (WHERE status='SUCCEEDED') "
                    "AS destructive_write_count,"
                    "MAX(overwritten_row_count) FILTER (WHERE status='SUCCEEDED') "
                    "AS overwritten_row_count,"
                    "MAX(deleted_row_count) FILTER (WHERE status='SUCCEEDED') "
                    "AS deleted_row_count FROM strategy_platform_migration_runs "
                    "WHERE migration_key=:migration_key "
                    "AND execution_scope='DESIGN_LAB'"
                ),
                {"migration_key": _MIGRATION_KEY},
            )
            .mappings()
            .one()
        )
        if (
            migration["nonterminal_count"] != 0
            or migration["succeeded_count"] != 1
            or migration["target_schema_version"] != _SCHEMA_VERSION
            or any(
                migration[key] != 0
                for key in (
                    "destructive_write_count",
                    "overwritten_row_count",
                    "deleted_row_count",
                )
            )
        ):
            _blocked(
                "ACTIVATION_MIGRATION_EVIDENCE_INVALID",
                "Owner activation requires one unambiguous terminal, "
                "non-destructive V1.3 v47 migration and no active run.",
            )

    @staticmethod
    def _adapter_values(adapter: InstalledAdapter) -> dict[str, Any]:
        return {
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
                "installed_manifest_digest": current_adapter_manifest_digest(),
            },
            "enabled": True,
            "registry_metadata_only": True,
            "contains_secret_material": False,
            "contains_executable_payload": False,
        }

    def _require_dependencies(
        self,
        configuration: ConfigurationPlan,
        dependencies: Sequence[tuple[str, int]],
    ) -> None:
        for relation, version_id in dependencies:
            version = self.db.get(ConfigurationVersion, version_id)
            if version is None or version.lifecycle_status != "VALIDATED":
                _blocked(
                    "ACTIVATION_DEPENDENCY_INVALID",
                    "Every activation dependency must be an existing VALIDATED version.",
                    relation_key=relation,
                    version_id=version_id,
                )
            if relation.startswith("strategy_family:"):
                if self.db.get(StrategyFamilyDefinitionVersion, version_id) is None:
                    _blocked(
                        "ACTIVATION_STRATEGY_FAMILY_MISSING",
                        "A generation family dependency is not registered.",
                        version_id=version_id,
                    )
            elif relation.startswith("metric:"):
                metric = self._metric(version_id)
                if metric is None or metric[0] != relation.removeprefix("metric:"):
                    _blocked(
                        "ACTIVATION_METRIC_MISSING",
                        "A profile metric dependency is not registered under the exact key.",
                        relation_key=relation,
                        version_id=version_id,
                    )
        for key in self._referenced_adapter_keys(configuration):
            adapter = self.db.get(AdapterDefinition, key)
            if adapter is None or adapter.enabled is not True:
                _blocked(
                    "ACTIVATION_ADAPTER_MISSING",
                    "A specialized profile references an unavailable adapter.",
                    adapter_key=key,
                )

    @staticmethod
    def _referenced_adapter_keys(configuration: ConfigurationPlan) -> set[str]:
        keys: set[str] = set()
        for row in configuration.specialized_rows:
            for field in (
                "evaluation_adapter_key",
                "scoring_adapter_key",
                "normalization_adapter_key",
            ):
                value = row.get(field)
                if isinstance(value, str):
                    keys.add(value)
        if configuration.plan_key == "quality":
            keys.add("threshold-comparison-v1")
        return keys

    @staticmethod
    def _require_result_dependencies(observed, expected) -> None:
        observed_edges = sorted(
            (row.relation_key, row.depends_on_version_id) for row in observed
        )
        if observed_edges != sorted(expected):
            _blocked(
                "ACTIVATION_DEPENDENCY_REPLAY_CONFLICT",
                "Persisted draft dependencies do not match the reviewed plan.",
            )

    def _persist_specialized(
        self,
        configuration: ConfigurationPlan,
        *,
        version_id: int,
        payload: Mapping[str, Any],
        root: Mapping[str, Any],
        dependencies: Sequence[tuple[str, int]],
        replay: bool,
    ) -> None:
        writers = {
            "generation": self._generation_rows,
            "diversity": self._diversity_rows,
            "quality": self._quality_rows,
            "scoring": self._scoring_rows,
            "research": self._research_rows,
        }
        writer = writers.get(configuration.plan_key)
        if writer is None or configuration.specialized_table not in {
            "generation_profile_versions",
            "diversity_profile_versions",
            "quality_gate_profile_versions",
            "scoring_profile_versions",
            "research_profile_versions",
        }:
            _blocked(
                "ACTIVATION_SPECIALIZED_TABLE_INVALID",
                "Activation plan requested an unsupported specialized table.",
            )
        writer(version_id, payload, root, dependencies, replay)

    def _generation_rows(self, version_id, payload, root, dependencies, replay) -> None:
        expected = {"configuration_version_id": version_id, **dict(root)}
        self._ensure_model(GenerationProfileVersion, version_id, expected, replay)
        self.db.flush()
        family_ids = tuple(payload["strategy_family_version_ids"])
        expected_rows = [
            {
                "generation_profile_version_id": version_id,
                "strategy_family_definition_version_id": family_id,
                "allocation_count": None,
                "ordinal": ordinal,
                "enabled": True,
            }
            for ordinal, family_id in enumerate(family_ids, start=1)
        ]
        self._ensure_collection(
            GenerationProfileFamily,
            GenerationProfileFamily.generation_profile_version_id,
            version_id,
            expected_rows,
            replay,
        )

    def _diversity_rows(self, version_id, payload, root, dependencies, replay) -> None:
        self._ensure_model(
            DiversityProfileVersion,
            version_id,
            {"configuration_version_id": version_id, **dict(root)},
            replay,
        )
        self.db.flush()
        adapter_key = str(root["evaluation_adapter_key"])
        expected_rows = [
            {
                "profile_version_id": version_id,
                "metric_key": metric_key,
                "comparison_adapter_key": adapter_key,
                "parameters": {"operator": "<="},
                "threshold_value": Decimal(str(threshold)),
                "unit": "ratio",
                "severity": "BLOCKING",
                "priority": ordinal * 10,
            }
            for ordinal, (metric_key, threshold) in enumerate(
                payload["thresholds"].items(), start=1
            )
        ]
        self._ensure_collection(
            DiversityRule,
            DiversityRule.profile_version_id,
            version_id,
            expected_rows,
            replay,
        )

    def _quality_rows(self, version_id, payload, root, dependencies, replay) -> None:
        if (
            root.get("profile_key") != _QUALITY_PROFILE_KEY
            or root.get("rules")
            != [*payload["elimination_rules"], *payload["warning_rules"]]
        ):
            _blocked(
                "ACTIVATION_QUALITY_SPECIALIZATION_INVALID",
                "Quality specialized rows do not match the reviewed rule payload.",
            )
        profile = self.db.scalar(
            select(QualityGateProfile).where(
                QualityGateProfile.profile_key == _QUALITY_PROFILE_KEY
            )
        )
        if profile is None:
            if replay:
                _blocked(
                    "ACTIVATION_SPECIALIZED_REPLAY_INCOMPLETE",
                    "Idempotent replay is missing the stable quality profile.",
                )
            profile = QualityGateProfile(
                profile_key=_QUALITY_PROFILE_KEY, name=_QUALITY_PROFILE_NAME
            )
            self.db.add(profile)
            self.db.flush()
        elif profile.name != _QUALITY_PROFILE_NAME:
            _blocked(
                "ACTIVATION_SPECIALIZED_REPLAY_CONFLICT",
                "Stable quality profile metadata differs from the reviewed contract.",
            )
        self._ensure_model(
            QualityGateProfileVersion,
            version_id,
            {
                "configuration_version_id": version_id,
                "quality_gate_profile_id": profile.id,
            },
            replay,
        )
        self.db.flush()
        metrics = self._metric_ids(dependencies)
        rules = [*payload["elimination_rules"], *payload["warning_rules"]]
        expected_rows = []
        for ordinal, rule in enumerate(rules, start=1):
            operator = _RULE_OPERATOR.get(str(rule["operator"]))
            if operator is None:
                _blocked(
                    "ACTIVATION_QUALITY_OPERATOR_INVALID",
                    "Quality rule operator is unsupported by the persisted comparator.",
                )
            threshold = rule["threshold"]
            metric = self._metric(metrics[str(rule["metric_key"])])
            if metric is None:
                _blocked("ACTIVATION_METRIC_MISSING", "Quality metric is missing.")
            expected_rows.append(
                {
                    "profile_version_id": version_id,
                    "pair": None,
                    "timeframe": None,
                    "window_selector": {"mode": "profile_required_windows"},
                    "metric_definition_id": metrics[str(rule["metric_key"])],
                    "evaluation_adapter_key": "threshold-comparison-v1",
                    "evaluation_parameters": {
                        "operator": operator,
                        "rule_key": rule["rule_key"],
                        "match_when_missing": rule.get("match_when_missing", False),
                    },
                    "threshold_value": Decimal(int(threshold) if isinstance(threshold, bool) else str(threshold)),
                    "threshold_max": None,
                    "unit": metric[2],
                    "severity": (
                        "BLOCKING"
                        if rule in payload["elimination_rules"]
                        else "WARNING"
                    ),
                    "score_weight": None,
                    "priority": ordinal * 10,
                }
            )
        self._ensure_collection(
            QualityGateRule,
            QualityGateRule.profile_version_id,
            version_id,
            expected_rows,
            replay,
        )

    def _scoring_rows(self, version_id, payload, root, dependencies, replay) -> None:
        self._ensure_model(
            ScoringProfileVersion,
            version_id,
            {"configuration_version_id": version_id, **dict(root)},
            replay,
        )
        self.db.flush()
        metrics = self._metric_ids(dependencies)
        expected_rows = []
        for ordinal, (metric_key, weight) in enumerate(
            payload["component_weights"].items(), start=1
        ):
            metric = self._metric(metrics[metric_key])
            if metric is None:
                _blocked("ACTIVATION_METRIC_MISSING", "Scoring metric is missing.")
            expected_rows.append(
                {
                    "profile_version_id": version_id,
                    "metric_definition_version_id": metrics[metric_key],
                    "normalization_adapter_key": root["scoring_adapter_key"],
                    "normalization_parameters": dict(
                        payload["normalization_rules"][metric_key]
                    ),
                    "weight": Decimal(str(weight)),
                    "data_source": metric[1],
                    "aggregation_method": root["aggregation_method"],
                    "window_selector": dict(root["primary_window_selector"]),
                    "priority": ordinal * 10,
                }
            )
        self._ensure_collection(
            ScoringRule,
            ScoringRule.profile_version_id,
            version_id,
            expected_rows,
            replay,
        )

    def _research_rows(self, version_id, payload, root, dependencies, replay) -> None:
        self._ensure_model(
            ResearchProfileVersion,
            version_id,
            {"configuration_version_id": version_id, **dict(root)},
            replay,
        )

    def _metric_ids(self, dependencies: Sequence[tuple[str, int]]) -> dict[str, int]:
        return {
            relation.removeprefix("metric:"): version_id
            for relation, version_id in dependencies
            if relation.startswith("metric:")
        }

    def _metric(self, version_id: int) -> tuple[str, str, str] | None:
        return self.db.execute(
            select(
                MetricDefinition.metric_key,
                MetricDefinitionVersion.data_source,
                MetricDefinitionVersion.unit,
            )
            .join(
                MetricDefinitionVersion,
                MetricDefinitionVersion.metric_definition_id == MetricDefinition.id,
            )
            .where(MetricDefinitionVersion.configuration_version_id == version_id)
        ).one_or_none()

    def _ensure_model(self, model, key: int, expected: Mapping[str, Any], replay: bool) -> None:
        row = self.db.get(model, key)
        if row is None:
            if replay:
                _blocked(
                    "ACTIVATION_SPECIALIZED_REPLAY_INCOMPLETE",
                    "Idempotent replay is missing specialized profile state.",
                )
            self.db.add(model(**expected))
            return
        self._require_exact_model(
            row,
            expected,
            code="ACTIVATION_SPECIALIZED_REPLAY_CONFLICT",
            identity={"configuration_version_id": key},
        )

    def _ensure_collection(
        self, model, parent_column, parent_id: int, expected_rows, replay: bool
    ) -> None:
        observed = list(
            self.db.scalars(
                select(model).where(parent_column == parent_id).order_by(model.id)
            ).all()
        )
        if not observed:
            if replay:
                _blocked(
                    "ACTIVATION_SPECIALIZED_REPLAY_INCOMPLETE",
                    "Idempotent replay is missing specialized child rows.",
                )
            self.db.add_all(model(**values) for values in expected_rows)
            return
        if len(observed) != len(expected_rows):
            _blocked(
                "ACTIVATION_SPECIALIZED_REPLAY_CONFLICT",
                "Specialized child row cardinality differs from the reviewed contract.",
            )
        for row, expected in zip(observed, expected_rows):
            self._require_exact_model(
                row,
                expected,
                code="ACTIVATION_SPECIALIZED_REPLAY_CONFLICT",
                identity={"parent_id": parent_id},
            )

    @staticmethod
    def _require_exact_model(row, expected, *, code: str, identity) -> None:
        mismatched = sorted(
            key for key, value in expected.items() if getattr(row, key) != value
        )
        if mismatched:
            _blocked(
                code,
                "Persisted owner activation state differs from the reviewed contract.",
                **identity,
                mismatched_fields=mismatched,
            )


def _blocked(code: str, message: str, **context: Any) -> None:
    raise StrategyPlatformReadError(code, message, context=context)
