"""Owner-only prerequisites for the V1.3 profile-bound research activation.

The functions in this module never commit.  A caller must keep schema
evolution, metric registration, profile activation and bundle materialization
inside one owner-controlled transaction and may roll the transaction back for
dry-run evidence.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.strategy_platform_errors import StrategyPlatformReadError
from app.db.migrations import strategy_platform_v13_owner_schema_problems
from app.models.strategy_platform import (
    ConfigurationActivation,
    ConfigurationAuditEvent,
    ConfigurationType,
    ConfigurationVersion,
    MetricDefinition,
    MetricDefinitionVersion,
)
from app.models.strategy_platform_extensions import (
    AdapterDefinition,
    GenerationProfileFamily,
    ResearchProfileVersion,
    ResearchTargetConfig,
)
from app.repositories.strategy_platform import StrategyPlatformConfigurationRepository
from app.schemas.strategy_platform import (
    ConfigurationDraftCreateRequest,
    ConfigurationVersionActionRequest,
)
from app.services.configuration_management import ConfigurationManagementService
from app.services.owner_research_activation import (
    OwnerActivationResult,
    OwnerResearchActivationPlan,
)
from app.services.strategy_platform_configuration_validation import (
    infer_closed_json_schema,
)
from app.services.strategy_platform_adapter_registry import (
    INSTALLED_ADAPTER_MANIFEST,
    installed_adapter_manifest_digest,
)


SCHEMA_EVOLUTION_CONTRACT = "strategy-platform-configuration-schema-registry-v1"
METRIC_REGISTRATION_CONTRACT = "profile-bound-score-v2-metric-registry-v1"
PROFILE_BOUND_SCORE_METRIC_KEYS: tuple[str, ...] = (
    "profit_score",
    "risk_score",
    "stability_score",
    "quality_score",
    "required_windows_score",
    "static_quality_score",
    "net_profit",
    "max_drawdown",
    "total_trades",
    "win_rate",
    "quality_error_count",
    "quality_warning_count",
    "all_metrics_missing",
    "validation_error",
)


@dataclass(frozen=True)
class RequiredMetricDefinition:
    metric_key: str
    name_zh: str
    unit: str
    data_source: str


@dataclass(frozen=True)
class ExistingResearchSource:
    research_profile_version_id: int
    provider_model_config_version_id: int
    research_target_config_set_id: int
    validation_window_config_set_id: int
    market_data_policy_version_id: int
    evidence_freshness_profile_version_id: int
    scheduler_profile_version_id: int
    worker_execution_profile_version_id: int
    strategy_family_version_ids: tuple[int, ...]
    target_count: int


REQUIRED_V2_METRICS: tuple[RequiredMetricDefinition, ...] = (
    RequiredMetricDefinition(
        "required_windows_score",
        "必需验证窗口得分",
        "score",
        "strategy_evaluation_summaries.required_window_counts",
    ),
    RequiredMetricDefinition(
        "static_quality_score",
        "静态质量得分",
        "score",
        "strategy_versions.static_validation_evidence",
    ),
    RequiredMetricDefinition(
        "net_profit",
        "净收益率",
        "ratio",
        "backtest_results.profit_pct",
    ),
    RequiredMetricDefinition(
        "win_rate",
        "胜率",
        "ratio",
        "backtest_results.win_rate",
    ),
    RequiredMetricDefinition(
        "quality_error_count",
        "质量错误数量",
        "count",
        "strategy_evaluation_summaries.quality_error_count",
    ),
    RequiredMetricDefinition(
        "quality_warning_count",
        "质量警告数量",
        "count",
        "strategy_evaluation_summaries.quality_warning_count",
    ),
    RequiredMetricDefinition(
        "all_metrics_missing",
        "全部指标缺失",
        "boolean",
        "strategy_evaluation_summaries.metric_completeness",
    ),
    RequiredMetricDefinition(
        "validation_error",
        "验证错误",
        "boolean",
        "strategy_validation_plans.error_evidence",
    ),
)


def assert_owner_activation_fence(db: Session) -> None:
    """Acquire the unique owner lock and verify a quiet accepted owner database."""

    repository = StrategyPlatformConfigurationRepository(db)
    repository.require_owner_connection()
    connection = db.connection()
    problems = strategy_platform_v13_owner_schema_problems(
        connection,
        expected_database="freqtrade_ai_design_lab",
    )
    if problems:
        _blocked(
            "OWNER_DATABASE_ATTESTATION_FAILED",
            "Owner database schema, ACL, migration, or secret-count evidence drifted.",
            problems=problems,
        )
    connection.execute(
        text(
            "SELECT pg_advisory_xact_lock("
            "hashtextextended('strategy-platform-v13-owner-activation',0))"
        )
    )
    activity = connection.execute(
        text(
            "SELECT "
            "(SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
            "AND pid<>pg_backend_pid() AND backend_type='client backend' "
            "AND state<>'idle') AS active_clients,"
            "(SELECT count(*) FROM pg_prepared_xacts "
            "WHERE database=current_database()) AS prepared_transactions,"
            "(SELECT count(*) FROM okx_demo_attestation_secrets) "
            "AS attestation_secret_rows,"
            "(SELECT count(*) FROM okx_demo_operator_consent_secrets) "
            "AS operator_secret_rows"
        )
    ).mappings().one()
    if any(int(value) != 0 for value in activity.values()):
        _blocked(
            "OWNER_ACTIVATION_FENCE_BLOCKED",
            "Owner activation requires zero competing clients, prepared work, and secrets.",
            **{key: int(value) for key, value in activity.items()},
        )


def discover_existing_research_source(
    db: Session,
    *,
    scope_type: str,
    scope_key: str,
) -> ExistingResearchSource:
    """Read one explicit validated research assembly as the v2 dependency source."""

    repository = StrategyPlatformConfigurationRepository(db)
    repository.require_owner_connection()
    activation = db.scalar(
        select(ConfigurationActivation).where(
            ConfigurationActivation.config_type == "research-profile",
            ConfigurationActivation.scope_type == scope_type,
            ConfigurationActivation.scope_key == scope_key,
        )
    )
    if activation is None:
        _blocked(
            "SOURCE_RESEARCH_ACTIVATION_NOT_FOUND",
            "Explicit source research activation does not exist.",
            scope_type=scope_type,
            scope_key=scope_key,
        )
    version = db.get(ConfigurationVersion, activation.version_id)
    profile = db.get(ResearchProfileVersion, activation.version_id)
    if (
        version is None
        or version.lifecycle_status != "VALIDATED"
        or profile is None
    ):
        _blocked(
            "SOURCE_RESEARCH_PROFILE_INVALID",
            "Source research profile is missing, incomplete, or not VALIDATED.",
            version_id=activation.version_id,
        )
    family_ids = tuple(
        db.scalars(
            select(GenerationProfileFamily.strategy_family_definition_version_id)
            .where(
                GenerationProfileFamily.generation_profile_version_id
                == profile.generation_profile_version_id,
                GenerationProfileFamily.enabled.is_(True),
            )
            .order_by(
                GenerationProfileFamily.ordinal,
                GenerationProfileFamily.strategy_family_definition_version_id,
            )
        ).all()
    )
    if not family_ids or len(family_ids) != len(set(family_ids)):
        _blocked(
            "SOURCE_STRATEGY_FAMILY_SET_INVALID",
            "Source generation profile has no exact unique enabled family set.",
        )
    target_count = int(
        db.scalar(
            select(func.count(ResearchTargetConfig.id)).where(
                ResearchTargetConfig.config_set_id
                == profile.research_target_config_set_id,
                ResearchTargetConfig.enabled.is_(True),
            )
        )
        or 0
    )
    if target_count <= 0:
        _blocked(
            "SOURCE_RESEARCH_TARGET_SET_EMPTY",
            "Source research target set has no enabled targets.",
        )
    return ExistingResearchSource(
        research_profile_version_id=version.id,
        provider_model_config_version_id=profile.provider_model_config_version_id,
        research_target_config_set_id=profile.research_target_config_set_id,
        validation_window_config_set_id=profile.validation_window_config_set_id,
        market_data_policy_version_id=profile.market_data_policy_version_id,
        evidence_freshness_profile_version_id=(
            profile.evidence_freshness_profile_version_id
        ),
        scheduler_profile_version_id=profile.scheduler_profile_version_id,
        worker_execution_profile_version_id=(
            profile.worker_execution_profile_version_id
        ),
        strategy_family_version_ids=family_ids,
        target_count=target_count,
    )


def evolve_activation_configuration_schemas(
    db: Session,
    plan: OwnerResearchActivationPlan,
    *,
    actor: str,
) -> None:
    """Install exact v2 closed schemas while retaining every previous schema."""

    repository = StrategyPlatformConfigurationRepository(db)
    repository.require_owner_connection()
    if not actor.strip():
        _blocked("OWNER_ACTOR_REQUIRED", "Owner schema evolution requires an actor.")
    for configuration in plan.configurations:
        type_row = repository.get_type(configuration.type_key)
        if type_row is None or type_row.enabled is not True:
            _blocked(
                "CONFIGURATION_TYPE_NOT_AVAILABLE",
                "Activation configuration type is missing or disabled.",
                config_type=configuration.type_key,
            )
        capability = _evolved_capability(
            type_row,
            schema_version=configuration.schema_version,
            schema=infer_closed_json_schema(
                _schema_shape_value(dict(configuration.payload))
            ),
            actor=actor,
        )
        if (
            type_row.schema_version == configuration.schema_version
            and type_row.editor_capability == capability
        ):
            continue
        type_row.schema_version = configuration.schema_version
        type_row.editor_capability = capability
        db.flush()


def reconcile_installed_adapter_registry(db: Session) -> None:
    """Make the metadata-only DB registry exactly match the installed manifest.

    Existing implementation identities are never rewritten.  The only allowed
    update is rebinding their display metadata to the current complete manifest
    digest; a source/schema/capability drift blocks the whole transaction.
    """

    StrategyPlatformConfigurationRepository(db).require_owner_connection()
    manifest_digest = installed_adapter_manifest_digest()
    installed_keys = {item.adapter_key for item in INSTALLED_ADAPTER_MANIFEST}
    observed_keys = set(db.scalars(select(AdapterDefinition.adapter_key)).all())
    unexpected = sorted(observed_keys - installed_keys)
    if unexpected:
        _blocked(
            "ADAPTER_REGISTRY_UNEXPECTED_ENTRY",
            "Persisted adapter registry contains an uninstalled adapter.",
            adapter_keys=unexpected,
        )
    for adapter in INSTALLED_ADAPTER_MANIFEST:
        expected = {
            "adapter_kind": adapter.adapter_kind,
            "implementation_version": adapter.implementation_version,
            "input_schema_version": adapter.input_schema_version,
            "output_schema_version": adapter.output_schema_version,
            "capabilities": dict(adapter.capabilities),
            "enabled": True,
            "registry_metadata_only": True,
            "contains_secret_material": False,
            "contains_executable_payload": False,
        }
        display = {
            "input_schema": adapter.input_schema,
            "output_schema": adapter.output_schema,
            "source_ref": adapter.source_ref,
            "source_sha256": adapter.source_sha256,
            "installed_manifest_digest": manifest_digest,
        }
        row = db.get(AdapterDefinition, adapter.adapter_key)
        if row is None:
            db.add(
                AdapterDefinition(
                    adapter_key=adapter.adapter_key,
                    display_metadata=display,
                    **expected,
                )
            )
            continue
        drift = sorted(
            key for key, value in expected.items() if getattr(row, key) != value
        )
        existing_display = dict(row.display_metadata or {})
        display_without_manifest = {
            key: value
            for key, value in existing_display.items()
            if key != "installed_manifest_digest"
        }
        expected_without_manifest = {
            key: value
            for key, value in display.items()
            if key != "installed_manifest_digest"
        }
        if drift or display_without_manifest != expected_without_manifest:
            _blocked(
                "ADAPTER_REGISTRY_IMPLEMENTATION_DRIFT",
                "Persisted adapter implementation identity differs from this release.",
                adapter_key=adapter.adapter_key,
                mismatched_fields=drift,
            )
        if existing_display != display:
            row.display_metadata = display
    db.flush()


def record_owner_activation_registry_audit(
    db: Session,
    plan: OwnerResearchActivationPlan,
    result: OwnerActivationResult,
    *,
    actor: str,
) -> None:
    """Append exact schema/adapter evolution evidence to every new v2 version."""

    for configuration in plan.configurations:
        version_id = result.version_ids[configuration.plan_key]
        type_row = db.get(ConfigurationType, configuration.type_key)
        if type_row is None:
            _blocked(
                "CONFIGURATION_TYPE_NOT_AVAILABLE",
                "Activation audit cannot resolve its configuration type.",
                config_type=configuration.type_key,
            )
        request_id = f"{plan.request_id}:{configuration.plan_key}:registry-audit"
        snapshot = {
            "contract": SCHEMA_EVOLUTION_CONTRACT,
            "plan_digest": plan.plan_digest,
            "configuration_type": configuration.type_key,
            "configuration_version_id": version_id,
            "schema_version": configuration.schema_version,
            "schema_registry_digest": _digest(
                type_row.editor_capability.get("schema_versions")
            ),
            "installed_adapter_manifest_digest": (
                plan.installed_adapter_manifest_digest
            ),
            "historical_values_recalculated": False,
            "worker_started": False,
            "backtest_started": False,
            "signal_or_order_created": False,
            "credential_values_recorded": False,
        }
        existing = db.scalar(
            select(ConfigurationAuditEvent).where(
                ConfigurationAuditEvent.request_id == request_id
            )
        )
        if existing is None:
            db.add(
                ConfigurationAuditEvent(
                    configuration_version_id=version_id,
                    event_type="VALIDATED",
                    actor=actor,
                    request_id=request_id,
                    scope_type=plan.scope_type,
                    scope_key=plan.scope_key,
                    reason="Issue 709 owner activation registry evidence",
                    event_snapshot=snapshot,
                )
            )
            continue
        if (
            existing.configuration_version_id != version_id
            or existing.event_type != "VALIDATED"
            or existing.actor != actor
            or existing.scope_type != plan.scope_type
            or existing.scope_key != plan.scope_key
            or existing.event_snapshot != snapshot
        ):
            _blocked(
                "ACTIVATION_REGISTRY_AUDIT_CONFLICT",
                "Existing registry audit does not match the reviewed activation.",
                request_id=request_id,
            )
    db.flush()


def ensure_required_v2_metric_versions(
    db: Session,
    *,
    actor: str,
    scope_type: str,
    scope_key: str,
    request_prefix: str,
) -> dict[str, int]:
    """Return exact validated metric versions, creating only missing v2 metrics."""

    repository = StrategyPlatformConfigurationRepository(db)
    repository.require_owner_connection()
    _enable_existing_schema_for_owner_writes(
        db,
        type_key="metric-definition",
        actor=actor,
    )
    service = ConfigurationManagementService(db, actor=actor)
    result: dict[str, int] = {}
    for definition in REQUIRED_V2_METRICS:
        stable = db.scalar(
            select(MetricDefinition).where(
                MetricDefinition.metric_key == definition.metric_key
            )
        )
        if stable is not None:
            versions = list(
                db.scalars(
                    select(MetricDefinitionVersion).where(
                        MetricDefinitionVersion.metric_definition_id == stable.id
                    )
                ).all()
            )
            exact = [
                item
                for item in versions
                if _metric_version_matches(item, definition)
                and _validated(db, item.configuration_version_id)
            ]
            if len(exact) != 1 or len(versions) != 1:
                _blocked(
                    "METRIC_DEFINITION_IDENTITY_CONFLICT",
                    "Existing metric definition is not one exact validated v2 identity.",
                    metric_key=definition.metric_key,
                )
            result[definition.metric_key] = exact[0].configuration_version_id
            continue

        payload = _metric_payload(definition)
        request_id = f"{request_prefix}:metric:{definition.metric_key}:draft"
        draft = service.create_draft(
            config_type="metric-definition",
            request=ConfigurationDraftCreateRequest(
                scope_type=scope_type,
                scope_key=scope_key,
                change_summary=(
                    f"Register {definition.metric_key} for {METRIC_REGISTRATION_CONTRACT}"
                ),
                payload_json=payload,
                dependencies=[],
            ),
            request_id=request_id,
        )
        version_id = draft.version.id
        stable = MetricDefinition(metric_key=definition.metric_key)
        db.add(stable)
        db.flush()
        db.add(
            MetricDefinitionVersion(
                configuration_version_id=version_id,
                metric_definition_id=stable.id,
                name_zh=definition.name_zh,
                unit=definition.unit,
                data_source=definition.data_source,
                available_aggregations=["identity"],
                display_metadata={
                    "contract": METRIC_REGISTRATION_CONTRACT,
                    "historical_values_recalculated": False,
                },
            )
        )
        db.flush()
        service.validate_version(
            config_type="metric-definition",
            version_id=version_id,
            request=ConfigurationVersionActionRequest(
                scope_type=scope_type,
                scope_key=scope_key,
                reason=f"Validate {METRIC_REGISTRATION_CONTRACT}",
            ),
            request_id=f"{request_prefix}:metric:{definition.metric_key}:validate",
        )
        result[definition.metric_key] = version_id
    return result


def discover_profile_bound_metric_versions(db: Session) -> dict[str, int]:
    """Resolve the exact VALIDATED metric set required by the v2 scorer."""

    rows = db.execute(
        select(
            MetricDefinition.metric_key,
            MetricDefinitionVersion.configuration_version_id,
            ConfigurationVersion.lifecycle_status,
        )
        .join(
            MetricDefinitionVersion,
            MetricDefinitionVersion.metric_definition_id == MetricDefinition.id,
        )
        .join(
            ConfigurationVersion,
            ConfigurationVersion.id
            == MetricDefinitionVersion.configuration_version_id,
        )
        .where(MetricDefinition.metric_key.in_(PROFILE_BOUND_SCORE_METRIC_KEYS))
        .order_by(
            MetricDefinition.metric_key,
            MetricDefinitionVersion.configuration_version_id,
        )
    ).all()
    by_key: dict[str, list[int]] = {}
    for metric_key, version_id, lifecycle_status in rows:
        if lifecycle_status == "VALIDATED":
            by_key.setdefault(metric_key, []).append(int(version_id))
    missing = sorted(set(PROFILE_BOUND_SCORE_METRIC_KEYS) - set(by_key))
    ambiguous = sorted(key for key, values in by_key.items() if len(values) != 1)
    if missing or ambiguous:
        _blocked(
            "ACTIVATION_METRIC_SET_INVALID",
            "Profile-bound scoring requires one exact VALIDATED metric version per key.",
            missing=missing,
            ambiguous=ambiguous,
        )
    return {key: by_key[key][0] for key in PROFILE_BOUND_SCORE_METRIC_KEYS}


def _enable_existing_schema_for_owner_writes(
    db: Session,
    *,
    type_key: str,
    actor: str,
) -> None:
    row = db.get(ConfigurationType, type_key)
    if row is None or row.enabled is not True:
        _blocked(
            "CONFIGURATION_TYPE_NOT_AVAILABLE",
            "Required registry configuration type is missing or disabled.",
            config_type=type_key,
        )
    capability = copy.deepcopy(row.editor_capability)
    if not isinstance(capability, dict) or not isinstance(
        capability.get("json_schema"), Mapping
    ):
        _blocked(
            "CONFIGURATION_SCHEMA_UNAVAILABLE",
            "Required registry type has no closed schema.",
            config_type=type_key,
        )
    if capability.get("schema_is_closed") is not True:
        _blocked(
            "CONFIGURATION_SCHEMA_NOT_CLOSED",
            "Owner writes require a closed schema registry.",
            config_type=type_key,
        )
    capability["write_enabled"] = True
    capability["read_only"] = False
    capability["schema_versions"] = {
        **dict(capability.get("schema_versions") or {}),
        row.schema_version: copy.deepcopy(capability["json_schema"]),
    }
    capability["schema_evolution"] = {
        "contract": SCHEMA_EVOLUTION_CONTRACT,
        "historical_schema_preserved": True,
    }
    row.editor_capability = capability
    db.flush()


def _evolved_capability(
    type_row: ConfigurationType,
    *,
    schema_version: str,
    schema: Mapping[str, Any],
    actor: str,
) -> dict[str, Any]:
    capability = copy.deepcopy(type_row.editor_capability)
    if not isinstance(capability, dict) or not isinstance(
        capability.get("json_schema"), Mapping
    ):
        _blocked(
            "CONFIGURATION_SCHEMA_UNAVAILABLE",
            "Configuration type has no current closed schema to preserve.",
            config_type=type_row.type_key,
        )
    if capability.get("schema_is_closed") is not True:
        _blocked(
            "CONFIGURATION_SCHEMA_NOT_CLOSED",
            "V1.3 schema evolution refuses open configuration schemas.",
            config_type=type_row.type_key,
        )
    declared = capability.get("schema_versions")
    if declared is not None and not isinstance(declared, Mapping):
        _blocked(
            "CONFIGURATION_SCHEMA_REGISTRY_INVALID",
            "Existing configuration schema registry is invalid.",
            config_type=type_row.type_key,
        )
    schemas = copy.deepcopy(dict(declared or {}))
    old_registered = schemas.get(type_row.schema_version)
    if old_registered is not None and old_registered != capability["json_schema"]:
        _blocked(
            "CONFIGURATION_SCHEMA_DRIFT",
            "Current schema differs from its historical registry entry.",
            config_type=type_row.type_key,
        )
    schemas[type_row.schema_version] = copy.deepcopy(capability["json_schema"])
    existing_new = schemas.get(schema_version)
    if existing_new is not None and existing_new != schema:
        _blocked(
            "CONFIGURATION_SCHEMA_VERSION_CONFLICT",
            "Requested schema version already identifies a different contract.",
            config_type=type_row.type_key,
            schema_version=schema_version,
        )
    schemas[schema_version] = copy.deepcopy(dict(schema))
    capability.update(
        {
            "json_schema": copy.deepcopy(dict(schema)),
            "schema_versions": schemas,
            "write_enabled": True,
            "read_only": False,
            "managed": True,
            "schema_is_closed": True,
            "schema_evolution": {
                "contract": SCHEMA_EVOLUTION_CONTRACT,
                "current_schema_version": schema_version,
                "historical_schema_preserved": True,
                "registry_digest": _digest(schemas),
            },
        }
    )
    return capability


def _metric_payload(definition: RequiredMetricDefinition) -> dict[str, str]:
    return {
        "metric_key": definition.metric_key,
        "name_zh": definition.name_zh,
        "unit": definition.unit,
        "data_source": definition.data_source,
    }


def _schema_shape_value(value: Any) -> Any:
    """Replace topological plan references with their persisted integer shape."""

    if isinstance(value, Mapping):
        return {key: _schema_shape_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_schema_shape_value(item) for item in value]
    if isinstance(value, tuple):
        return [_schema_shape_value(item) for item in value]
    if isinstance(value, str) and value.startswith("$"):
        return 1
    return value


def _metric_version_matches(
    item: MetricDefinitionVersion,
    definition: RequiredMetricDefinition,
) -> bool:
    return (
        item.name_zh == definition.name_zh
        and item.unit == definition.unit
        and item.data_source == definition.data_source
        and item.available_aggregations == ["identity"]
        and item.display_metadata.get("contract") == METRIC_REGISTRATION_CONTRACT
        and item.display_metadata.get("historical_values_recalculated") is False
    )


def _validated(db: Session, version_id: int) -> bool:
    version = db.get(ConfigurationVersion, version_id)
    return version is not None and version.lifecycle_status == "VALIDATED"


def _digest(value: Any) -> str:
    serialized = json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _blocked(code: str, message: str, **context: Any) -> None:
    raise StrategyPlatformReadError(code, message, context=context)
