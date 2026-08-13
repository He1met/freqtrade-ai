from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.session import get_db
from app.main import app
from app.models import (
    Base,
    ConfigurationActivation,
    ConfigurationAuditEvent,
    ConfigurationDependency,
    ConfigurationType,
    ConfigurationVersion,
)
from app.services.operator_authorization import operator_request_coordinator

NOW = datetime(2026, 8, 13, 2, 30, tzinfo=timezone.utc)
SCOPE = {"scope_type": "research", "scope_key": "production-research"}
OWNER_TOKEN = "synthetic-test-operator-token"


def _schema(*, profile: bool) -> dict:
    properties = (
        {
            "profile": {"type": "string", "minLength": 1},
            "candidate_count": {"type": "integer", "minimum": 1, "maximum": 100},
            "demo_only": {"type": "boolean", "const": True},
        }
        if profile
        else {
            "targets": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "minLength": 1},
            }
        }
    )
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _type(type_key: str, *, profile: bool) -> ConfigurationType:
    return ConfigurationType(
        type_key=type_key,
        name_zh=type_key,
        description_zh=f"{type_key} configuration",
        schema_version="v1",
        handler_key="generic-json-v1",
        editor_capability={
            "write_enabled": True,
            "json_schema": _schema(profile=profile),
            "safety_capability": {
                "demo_only": True,
                "allow_real_funds": False,
                "single_writer_required": True,
            },
        },
        enabled=True,
    )


