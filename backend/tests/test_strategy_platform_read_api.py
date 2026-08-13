from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from app.core.strategy_platform_errors import StrategyPlatformReadError
from app.db.session import get_db
from app.main import app
from app.models import (
    Base,
    ConfigurationActivation,
    ConfigurationBundleSnapshot,
    ConfigurationDependency,
    ConfigurationType,
    ConfigurationVersion,
    ExecutionTargetDefinition,
    Strategy,
    StrategyEvaluationSummary,
    StrategyTarget,
    StrategyValidationPlan,
    StrategyValidationWindow,
    StrategyVersion,
    ValidationWindowConfig,
    ValidationWindowConfigSet,
    ValidationWindowPurpose,
    ValidationWindowScore,
)
from app.services.configuration_resolver import ConfigurationResolverService
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

NOW = datetime(2026, 8, 13, 0, 0, tzinfo=timezone.utc)
OWNER_HEADERS = {"X-Operator-Token": "synthetic-test-operator-token"}


@pytest.fixture()
def db() -> Session:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = Session(engine)
    _seed_configuration(session)
    _seed_strategy_projection(session)
    yield session
    session.close()
    engine.dispose()


@pytest.fixture()
def client(db: Session):
    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _configuration_type(type_key: str, name: str) -> ConfigurationType:
    return ConfigurationType(
        type_key=type_key,
        name_zh=name,
        description_zh=f"{name} contract",
        schema_version="v1",
        handler_key="generic-json-v1",
        editor_capability={"read_only": True},
        enabled=True,
    )


def _configuration_version(
    type_key: str, version_number: int, digest_char: str, payload: dict
) -> ConfigurationVersion:
    return ConfigurationVersion(
        type_key=type_key,
        version_number=version_number,
        lifecycle_status="VALIDATED",
        payload_json=payload,
        schema_version="v1",
        config_digest=digest_char * 64,
        created_by="test-owner",
        created_at=NOW,
        validated_at=NOW,
    )


def _seed_configuration(db: Session) -> None:
    db.add_all(
        (
            _configuration_type("research-profile", "研究装配"),
            _configuration_type("research-targets", "研究目标"),
            _configuration_type("validation-windows", "验证窗口"),
        )
    )
    root = _configuration_version(
        "research-profile",
        1,
        "a",
        {"profile": "production", "candidate_count": 60},
    )
    targets = _configuration_version(
        "research-targets",
        1,
        "b",
        {"targets": [{"pair": "BTC/USDT:USDT", "timeframe": "5m"}]},
    )
    windows = _configuration_version(
        "validation-windows",
        1,
        "c",
        {"window_count": 2},
    )
    db.add_all((root, targets, windows))
    db.flush()
    db.add_all(
        (
            ConfigurationDependency(
                configuration_version_id=root.id,
                depends_on_version_id=targets.id,
                relation_key="targets",
            ),
            ConfigurationDependency(
                configuration_version_id=root.id,
                depends_on_version_id=windows.id,
                relation_key="validation-windows",
            ),
            ConfigurationActivation(
                config_type="research-profile",
                scope_type="research",
                scope_key="production-research",
                version_id=root.id,
                activated_at=NOW,
                activated_by="test-owner",
            ),
        )
    )
    db.add(
        Strategy(
            name="Archived Catalog Fixture",
            slug="archived-catalog-fixture",
            description=None,
            status="archived",
            source="imported",
            tags=[],
            created_at=NOW - timedelta(days=1),
            updated_at=NOW - timedelta(days=1),
        )
    )
    db.commit()


