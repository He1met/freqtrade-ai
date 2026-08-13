from __future__ import annotations

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.models.base import Base
from app.models.strategy_platform import (
    ConfigurationType,
    ConfigurationVersion,
    MetricDefinition,
)
from app.models.strategy_platform_extensions import AdapterDefinition
from app.core.strategy_platform_errors import StrategyPlatformReadError
from app.services.configuration_resolver import (
    validate_configuration_payload_for_type,
)
from app.services.owner_research_activation import (
    ExistingResearchBindings,
    build_owner_research_activation_plan,
)
from app.services.owner_research_activation_prerequisites import (
    REQUIRED_V2_METRICS,
    ensure_required_v2_metric_versions,
    evolve_activation_configuration_schemas,
    reconcile_installed_adapter_registry,
    _schema_shape_value,
)
from app.services.strategy_platform_adapter_registry import (
    INSTALLED_ADAPTER_MANIFEST,
    installed_adapter_manifest_digest,
)


METRICS = (
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


def _schema(field: str) -> dict:
    return {
        "type": "object",
        "required": [field],
        "properties": {field: {"type": "string"}},
        "additionalProperties": False,
    }


def _capability(schema: dict) -> dict:
    return {
        "json_schema": schema,
        "schema_is_closed": True,
        "managed": True,
        "safety": {
            "demo_only": True,
            "allow_real_funds": False,
            "single_writer_required": True,
        },
    }


def _plan():
    return build_owner_research_activation_plan(
        ExistingResearchBindings(
            provider_model_config_version_id=10,
            research_target_config_set_id=11,
            validation_window_config_set_id=12,
            market_data_policy_version_id=13,
            evidence_freshness_profile_version_id=14,
            scheduler_profile_version_id=15,
            worker_execution_profile_version_id=16,
            strategy_family_version_ids=(31, 32),
            metric_version_ids={key: index for index, key in enumerate(METRICS, 101)},
        ),
        candidates_per_target=3,
        target_count=2,
        candidate_count=6,
    )


def test_schema_evolution_preserves_v1_and_installs_exact_v2() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    plan = _plan()
    with Session(engine) as db:
        for configuration in plan.configurations:
            db.add(
                ConfigurationType(
                    type_key=configuration.type_key,
                    name_zh=configuration.type_key,
                    description_zh="test",
                    schema_version="1",
                    handler_key="strategy-platform-closed-json-schema-v1",
                    editor_capability=_capability(_schema("legacy")),
                    enabled=True,
                )
            )
        db.flush()
        evolve_activation_configuration_schemas(db, plan, actor="owner:test")

        for configuration in plan.configurations:
            row = db.get(ConfigurationType, configuration.type_key)
            assert row is not None
            assert row.schema_version == configuration.schema_version
            assert set(row.editor_capability["schema_versions"]) == {
                "1",
                configuration.schema_version,
            }
            assert row.editor_capability["write_enabled"] is True
            validate_configuration_payload_for_type(
                row,
                {"legacy": "preserved"},
                schema_version="1",
                version_id=1,
            )
            validate_configuration_payload_for_type(
                row,
                    _schema_shape_value(dict(configuration.payload)),
                schema_version=configuration.schema_version,
                version_id=2,
            )


def test_required_metric_registration_is_validated_and_idempotent() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    metric_schema = {
        "type": "object",
        "required": ["data_source", "metric_key", "name_zh", "unit"],
        "properties": {
            "data_source": {"type": "string"},
            "metric_key": {"type": "string"},
            "name_zh": {"type": "string"},
            "unit": {"type": "string"},
        },
        "additionalProperties": False,
    }
    with Session(engine) as db:
        db.add(
            ConfigurationType(
                type_key="metric-definition",
                name_zh="指标定义",
                description_zh="test",
                schema_version="1",
                handler_key="strategy-platform-closed-json-schema-v1",
                editor_capability=_capability(metric_schema),
                enabled=True,
            )
        )
        db.flush()
        first = ensure_required_v2_metric_versions(
            db,
            actor="owner:test",
            scope_type="WORKFLOW",
            scope_key="production-research-v13",
            request_prefix="issue-709:test",
        )
        second = ensure_required_v2_metric_versions(
            db,
            actor="owner:test",
            scope_type="WORKFLOW",
            scope_key="production-research-v13",
            request_prefix="issue-709:test",
        )

        assert first == second
        assert set(first) == {item.metric_key for item in REQUIRED_V2_METRICS}
        assert db.scalar(select(func.count(MetricDefinition.id))) == len(
            REQUIRED_V2_METRICS
        )
        assert db.scalar(
            select(func.count(ConfigurationVersion.id)).where(
                ConfigurationVersion.lifecycle_status == "VALIDATED"
            )
        ) == len(REQUIRED_V2_METRICS)


def test_adapter_reconciliation_only_rebinds_manifest_metadata() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    adapter = INSTALLED_ADAPTER_MANIFEST[0]
    with Session(engine) as db:
        db.add(
            AdapterDefinition(
                adapter_key=adapter.adapter_key,
                adapter_kind=adapter.adapter_kind,
                implementation_version=adapter.implementation_version,
                input_schema_version=adapter.input_schema_version,
                output_schema_version=adapter.output_schema_version,
                capabilities=dict(adapter.capabilities),
                display_metadata={
                    "input_schema": adapter.input_schema,
                    "output_schema": adapter.output_schema,
                    "source_ref": adapter.source_ref,
                    "source_sha256": adapter.source_sha256,
                    "installed_manifest_digest": "0" * 64,
                },
                enabled=True,
                registry_metadata_only=True,
                contains_secret_material=False,
                contains_executable_payload=False,
            )
        )
        db.flush()
        reconcile_installed_adapter_registry(db)
        assert db.scalar(select(func.count(AdapterDefinition.adapter_key))) == len(
            INSTALLED_ADAPTER_MANIFEST
        )
        assert (
            db.get(AdapterDefinition, adapter.adapter_key).display_metadata[
                "installed_manifest_digest"
            ]
            == installed_adapter_manifest_digest()
        )
        db.get(AdapterDefinition, adapter.adapter_key).implementation_version = "drift"
        db.flush()
        with pytest.raises(StrategyPlatformReadError) as exc_info:
            reconcile_installed_adapter_registry(db)
        assert exc_info.value.code == "ADAPTER_REGISTRY_IMPLEMENTATION_DRIFT"
