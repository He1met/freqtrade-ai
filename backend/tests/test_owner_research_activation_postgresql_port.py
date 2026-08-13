from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

import app.models  # noqa: F401 - register the complete metadata graph
import app.services.owner_research_activation_postgresql as postgresql_port
from app.core.strategy_platform_errors import StrategyPlatformReadError
from app.models.base import Base
from app.models.strategy_platform import MetricDefinition, MetricDefinitionVersion
from app.models.strategy_platform_extensions import (
    AdapterDefinition,
    DiversityRule,
    GenerationProfileFamily,
    ScoringRule,
)
from app.services.owner_research_activation import (
    ExistingResearchBindings,
    build_owner_research_activation_plan,
)
from app.services.owner_research_activation_postgresql import (
    PostgreSQLOwnerResearchActivationPort,
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


@pytest.fixture()
def sqlite_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
        session.rollback()
    engine.dispose()


def _bindings() -> ExistingResearchBindings:
    return ExistingResearchBindings(
        provider_model_config_version_id=10,
        research_target_config_set_id=11,
        validation_window_config_set_id=12,
        market_data_policy_version_id=13,
        evidence_freshness_profile_version_id=14,
        scheduler_profile_version_id=15,
        worker_execution_profile_version_id=16,
        strategy_family_version_ids=(31, 32, 33),
        metric_version_ids={
            key: version_id for version_id, key in enumerate(METRICS, start=101)
        },
    )


def _resolve(value, ids):
    if isinstance(value, dict):
        return {key: _resolve(item, ids) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_resolve(item, ids) for item in value)
    if isinstance(value, str) and value.startswith("$"):
        return ids[value[1:]]
    return value


def _seed_metrics(session: Session, bindings: ExistingResearchBindings) -> None:
    for key, version_id in bindings.metric_version_ids.items():
        definition = MetricDefinition(metric_key=key)
        session.add(definition)
        session.flush()
        session.add(
            MetricDefinitionVersion(
                configuration_version_id=version_id,
                metric_definition_id=definition.id,
                name_zh=key,
                unit=("boolean" if key in {"all_metrics_missing", "validation_error"} else "ratio"),
                data_source=f"evidence.{key}",
                available_aggregations=["latest"],
                display_metadata={},
            )
        )
    session.flush()


def test_preflight_rejects_non_postgresql_before_any_write() -> None:
    db = MagicMock()
    db.get_bind.return_value.dialect.name = "sqlite"
    port = PostgreSQLOwnerResearchActivationPort(db)

    with pytest.raises(StrategyPlatformReadError) as exc_info:
        port.register_adapter(INSTALLED_ADAPTER_MANIFEST[0])

    assert exc_info.value.code == "ACTIVATION_POSTGRESQL_REQUIRED"
    db.add.assert_not_called()
    db.flush.assert_not_called()


def test_preflight_requires_exact_database_and_non_destructive_v47() -> None:
    db = MagicMock()
    db.get_bind.return_value.dialect.name = "postgresql"
    wrong_database = MagicMock()
    wrong_database.scalar_one.return_value = "freqtrade_ai"
    db.execute.return_value = wrong_database
    port = PostgreSQLOwnerResearchActivationPort(db)

    with pytest.raises(StrategyPlatformReadError) as exc_info:
        port._preflight()
    assert exc_info.value.code == "ACTIVATION_DATABASE_INVALID"

    database = MagicMock()
    database.scalar_one.return_value = "freqtrade_ai_design_lab"
    ownership = MagicMock()
    ownership.mappings.return_value.one.return_value = {
        "current_user": "design_lab_owner",
        "schema_name": "public",
        "table_count": len(postgresql_port._OWNER_WRITE_TABLES),
        "owns_all": True,
    }
    migration = MagicMock()
    migration.mappings.return_value.one.return_value = {
        "target_schema_version": "20260813_47",
        "nonterminal_count": 0,
        "succeeded_count": 1,
        "destructive_write_count": 1,
        "overwritten_row_count": 0,
        "deleted_row_count": 0,
    }
    db.execute.side_effect = [database, ownership, migration]
    port.management.repository.require_owner_connection = MagicMock()
    with pytest.raises(StrategyPlatformReadError) as exc_info:
        port._preflight()
    assert exc_info.value.code == "ACTIVATION_MIGRATION_EVIDENCE_INVALID"


@pytest.mark.parametrize(
    ("nonterminal_count", "succeeded_count"),
    ((1, 1), (0, 0), (0, 2)),
)
def test_preflight_rejects_active_missing_or_ambiguous_migration_runs(
    nonterminal_count: int,
    succeeded_count: int,
) -> None:
    db = MagicMock()
    db.get_bind.return_value.dialect.name = "postgresql"
    database = MagicMock()
    database.scalar_one.return_value = "freqtrade_ai_design_lab"
    ownership = MagicMock()
    ownership.mappings.return_value.one.return_value = {
        "current_user": "design_lab_owner",
        "schema_name": "public",
        "table_count": len(postgresql_port._OWNER_WRITE_TABLES),
        "owns_all": True,
    }
    migration = MagicMock()
    migration.mappings.return_value.one.return_value = {
        "target_schema_version": "20260813_47",
        "nonterminal_count": nonterminal_count,
        "succeeded_count": succeeded_count,
        "destructive_write_count": 0,
        "overwritten_row_count": 0,
        "deleted_row_count": 0,
    }
    db.execute.side_effect = [database, ownership, migration]
    port = PostgreSQLOwnerResearchActivationPort(db)
    port.management.repository.require_owner_connection = MagicMock()

    with pytest.raises(StrategyPlatformReadError) as exc_info:
        port._preflight()

    assert exc_info.value.code == "ACTIVATION_MIGRATION_EVIDENCE_INVALID"
    assert db.execute.call_args_list[-1].args[1]["migration_key"] == (
        postgresql_port._MIGRATION_KEY
    )


def test_register_adapter_is_exact_idempotent_metadata_only(sqlite_session: Session) -> None:
    adapter = next(
        item
        for item in INSTALLED_ADAPTER_MANIFEST
        if item.adapter_key == "diversity-threshold-v2"
    )
    port = PostgreSQLOwnerResearchActivationPort(sqlite_session)
    port._preflight = lambda: None  # type: ignore[method-assign]

    port.register_adapter(adapter)
    port.register_adapter(adapter)
    row = sqlite_session.get(AdapterDefinition, adapter.adapter_key)

    assert row is not None
    assert row.output_schema_version == adapter.output_schema_version
    assert row.display_metadata == {
        "input_schema": adapter.input_schema,
        "output_schema": adapter.output_schema,
        "source_ref": adapter.source_ref,
        "source_sha256": adapter.source_sha256,
        "installed_manifest_digest": installed_adapter_manifest_digest(),
    }
    assert row.registry_metadata_only is True
    assert row.contains_secret_material is False
    assert row.contains_executable_payload is False

    row.display_metadata = {**row.display_metadata, "source_sha256": "0" * 64}
    sqlite_session.flush()
    with pytest.raises(StrategyPlatformReadError) as exc_info:
        port.register_adapter(adapter)
    assert exc_info.value.code == "ACTIVATION_ADAPTER_REGISTRY_CONFLICT"


def test_specialized_profiles_are_complete_and_replay_exact(
    sqlite_session: Session,
) -> None:
    bindings = _bindings()
    _seed_metrics(sqlite_session, bindings)
    plan = build_owner_research_activation_plan(
        bindings,
        candidates_per_target=7,
        target_count=4,
        candidate_count=28,
    )
    port = PostgreSQLOwnerResearchActivationPort(sqlite_session)
    port._preflight = lambda: None  # type: ignore[method-assign]
    port._require_dependencies = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    version_ids: dict[str, int] = {}
    replay = False

    def create_draft(*, config_type, request, request_id):
        plan_key = request_id.split(":")[-2]
        version_id = version_ids.setdefault(plan_key, 1000 + len(version_ids))
        return SimpleNamespace(
            version=SimpleNamespace(
                id=version_id,
                schema_version=next(
                    item.schema_version
                    for item in plan.configurations
                    if item.plan_key == plan_key
                ),
            ),
            dependencies=[
                SimpleNamespace(
                    relation_key=item.relation_key,
                    depends_on_version_id=item.depends_on_version_id,
                )
                for item in request.dependencies or []
            ],
            idempotent_replay=replay,
        )

    port.management.create_draft = create_draft  # type: ignore[method-assign]

    def persist_all() -> None:
        for configuration in plan.configurations:
            payload = _resolve(configuration.payload, version_ids)
            dependencies = tuple(
                (relation, _resolve(reference, version_ids))
                for relation, reference in configuration.dependencies
            )
            roots = tuple(
                _resolve(item, version_ids) for item in configuration.specialized_rows
            )
            port.create_draft(
                configuration,
                resolved_payload=payload,
                resolved_dependencies=dependencies,
                resolved_specialized_rows=roots,
                request_id=f"test:{configuration.plan_key}:draft",
            )

    persist_all()
    sqlite_session.flush()
    assert sqlite_session.scalar(select(func.count()).select_from(GenerationProfileFamily)) == 3
    assert sqlite_session.scalar(select(func.count()).select_from(DiversityRule)) == 2
    assert sqlite_session.scalar(select(func.count()).select_from(ScoringRule)) == 4

    replay = True
    persist_all()
    sqlite_session.flush()
    assert sqlite_session.scalar(select(func.count()).select_from(GenerationProfileFamily)) == 3

    scoring_rule = sqlite_session.scalars(
        select(ScoringRule).order_by(ScoringRule.id)
    ).first()
    assert scoring_rule is not None
    scoring_rule.weight = 0
    sqlite_session.flush()
    with pytest.raises(StrategyPlatformReadError) as exc_info:
        persist_all()
    assert exc_info.value.code == "ACTIVATION_SPECIALIZED_REPLAY_CONFLICT"


def test_materialize_revalidates_scope_profiles_safety_and_digests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = MagicMock()
    port = PostgreSQLOwnerResearchActivationPort(db)
    port._preflight = lambda: None  # type: ignore[method-assign]
    manifest_digest = installed_adapter_manifest_digest()
    bundle_digest = "a" * 64
    port.resolver.resolve_active = MagicMock(
        return_value=SimpleNamespace(
            aggregate_profile_version_id=1004,
            bundle_digest=bundle_digest,
        )
    )
    port.management.repository.find_bundle = MagicMock(return_value=None)
    port.management.repository.acquire_idempotency_lock = MagicMock()
    port.resolver.materialize_bundle = MagicMock(
        return_value=SimpleNamespace(snapshot_id=5000, bundle_digest=bundle_digest)
    )
    snapshot = SimpleNamespace(
        capability_snapshot={
            "adapter_registry_digest": "b" * 64,
            "installed_adapter_manifest_digest": manifest_digest,
        }
    )
    port.resolver.read_bundle = MagicMock(return_value=snapshot)
    profiles = {
        "generation-profile": SimpleNamespace(id=1000, payload_json={}),
        "diversity-profile": SimpleNamespace(id=1001, payload_json={}),
        "quality-gate-profile": SimpleNamespace(
            id=1002,
            payload_json={
                "profile_key": "profile-bound-score-v2-quality",
                "quality_components": [{"component_key": "quality"}],
                "elimination_rules": [{"rule_key": "eliminate"}],
                "warning_rules": [{"rule_key": "warn"}],
            },
        ),
        "scoring-profile": SimpleNamespace(id=1003, payload_json={}),
        "research-profile": SimpleNamespace(
            id=1004,
            payload_json={
                "generation_profile_version_id": 1000,
                "diversity_profile_version_id": 1001,
                "quality_gate_profile_version_id": 1002,
                "scoring_profile_version_id": 1003,
            },
        ),
    }

    class Verified:
        def require_single_version(self, type_key):
            return profiles[type_key]

    monkeypatch.setattr(
        postgresql_port,
        "validate_frozen_configuration_bundle",
        lambda *_args, **_kwargs: Verified(),
    )
    generation = SimpleNamespace(version_id=1000, family_version_ids=(31, 32))
    diversity = SimpleNamespace(
        generation_profile_version_id=1000,
        required_family_version_ids=(31, 32),
    )
    scoring = SimpleNamespace(
        quality_components=({"component_key": "quality"},),
        elimination_rules=({"rule_key": "eliminate"},),
        warning_rules=({"rule_key": "warn"},),
    )
    monkeypatch.setattr(postgresql_port, "generation_profile", lambda _: generation)
    monkeypatch.setattr(postgresql_port, "diversity_profile", lambda _: diversity)
    monkeypatch.setattr(postgresql_port, "scoring_profile", lambda _: scoring)

    result = port.materialize_bundle(
        workflow_kind="RESEARCH",
        scope_type="WORKFLOW",
        scope_key="production-research-v13",
        aggregate_version_id=1004,
        installed_adapter_manifest_digest=manifest_digest,
        request_id="issue-707:test:bundle",
    )

    assert result == (5000, bundle_digest, False)
    port.management.repository.acquire_idempotency_lock.assert_called_once_with(
        "issue-707:test:bundle"
    )
    db.commit.assert_not_called()
