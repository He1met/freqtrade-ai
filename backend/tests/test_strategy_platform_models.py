from __future__ import annotations

import inspect as pyinspect
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.migrations import (
    SCHEMA_VERSION,
    STRATEGY_PLATFORM_V1_BASE_VERSION,
    STRATEGY_PLATFORM_V1_TABLES,
    _add_strategy_platform_v1_foundation,
)
from app.models import (
    Base,
    ConfigurationActivation,
    ConfigurationType,
    ConfigurationVersion,
    ExecutionTargetDefinition,
    ExecutionTargetDefinitionVersion,
)


def _sqlite_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_v13_foundation_metadata_is_forward_only_and_complete() -> None:
    assert STRATEGY_PLATFORM_V1_BASE_VERSION == "20260811_45"
    assert SCHEMA_VERSION == "20260813_46"
    assert set(STRATEGY_PLATFORM_V1_TABLES).issubset(Base.metadata.tables)

    plan_columns = Base.metadata.tables["strategy_validation_plans"].c
    assert {
        "strategy_target_id",
        "quality_gate_profile_version_id",
        "validation_window_config_set_id",
        "configuration_bundle_snapshot_id",
        "cycle_number",
        "trigger_source_key",
        "trigger_metadata",
        "policy_snapshot_digest",
        "market_data_snapshot_digest",
    }.issubset(plan_columns.keys())

    window = Base.metadata.tables["strategy_validation_windows"]
    assert window.c.window_kind.nullable is True
    assert {
        "window_config_id",
        "window_key_snapshot",
        "name_zh_snapshot",
        "description_zh_snapshot",
        "attempt_number",
        "net_profit_after_cost",
        "max_drawdown",
        "volatility",
        "total_trades",
        "failure_code",
        "failure_message",
    }.issubset(window.c.keys())
    unique_sets = {
        frozenset(column.name for column in constraint.columns)
        for constraint in window.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert (
        frozenset({"validation_plan_id", "window_config_id", "attempt_number"})
        in unique_sets
    )
    assert frozenset({"validation_plan_id", "ordinal"}) not in unique_sets


def test_configuration_lifecycle_scope_and_demo_safety_constraints() -> None:
    db = _sqlite_session()
    db.add(
        ConfigurationType(
            type_key="execution-target",
            name_zh="执行目标",
            description_zh="测试执行目标定义",
            schema_version="v1",
            handler_key="execution-target-v1",
            editor_capability={},
            enabled=True,
        )
    )
    db.commit()

    db.add(
        ConfigurationVersion(
            type_key="execution-target",
            version_number=1,
            lifecycle_status="VALIDATED",
            payload_json={},
            schema_version="v1",
            config_digest="a" * 64,
            created_by="test",
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    first = ConfigurationVersion(
        type_key="execution-target",
        version_number=1,
        lifecycle_status="DRAFT",
        payload_json={},
        schema_version="v1",
        config_digest="a" * 64,
        created_by="test",
    )
    second = ConfigurationVersion(
        type_key="execution-target",
        version_number=2,
        lifecycle_status="DRAFT",
        payload_json={},
        schema_version="v1",
        config_digest="b" * 64,
        created_by="test",
    )
    db.add_all((first, second))
    db.commit()

    db.add_all(
        (
            ConfigurationActivation(
                config_type="execution-target",
                scope_type="research",
                scope_key="production-research",
                version_id=first.id,
                activated_by="test",
            ),
            ConfigurationActivation(
                config_type="execution-target",
                scope_type="research",
                scope_key="production-research",
                version_id=second.id,
                activated_by="test",
            ),
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    target = ExecutionTargetDefinition(target_key="paper-target")
    db.add(target)
    db.flush()
    db.add(
        ExecutionTargetDefinitionVersion(
            configuration_version_id=first.id,
            execution_target_definition_id=target.id,
            name_zh="测试目标",
            description_zh="不得连接真实资金",
            scope_kind="NON_EXCHANGE",
            writer_policy={},
            enabled=True,
            demo_only=True,
            allow_real_funds=True,
            single_writer_required=True,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_postgresql_foundation_has_immutable_bundle_and_legal_state_guards() -> None:
    source = pyinspect.getsource(_add_strategy_platform_v1_foundation)
    for fragment in (
        "configuration versions must start as DRAFT",
        "activation requires matching VALIDATED version",
        "configuration dependency cycle is forbidden",
        "validated configuration dependencies are immutable",
        "configuration dependencies must be VALIDATED",
        "bundle resolved version/digest mismatch",
        "bundle safety capability snapshot is incomplete",
        '"configuration_bundle_snapshots"',
        "{table_name}_immutable",
        "validated configuration children are immutable",
        "illegal strategy validation plan transition",
        "illegal strategy validation window transition",
        "terminal strategy validation plan is immutable",
        "terminal strategy validation window is immutable",
        "configuration cannot weaken Demo-only writer safety",
        "configuration payload cannot contain secret values",
        "configuration payload cannot contain executable code",
        "ADD COLUMN IF NOT EXISTS configuration_bundle_snapshot_id",
        "ALTER COLUMN window_kind DROP NOT NULL",
    ):
        assert fragment in source
    assert "DROP TABLE" not in source
    assert "DELETE FROM" not in source


def test_fresh_sqlite_metadata_builds_without_fixed_window_enums() -> None:
    db = _sqlite_session()
    inspector = inspect(db.get_bind())
    assert set(STRATEGY_PLATFORM_V1_TABLES).issubset(inspector.get_table_names())

    window_checks = " ".join(
        str(item["sqltext"])
        for item in inspector.get_check_constraints("validation_window_configs")
    )
    for fixed_key in ("wf_bull", "wf_range", "wf_bear", "oos"):
        assert fixed_key not in window_checks

    now = datetime.now(timezone.utc)
    assert now.tzinfo is not None
