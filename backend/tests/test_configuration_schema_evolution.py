from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.strategy_platform_errors import StrategyPlatformReadError
from app.models import (
    Base,
    ConfigurationActivation,
    ConfigurationDependency,
    ConfigurationType,
    ConfigurationVersion,
)
from app.schemas.strategy_platform import ConfigurationDraftCreateRequest
from app.services.configuration_management import ConfigurationManagementService
from app.services.configuration_resolver import ConfigurationResolverService


NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)
SCOPE = {"scope_type": "WORKFLOW", "scope_key": "production-research-v13"}


def _schema(*, current: bool) -> dict:
    properties = {"value": {"type": "integer", "minimum": 1}}
    if current:
        properties["label"] = {"type": "string", "minLength": 1}
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _type(
    type_key: str,
    *,
    handler_key: str = "generic-json-v1",
) -> ConfigurationType:
    v1 = _schema(current=False)
    v2 = _schema(current=True)
    return ConfigurationType(
        type_key=type_key,
        name_zh=type_key,
        description_zh=f"{type_key} schema-evolution test",
        schema_version="v2",
        handler_key=handler_key,
        editor_capability={
            "write_enabled": True,
            "read_only": False,
            "json_schema": v2,
            "schema_versions": {"v1": v1, "v2": v2},
        },
        enabled=True,
    )


def _type_with_schema(type_key: str, schema: dict) -> ConfigurationType:
    return ConfigurationType(
        type_key=type_key,
        name_zh=type_key,
        description_zh=f"{type_key} anyOf test",
        schema_version="v2",
        handler_key="strategy-platform-closed-json-schema-v1",
        editor_capability={
            "write_enabled": True,
            "read_only": False,
            "json_schema": schema,
            "schema_versions": {"v2": schema},
        },
        enabled=True,
    )


def _version(
    type_key: str,
    number: int,
    *,
    schema_version: str,
    payload: dict,
    status: str = "VALIDATED",
) -> ConfigurationVersion:
    return ConfigurationVersion(
        type_key=type_key,
        version_number=number,
        lifecycle_status=status,
        payload_json=payload,
        schema_version=schema_version,
        config_digest=(str(number % 10) or "a") * 64,
        change_summary="schema evolution fixture",
        created_by="test-owner",
        created_at=NOW,
        validated_at=NOW if status != "DRAFT" else None,
    )


@pytest.fixture()
def db() -> Session:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_v1_history_uses_its_own_schema_after_type_advances_to_v2(db: Session) -> None:
    db.add(_type("metric-family"))
    old = _version(
        "metric-family",
        1,
        schema_version="v1",
        payload={"value": 1},
    )
    db.add(old)
    db.flush()

    resolver = ConfigurationResolverService(db)
    resolver._validate_version(old, allow_historical=True)
    resolver._validate_version(old)

    old.payload_json = {"value": 1, "label": "not-valid-under-v1"}
    with pytest.raises(StrategyPlatformReadError) as exc_info:
        resolver._validate_version(old, allow_historical=True)
    assert exc_info.value.code == "CONFIGURATION_PAYLOAD_SCHEMA_INVALID"


def test_create_draft_uses_current_schema_and_records_current_version(
    db: Session,
) -> None:
    db.add(_type("generation-profile"))
    db.flush()
    service = ConfigurationManagementService(db, actor="test-owner")

    created = service.create_draft(
        config_type="generation-profile",
        request=ConfigurationDraftCreateRequest(
            **SCOPE,
            change_summary="create exact v2 draft",
            payload_json={"value": 2, "label": "v2"},
            dependencies=[],
        ),
        request_id="schema-evolution-create-v2",
    )
    assert created.version.schema_version == "v2"

    with pytest.raises(StrategyPlatformReadError) as exc_info:
        service.create_draft(
            config_type="generation-profile",
            request=ConfigurationDraftCreateRequest(
                **SCOPE,
                change_summary="v1 payload cannot become an implicit v2 draft",
                payload_json={"value": 2},
                dependencies=[],
            ),
            request_id="schema-evolution-reject-v1-shape",
        )
    assert exc_info.value.code == "CONFIGURATION_PAYLOAD_SCHEMA_INVALID"