def _seed_strategy_projection(db: Session) -> None:
    strategy = Strategy(
        name="Dynamic Windows Strategy",
        slug="dynamic-windows-strategy",
        description="read projection fixture",
        status="active",
        source="manual",
        tags=["v1.3"],
        created_at=NOW,
        updated_at=NOW,
    )
    db.add(strategy)
    db.flush()
    version = StrategyVersion(
        strategy_id=strategy.id,
        version_number=1,
        blueprint={"family": "fixture"},
        generated_code="class Fixture: pass",
        file_path="user_data/strategies/DynamicWindowsStrategy.py",
        validation_status="passed",
        validation_errors=[],
        diff_snapshot={},
        created_at=NOW,
    )
    db.add(version)
    db.flush()
    strategy.current_version_id = version.id
    execution_target = ExecutionTargetDefinition(target_key="OKX_DEMO", created_at=NOW)
    db.add(execution_target)
    db.flush()
    target = StrategyTarget(
        strategy_version_id=version.id,
        execution_target_id=execution_target.id,
        instrument_id="BTC-USDT-SWAP",
        pair="BTC/USDT:USDT",
        timeframe="5m",
        status="ENABLED",
        validation_priority=20,
        created_at=NOW,
        updated_at=NOW,
    )
    db.add(target)
    db.flush()

    window_version = db.scalar(
        select(ConfigurationVersion).where(
            ConfigurationVersion.type_key == "validation-windows"
        )
    )
    assert window_version is not None
    db.add(
        ValidationWindowConfigSet(
            id=window_version.id,
            name="动态窗口配置",
            default_classifier_adapter_key="window-close-return-v1",
            default_classifier_parameters={},
        )
    )
    db.add(
        ValidationWindowPurpose(
            config_set_id=window_version.id,
            key="admission",
            name_zh="准入验证",
            description_zh="动态准入验证用途",
            counts_for_qualification=True,
            enabled=True,
            sort_order=10,
        )
    )
    db.flush()
    high_volatility = ValidationWindowConfig(
        config_set_id=window_version.id,
        pair=target.pair,
        timeframe=target.timeframe,
        data_kind="futures",
        window_key="stress_high_volatility",
        purpose_key="admission",
        ordinal=15,
        name_zh="高波动压力窗口",
        description_zh="不依赖固定 bull/range/bear key",
        start_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        end_at=datetime(2025, 2, 1, tzinfo=timezone.utc),
        classifier_parameters={},
        required=True,
        classification_evidence={},
    )
    liquidity = ValidationWindowConfig(
        config_set_id=window_version.id,
        pair=target.pair,
        timeframe=target.timeframe,
        data_kind="futures",
        window_key="liquidity_drought",
        purpose_key="admission",
        ordinal=35,
        name_zh="低流动性窗口",
        description_zh="任意新增窗口无需 DTO 字段",
        start_at=datetime(2025, 2, 1, tzinfo=timezone.utc),
        end_at=datetime(2025, 3, 1, tzinfo=timezone.utc),
        classifier_parameters={},
        required=True,
        classification_evidence={},
    )
    db.add_all((high_volatility, liquidity))
    db.flush()
    plan = StrategyValidationPlan(
        strategy_version_id=version.id,
        strategy_target_id=target.id,
        validation_window_config_set_id=window_version.id,
        cycle_number=1,
        trigger_source_key="manual",
        trigger_metadata={},
        promotion_backtest_result_id=999,
        provider_name="test",
        strategy_code_digest="d" * 64,
        plan_digest="e" * 64,
        plan_snapshot={},
        status="REJECTED",
        promotion_evidence={},
        created_at=NOW,
        completed_at=NOW,
    )
    db.add(plan)
    db.flush()
    first = StrategyValidationWindow(
        validation_plan_id=plan.id,
        window_config_id=high_volatility.id,
        ordinal=high_volatility.ordinal,
        window_key_snapshot=high_volatility.window_key,
        name_zh_snapshot=high_volatility.name_zh,
        description_zh_snapshot=high_volatility.description_zh,
        attempt_number=1,
        timerange="20250101-20250201",
        profile_snapshot={},
        expected_market_data_digest="f" * 64,
        net_profit_after_cost=0.04,
        max_drawdown=0.08,
        total_trades=44,
        status="PASSED",
        created_at=NOW,
    )
    second = StrategyValidationWindow(
        validation_plan_id=plan.id,
        window_config_id=liquidity.id,
        ordinal=liquidity.ordinal,
        window_key_snapshot=liquidity.window_key,
        name_zh_snapshot=liquidity.name_zh,
        description_zh_snapshot=liquidity.description_zh,
        attempt_number=1,
        timerange="20250201-20250301",
        profile_snapshot={},
        expected_market_data_digest="1" * 64,
        net_profit_after_cost=-0.01,
        max_drawdown=0.12,
        total_trades=17,
        status="REJECTED",
        failure_code="INSUFFICIENT_TRADES",
        failure_message="实际 17 笔，未达到配置门槛",
        created_at=NOW,
    )
    db.add_all((first, second))
    db.flush()
    db.add_all(
        (
            ValidationWindowScore(
                validation_window_id=first.id,
                scoring_version="dynamic-v1",
                profile_version_id=window_version.id,
                total_score=82.5,
                component_scores_snapshot={},
                metrics_snapshot={},
                score_digest="2" * 64,
                created_at=NOW,
            ),
            ValidationWindowScore(
                validation_window_id=second.id,
                scoring_version="dynamic-v1",
                profile_version_id=window_version.id,
                total_score=43.0,
                component_scores_snapshot={},
                metrics_snapshot={},
                score_digest="3" * 64,
                created_at=NOW,
            ),
            StrategyEvaluationSummary(
                validation_plan_id=plan.id,
                required_window_count=2,
                passed_window_count=1,
                failed_window_count=1,
                overall_score=62.75,
                status="REJECTED",
                primary_failure_window_config_id=liquidity.id,
                reason_codes=["INSUFFICIENT_TRADES"],
                summary_digest="4" * 64,
                created_at=NOW,
            ),
        )
    )
    db.commit()