def _version(
    type_key: str,
    number: int,
    payload: dict,
    digest_char: str,
) -> ConfigurationVersion:
    return ConfigurationVersion(
        type_key=type_key,
        version_number=number,
        lifecycle_status="VALIDATED",
        payload_json=payload,
        schema_version="v1",
        config_digest=digest_char * 64,
        change_summary="seed",
        created_by="seed-owner",
        created_at=NOW,
        validated_at=NOW,
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
    session.add_all(
        (
            _type("research-profile", profile=True),
            _type("research-targets", profile=False),
        )
    )
    targets_v1 = _version(
        "research-targets",
        1,
        {"targets": ["BTC/USDT:USDT"]},
        "b",
    )
    targets_v2 = _version(
        "research-targets",
        2,
        {"targets": ["BTC/USDT:USDT", "ETH/USDT:USDT"]},
        "c",
    )
    root_v1 = _version(
        "research-profile",
        1,
        {"profile": "production", "candidate_count": 60, "demo_only": True},
        "a",
    )
    session.add_all((targets_v1, targets_v2, root_v1))
    session.flush()
    session.add(
        ConfigurationDependency(
            configuration_version_id=root_v1.id,
            depends_on_version_id=targets_v1.id,
            relation_key="targets",
        )
    )
    session.add_all(
        (
            ConfigurationActivation(
                config_type="research-targets",
                scope_type=SCOPE["scope_type"],
                scope_key=SCOPE["scope_key"],
                version_id=targets_v1.id,
                activated_at=NOW,
                activated_by="seed-owner",
            ),
            ConfigurationActivation(
                config_type="research-profile",
                scope_type=SCOPE["scope_type"],
                scope_key=SCOPE["scope_key"],
                version_id=root_v1.id,
                activated_at=NOW,
                activated_by="seed-owner",
            ),
        )
    )
    session.commit()
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


def _headers(request_id: str) -> dict[str, str]:
    return {
        "X-Operator-Token": OWNER_TOKEN,
        "Idempotency-Key": request_id,
    }


def _action(client: TestClient, path: str, request_id: str):
    return client.post(
        path,
        headers=_headers(request_id),
        json={**SCOPE, "reason": request_id},
    )


def test_owner_can_create_validate_switch_and_retire_immutable_versions(
    client: TestClient,
    db: Session,
) -> None:
    targets_v1, targets_v2 = db.scalars(
        select(ConfigurationVersion)
        .where(ConfigurationVersion.type_key == "research-targets")
        .order_by(ConfigurationVersion.version_number)
    ).all()
    root_v1 = db.scalar(
        select(ConfigurationVersion).where(
            ConfigurationVersion.type_key == "research-profile"
        )
    )
    assert root_v1 is not None

    created = client.post(
        "/api/v1/configurations/research-profile/versions",
        headers=_headers("config-create-0001"),
        json={
            **SCOPE,
            "change_summary": "increase controlled candidate capacity",
            "payload_json": {
                "profile": "production",
                "candidate_count": 80,
                "demo_only": True,
            },
            "dependencies": [
                {
                    "depends_on_version_id": targets_v2.id,
                    "relation_key": "targets",
                }
            ],
        },
    )
    assert created.status_code == 201
    draft = created.json()
    assert draft["version"]["lifecycle_status"] == "DRAFT"
    assert draft["version"]["version_number"] == 2
    assert draft["dependencies"][0]["depends_on_version_id"] == targets_v2.id

    draft_copy = client.post(
        "/api/v1/configurations/research-profile/versions",
        headers=_headers("config-copy-draft-block-0001"),
        json={
            **SCOPE,
            "change_summary": "draft history is not an immutable source",
            "source_version_id": draft["version"]["id"],
        },
    )
    assert draft_copy.status_code == 409
    assert draft_copy.json()["detail"]["code"] == "CONFIGURATION_DRAFT_SOURCE_INVALID"

    validated = _action(
        client,
        f"/api/v1/configurations/research-profile/versions/{draft['version']['id']}/validate",
        "config-validate-0001",
    )
    assert validated.status_code == 200
    assert validated.json()["version"]["lifecycle_status"] == "VALIDATED"
    assert validated.json()["validation_bundle"]["capability_snapshot"] == {
        "allow_real_funds": False,
        "demo_only": True,
        "resolution_contract": "strategy-platform-owner-resolver-v1",
        "resolved_type_count": 2,
        "single_writer_required": True,
    }

    conflict = _action(
        client,
        f"/api/v1/configurations/research-profile/versions/{draft['version']['id']}/activate",
        "config-activate-conflict-0001",
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "CONFIGURATION_SCOPE_CONFLICT"
    operator_request_coordinator.reset_for_tests()
    durable_conflict_replay = _action(
        client,
        f"/api/v1/configurations/research-profile/versions/{draft['version']['id']}/activate",
        "config-activate-conflict-0001",
    )
    assert durable_conflict_replay.status_code == 409
    assert durable_conflict_replay.json()["detail"]["code"] == "CONFIGURATION_SCOPE_CONFLICT"
    assert db.scalar(
        select(func.count(ConfigurationAuditEvent.id)).where(
            ConfigurationAuditEvent.request_id == "config-activate-conflict-0001"
        )
    ) == 1

    child_switch = _action(
        client,
        f"/api/v1/configurations/research-targets/versions/{targets_v2.id}/activate",
        "config-child-activate-0001",
    )
    assert child_switch.status_code == 200

    activated = _action(
        client,
        f"/api/v1/configurations/research-profile/versions/{draft['version']['id']}/activate",
        "config-activate-0001",
    )
    assert activated.status_code == 200
    assert activated.json()["previous_active_version_id"] == root_v1.id
    assert activated.json()["active_version_id"] == draft["version"]["id"]

    replay = _action(
        client,
        f"/api/v1/configurations/research-profile/versions/{draft['version']['id']}/activate",
        "config-activate-0001",
    )
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True
    assert db.scalar(
        select(func.count(ConfigurationAuditEvent.id)).where(
            ConfigurationAuditEvent.request_id == "config-activate-0001"
        )
    ) == 1

    retired = _action(
        client,
        f"/api/v1/configurations/research-profile/versions/{root_v1.id}/retire",
        "config-retire-0001",
    )
    assert retired.status_code == 200
    assert retired.json()["version"]["lifecycle_status"] == "RETIRED"

    active_retire = _action(
        client,
        f"/api/v1/configurations/research-profile/versions/{draft['version']['id']}/retire",
        "config-retire-active-0001",
    )
    assert active_retire.status_code == 409
    assert active_retire.json()["detail"]["code"] == "ACTIVE_CONFIGURATION_CANNOT_BE_RETIRED"

    detail = client.get(
        f"/api/v1/configurations/research-profile/versions/{draft['version']['id']}",
        headers={"X-Operator-Token": OWNER_TOKEN},
    )
    assert detail.status_code == 200
    assert detail.json()["dependencies"][0]["depends_on_type"] == "research-targets"

    diff = client.get(
        f"/api/v1/configurations/research-profile/versions/{draft['version']['id']}/diff",
        params={**SCOPE, "against_version_id": root_v1.id},
        headers={"X-Operator-Token": OWNER_TOKEN},
    )
    assert diff.status_code == 200
    paths = {item["path"] for item in diff.json()["items"]}
    assert "$.payload_json.candidate_count" in paths
    assert "$.dependencies[0].depends_on_version_id" in paths

    audit = client.get(
        "/api/v1/configurations/research-profile/audit-events",
        params=SCOPE,
        headers={"X-Operator-Token": OWNER_TOKEN},
    )
    assert audit.status_code == 200
    assert {item["event_type"] for item in audit.json()["items"]} >= {
        "DRAFT_CREATED",
        "VALIDATED",
        "ACTIVATED",
        "ACTIVATION_FAILED",
        "RETIRED",
    }


def test_validation_failure_is_durable_audited_and_idempotent(
    client: TestClient,
    db: Session,
) -> None:
    targets_v2 = db.scalars(
        select(ConfigurationVersion)
        .where(ConfigurationVersion.type_key == "research-targets")
        .order_by(ConfigurationVersion.version_number.desc())
    ).first()
    assert targets_v2 is not None

    child_draft_response = client.post(
        "/api/v1/configurations/research-targets/versions",
        headers=_headers("config-child-draft-0001"),
        json={
            **SCOPE,
            "change_summary": "unvalidated child",
            "source_version_id": targets_v2.id,
        },
    )
    assert child_draft_response.status_code == 201
    child_draft_id = child_draft_response.json()["version"]["id"]

    root_draft_response = client.post(
        "/api/v1/configurations/research-profile/versions",
        headers=_headers("config-root-draft-0001"),
        json={
            **SCOPE,
            "change_summary": "root with unvalidated dependency",
            "payload_json": {
                "profile": "production",
                "candidate_count": 60,
                "demo_only": True,
            },
            "dependencies": [
                {
                    "depends_on_version_id": child_draft_id,
                    "relation_key": "targets",
                }
            ],
        },
    )
    assert root_draft_response.status_code == 201
    root_draft_id = root_draft_response.json()["version"]["id"]

    path = f"/api/v1/configurations/research-profile/versions/{root_draft_id}/validate"
    failed = _action(client, path, "config-validation-failed-0001")
    assert failed.status_code == 409
    assert failed.json()["detail"]["code"] == "CONFIGURATION_DEPENDENCY_NOT_VALIDATED"

    operator_request_coordinator.reset_for_tests()
    replay = _action(client, path, "config-validation-failed-0001")
    assert replay.status_code == 409
    assert replay.json()["detail"]["code"] == "CONFIGURATION_DEPENDENCY_NOT_VALIDATED"
    events = db.scalars(
        select(ConfigurationAuditEvent).where(
            ConfigurationAuditEvent.request_id == "config-validation-failed-0001"
        )
    ).all()
    assert len(events) == 1
    assert events[0].event_type == "VALIDATION_FAILED"


def test_writes_fail_closed_on_auth_schema_safety_and_idempotency_reuse(
    client: TestClient,
    db: Session,
) -> None:
    request = {
        **SCOPE,
        "change_summary": "bad unknown field",
        "payload_json": {
            "profile": "production",
            "candidate_count": 60,
            "demo_only": True,
            "unexpected": 1,
        },
        "dependencies": [],
    }
    missing_idempotency = client.post(
        "/api/v1/configurations/research-profile/versions",
        headers={"X-Operator-Token": OWNER_TOKEN},
        json=request,
    )
    assert missing_idempotency.status_code == 428

    whitespace_scope = client.post(
        "/api/v1/configurations/research-profile/versions",
        headers=_headers("config-whitespace-scope-0001"),
        json={**request, "scope_key": "   "},
    )
    assert whitespace_scope.status_code == 422

    whitespace_dependency = client.post(
        "/api/v1/configurations/research-profile/versions",
        headers=_headers("config-whitespace-dependency-0001"),
        json={
            **request,
            "dependencies": [
                {"depends_on_version_id": 1, "relation_key": "   "}
            ],
        },
    )
    assert whitespace_dependency.status_code == 422

    rejected = client.post(
        "/api/v1/configurations/research-profile/versions",
        headers=_headers("config-schema-block-0001"),
        json=request,
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "CONFIGURATION_PAYLOAD_SCHEMA_INVALID"

    unsafe = client.post(
        "/api/v1/configurations/research-profile/versions",
        headers=_headers("config-safety-block-0001"),
        json={
            **request,
            "change_summary": "attempt unsafe setting",
            "payload_json": {
                "profile": "production",
                "candidate_count": 60,
                "demo_only": False,
            },
        },
    )
    assert unsafe.status_code == 409
    assert unsafe.json()["detail"]["code"] == "CONFIGURATION_SAFETY_INVARIANT_VIOLATION"

    valid = {
        **request,
        "change_summary": "valid first payload",
        "payload_json": {
            "profile": "production",
            "candidate_count": 61,
            "demo_only": True,
        },
    }
    first = client.post(
        "/api/v1/configurations/research-profile/versions",
        headers=_headers("config-idempotency-0001"),
        json=valid,
    )
    assert first.status_code == 201
    mismatch = client.post(
        "/api/v1/configurations/research-profile/versions",
        headers=_headers("config-idempotency-0001"),
        json={
            **valid,
            "payload_json": {**valid["payload_json"], "candidate_count": 62},
        },
    )
    assert mismatch.status_code == 409
    assert db.scalar(
        select(func.count(ConfigurationAuditEvent.id)).where(
            ConfigurationAuditEvent.request_id == "config-idempotency-0001"
        )
    ) == 1