def test_unknown_and_drifted_schema_registries_fail_closed(db: Session) -> None:
    type_row = _type("quality-profile")
    db.add(type_row)
    unknown = _version(
        "quality-profile",
        1,
        schema_version="v3",
        payload={"value": 1, "label": "unknown"},
    )
    db.add(unknown)
    db.flush()

    resolver = ConfigurationResolverService(db)
    with pytest.raises(StrategyPlatformReadError) as exc_info:
        resolver._validate_version(unknown, allow_historical=True)
    assert exc_info.value.code == "CONFIGURATION_SCHEMA_UNKNOWN"

    capability = dict(type_row.editor_capability)
    capability["json_schema"] = _schema(current=False)
    type_row.editor_capability = capability
    with pytest.raises(StrategyPlatformReadError) as exc_info:
        ConfigurationManagementService(db)._require_type(
            "quality-profile", require_manageable=True
        )
    assert exc_info.value.code == "CONFIGURATION_SCHEMA_DRIFT"


def test_owner_write_handler_allowlist_is_strict(db: Session) -> None:
    installed = _type(
        "installed-closed-schema",
        handler_key="strategy-platform-closed-json-schema-v1",
    )
    unknown = _type("unknown-handler", handler_key="user-selected-handler")
    db.add_all((installed, unknown))
    db.flush()

    service = ConfigurationManagementService(db)
    assert (
        service._require_type("installed-closed-schema", require_manageable=True)
        is installed
    )
    with pytest.raises(StrategyPlatformReadError) as exc_info:
        service._require_type("unknown-handler", require_manageable=True)
    assert exc_info.value.code == "CONFIGURATION_HANDLER_UNAVAILABLE"


def test_heterogeneous_array_anyof_is_strictly_validated(db: Session) -> None:
    schema = {
        "type": "object",
        "properties": {
            "members": {
                "type": "array",
                "items": {
                    "anyOf": [
                        {
                            "type": "object",
                            "properties": {"metric": {"type": "string"}},
                            "required": ["metric"],
                            "additionalProperties": False,
                        },
                        {
                            "type": "object",
                            "properties": {"threshold": {"type": "number"}},
                            "required": ["threshold"],
                            "additionalProperties": False,
                        },
                    ]
                },
            }
        },
        "required": ["members"],
        "additionalProperties": False,
    }
    db.add(_type_with_schema("heterogeneous-family", schema))
    db.flush()
    service = ConfigurationManagementService(db)

    created = service.create_draft(
        config_type="heterogeneous-family",
        request=ConfigurationDraftCreateRequest(
            **SCOPE,
            change_summary="strict heterogeneous members",
            payload_json={
                "members": [{"metric": "profit"}, {"threshold": 0.5}]
            },
            dependencies=[],
        ),
        request_id="schema-anyof-heterogeneous",
    )
    assert created.version.schema_version == "v2"

    with pytest.raises(StrategyPlatformReadError) as exc_info:
        service.create_draft(
            config_type="heterogeneous-family",
            request=ConfigurationDraftCreateRequest(
                **SCOPE,
                change_summary="unknown union member must fail",
                payload_json={"members": [{"fallback": True}]},
                dependencies=[],
            ),
            request_id="schema-anyof-heterogeneous-invalid",
        )
    assert exc_info.value.code == "CONFIGURATION_PAYLOAD_SCHEMA_INVALID"