def test_resolver_materializes_idempotent_immutable_snapshot(db: Session) -> None:
    service = ConfigurationResolverService(db)
    resolution = service.resolve_active(
        workflow_kind="research",
        aggregate_config_type="research-profile",
        scope_type="research",
        scope_key="production-research",
    )

    assert resolution.persisted is False
    assert resolution.snapshot_id is None
    assert {
        key.rsplit(":", 1)[0]
        for key in resolution.resolved_versions_json
    } == {"research-profile", "research-targets", "validation-windows"}
    assert len(resolution.bundle_digest) == 64
    assert resolution.capability_snapshot["demo_only"] is True
    assert resolution.capability_snapshot["allow_real_funds"] is False
    assert resolution.capability_snapshot["single_writer_required"] is True

    first = service.materialize_bundle(resolution)
    second = service.materialize_bundle(resolution)
    db.commit()

    assert first.snapshot_id == second.snapshot_id
    assert db.scalar(select(func.count(ConfigurationBundleSnapshot.id))) == 1
    stored = service.read_bundle(first.snapshot_id)
    assert stored.bundle_digest == resolution.bundle_digest
    assert stored.resolved_versions_json == resolution.resolved_versions_json


def test_resolver_fails_closed_for_missing_scope_and_dependency_cycle(
    db: Session,
) -> None:
    service = ConfigurationResolverService(db)
    with pytest.raises(StrategyPlatformReadError) as missing:
        service.resolve_active(
            workflow_kind="research",
            aggregate_config_type="research-profile",
            scope_type="research",
            scope_key="design-lab",
        )
    assert missing.value.code == "ACTIVE_CONFIGURATION_NOT_FOUND"

    root = db.scalar(
        select(ConfigurationVersion).where(
            ConfigurationVersion.type_key == "research-profile"
        )
    )
    child = db.scalar(
        select(ConfigurationVersion).where(
            ConfigurationVersion.type_key == "research-targets"
        )
    )
    assert root is not None and child is not None
    db.add(
        ConfigurationDependency(
            configuration_version_id=child.id,
            depends_on_version_id=root.id,
            relation_key="cycle-fixture",
        )
    )
    db.commit()
    with pytest.raises(StrategyPlatformReadError) as cycle:
        service.resolve_active(
            workflow_kind="research",
            aggregate_config_type="research-profile",
            scope_type="research",
            scope_key="production-research",
        )
    assert cycle.value.code == "CONFIGURATION_DEPENDENCY_CYCLE"


def test_historical_bundle_remains_readable_after_retirement(db: Session) -> None:
    service = ConfigurationResolverService(db)
    resolution = service.resolve_active(
        workflow_kind="research",
        aggregate_config_type="research-profile",
        scope_type="research",
        scope_key="production-research",
    )
    snapshot = service.materialize_bundle(resolution)
    db.commit()

    for version in db.scalars(select(ConfigurationVersion)).all():
        version.lifecycle_status = "RETIRED"
    for type_row in db.scalars(select(ConfigurationType)).all():
        type_row.enabled = False
    db.commit()

    stored = service.read_bundle(snapshot.snapshot_id)
    assert stored.bundle_digest == resolution.bundle_digest
    with pytest.raises(StrategyPlatformReadError) as disabled:
        service.resolve_active(
            workflow_kind="research",
            aggregate_config_type="research-profile",
            scope_type="research",
            scope_key="production-research",
        )
    assert disabled.value.code == "CONFIGURATION_TYPE_DISABLED"