def test_top_level_anyof_is_supported_without_open_schema_escape(db: Session) -> None:
    schema = {
        "anyOf": [
            {
                "type": "object",
                "properties": {"quality": {"type": "number"}},
                "required": ["quality"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {"score": {"type": "number"}},
                "required": ["score"],
                "additionalProperties": False,
            },
        ]
    }
    db.add(_type_with_schema("top-level-union", schema))
    db.flush()
    service = ConfigurationManagementService(db)
    created = service.create_draft(
        config_type="top-level-union",
        request=ConfigurationDraftCreateRequest(
            **SCOPE,
            change_summary="existing top-level scoring contract",
            payload_json={"score": 0.75},
            dependencies=[],
        ),
        request_id="schema-anyof-top-level",
    )
    assert created.version.schema_version == "v2"

    invalid_schema = dict(schema)
    invalid_schema["type"] = "object"
    db.add(_type_with_schema("ambiguous-open-union", invalid_schema))
    db.flush()
    with pytest.raises(StrategyPlatformReadError) as exc_info:
        service._require_type("ambiguous-open-union", require_manageable=True)
    assert exc_info.value.code == "CONFIGURATION_SCHEMA_INVALID"


def test_read_only_dependency_is_resolvable_but_cannot_be_lifecycle_root(
    db: Session,
) -> None:
    root_type = _type("writable-root")
    dependency_type = _type("read-only-provider", handler_key="read-only-provider-v1")
    dependency_capability = dict(dependency_type.editor_capability)
    dependency_capability["write_enabled"] = False
    dependency_capability["read_only"] = True
    dependency_type.editor_capability = dependency_capability
    db.add_all((root_type, dependency_type))
    dependency = _version(
        "read-only-provider",
        1,
        schema_version="v1",
        payload={"value": 1},
    )
    root = _version(
        "writable-root",
        1,
        schema_version="v2",
        payload={"value": 2, "label": "owner writable root"},
        status="DRAFT",
    )
    db.add_all((dependency, root))
    db.flush()
    db.add(
        ConfigurationDependency(
            configuration_version_id=root.id,
            depends_on_version_id=dependency.id,
            relation_key="provider:exact",
        )
    )
    db.flush()

    service = ConfigurationManagementService(db)
    graph = service._validate_graph(root=root, allow_draft_root=True)
    assert set(graph) == {root.id, dependency.id}

    with pytest.raises(StrategyPlatformReadError) as exc_info:
        service._validate_graph(root=dependency, allow_draft_root=False)
    assert exc_info.value.code == "CONFIGURATION_HANDLER_UNAVAILABLE"




def test_graph_allows_multiple_exact_family_versions_and_keeps_cycle_guard(
    db: Session,
) -> None:
    db.add_all((_type("research-profile"), _type("metric-family")))
    family_v1 = _version(
        "metric-family",
        1,
        schema_version="v1",
        payload={"value": 1},
    )
    family_v2 = _version(
        "metric-family",
        2,
        schema_version="v2",
        payload={"value": 2, "label": "second exact member"},
    )
    root = _version(
        "research-profile",
        1,
        schema_version="v2",
        payload={"value": 3, "label": "aggregate"},
        status="DRAFT",
    )
    db.add_all((family_v1, family_v2, root))
    db.flush()
    db.add_all(
        (
            ConfigurationDependency(
                configuration_version_id=root.id,
                depends_on_version_id=family_v1.id,
                relation_key="metric:first",
            ),
            ConfigurationDependency(
                configuration_version_id=root.id,
                depends_on_version_id=family_v2.id,
                relation_key="metric:second",
            ),
            ConfigurationActivation(
                config_type="metric-family",
                version_id=family_v1.id,
                activated_by="test-owner",
                **SCOPE,
            ),
        )
    )
    db.flush()

    service = ConfigurationManagementService(db)
    graph = service._validate_graph(root=root, allow_draft_root=True)
    assert set(graph) == {root.id, family_v1.id, family_v2.id}
    service._require_scope_compatibility(
        graph=graph,
        root_version_id=root.id,
        **SCOPE,
    )

    db.add(
        ConfigurationDependency(
            configuration_version_id=family_v2.id,
            depends_on_version_id=root.id,
            relation_key="cycle-back-to-root",
        )
    )
    db.flush()
    with pytest.raises(StrategyPlatformReadError) as exc_info:
        service._validate_graph(root=root, allow_draft_root=True)
    assert exc_info.value.code == "CONFIGURATION_DEPENDENCY_CYCLE"