def test_materialize_blocks_when_scope_activation_changes(db: Session) -> None:
    service = ConfigurationResolverService(db)
    original = service.resolve_active(
        workflow_kind="research",
        aggregate_config_type="research-profile",
        scope_type="research",
        scope_key="production-research",
    )
    replacement = _configuration_version(
        "research-profile",
        2,
        "9",
        {"profile": "replacement"},
    )
    db.add(replacement)
    db.flush()
    activation = db.scalar(
        select(ConfigurationActivation).where(
            ConfigurationActivation.config_type == "research-profile"
        )
    )
    assert activation is not None
    activation.version_id = replacement.id
    db.commit()

    with pytest.raises(StrategyPlatformReadError) as stale:
        service.materialize_bundle(original)
    assert stale.value.code == "BUNDLE_RESOLUTION_STALE"
    assert db.scalar(select(func.count(ConfigurationBundleSnapshot.id))) == 0


def test_owner_read_auth_and_configuration_preview_are_read_only(
    client: TestClient, db: Session
) -> None:
    assert client.get("/api/v1/configuration-catalog").status_code == 401
    assert (
        client.get(
            "/api/v1/configuration-catalog",
            headers={"X-Operator-Token": "wrong-token"},
        ).status_code
        == 401
    )

    catalog = client.get("/api/v1/configuration-catalog", headers=OWNER_HEADERS)
    assert catalog.status_code == 200
    assert {item["type_key"] for item in catalog.json()["items"]} == {
        "research-profile",
        "research-targets",
        "validation-windows",
    }

    preview = client.post(
        "/api/v1/configuration-bundles/resolve",
        headers=OWNER_HEADERS,
        json={
            "workflow_kind": "research",
            "aggregate_config_type": "research-profile",
            "scope_type": "research",
            "scope_key": "production-research",
        },
    )
    assert preview.status_code == 200
    assert preview.json()["persisted"] is False
    assert db.scalar(select(func.count(ConfigurationBundleSnapshot.id))) == 0


def test_active_configuration_error_has_stable_code(client: TestClient) -> None:
    response = client.get(
        "/api/v1/configurations/research-profile/active",
        params={"scope_type": "research", "scope_key": "design-lab"},
        headers=OWNER_HEADERS,
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "ACTIVE_CONFIGURATION_NOT_FOUND"
    assert response.json()["detail"]["operation_status"] == "BLOCKED"


def test_catalog_target_and_dynamic_validation_window_projection(
    client: TestClient,
) -> None:
    catalog = client.get(
        "/api/v1/strategy-catalog", headers=OWNER_HEADERS, params={"limit": 1}
    )
    assert catalog.status_code == 200
    item = catalog.json()["items"][0]
    assert item["catalog_status"] == "ACTIVE"
    assert item["current_version"]["static_validation_status"] == "PASSED"
    assert item["target_count"] == 1
    assert item["targets"][0]["execution_target_key"] == "OKX_DEMO"
    assert item["targets"][0]["research_status"] == "REJECTED"
    assert catalog.json()["next_cursor"]

    second_page = client.get(
        "/api/v1/strategy-catalog",
        headers=OWNER_HEADERS,
        params={"limit": 1, "cursor": catalog.json()["next_cursor"]},
    )
    assert second_page.status_code == 200
    assert second_page.json()["items"][0]["slug"] == "archived-catalog-fixture"

    history = client.get(
        f"/api/v1/strategies/{item['id']}/validation-history",
        headers=OWNER_HEADERS,
    )
    assert history.status_code == 200
    cycle = history.json()["cycles"][0]
    assert cycle["required_window_count"] == 2
    assert [window["window_key"] for window in cycle["windows"]] == [
        "stress_high_volatility",
        "liquidity_drought",
    ]
    assert [window["name_zh"] for window in cycle["windows"]] == [
        "高波动压力窗口",
        "低流动性窗口",
    ]
    assert cycle["windows"][1]["failure_reasons"] == [
        {
            "code": "INSUFFICIENT_TRADES",
            "message": "实际 17 笔，未达到配置门槛",
            "quality_gate_rule_id": None,
            "actual_value": None,
            "operator": None,
            "threshold_snapshot": None,
        }
    ]


def test_openapi_exposes_no_configuration_mutation_routes(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    configuration_methods = {
        path: set(methods)
        for path, methods in paths.items()
        if path.startswith("/api/v1/configuration")
    }
    assert configuration_methods["/api/v1/configuration-catalog"] == {"get"}
    assert configuration_methods["/api/v1/configuration-bundles/{bundle_id}"] == {"get"}
    assert configuration_methods["/api/v1/configuration-bundles/resolve"] == {"post"}
    assert all(
        not ({"put", "patch", "delete"} & methods)
        for methods in configuration_methods.values()
    )
