from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import inspect as python_inspect
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

from app.canonical_v13 import api as canonical_api
from app.canonical_v13.api import API_PREFIX, create_canonical_v13_app
from app.canonical_v13.genesis import install_canonical_genesis
from app.canonical_v13.control_plane import (
    ConfigurationDependencyInput,
    create_configuration_draft,
    validate_configuration_version,
)
from app.canonical_v13.manifest import (
    CANONICAL_BUSINESS_SCHEMA,
    P0_CONFIGURATION_KINDS,
)
from app.canonical_v13.market import (
    MarketInspectionFacts,
    accept_market_artifact,
    create_market_profile_draft,
    seal_market_snapshot,
    validate_market_profile,
)
from app.canonical_v13.models import (
    AUDIT_EVENTS_TABLE,
    CONFIGURATION_ACTIVATIONS_TABLE,
    CONFIGURATION_BUNDLES_TABLE,
    CONFIGURATION_PROFILES_TABLE,
    CONFIGURATION_SNAPSHOTS_TABLE,
    CONFIGURATION_VERSIONS_TABLE,
    DEPLOYMENTS_TABLE,
    IDEMPOTENCY_RECEIPTS_TABLE,
    OPTIMIZATION_RUNS_TABLE,
    ORDERS_TABLE,
    RUNTIME_INSTANCES_TABLE,
    RESEARCH_TARGETS_TABLE,
    SIGNALS_TABLE,
    STRATEGIES_TABLE,
    VALIDATION_ATTEMPTS_TABLE,
)


_HEX = "a" * 64
_MANIFEST_DIGEST = "b" * 64
_NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)


def _client():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as connection:
        install_canonical_genesis(connection, installer_identity="canonical-api-test")

    @contextmanager
    def connection_factory():
        with engine.connect() as connection:
            yield connection

    app = create_canonical_v13_app(
        reader_connection_factory=connection_factory,
        control_connection_factory=connection_factory,
    )
    return engine, TestClient(app, raise_server_exceptions=False)


def _submission_payload(*, idempotency_key: str = "submit-1") -> dict[str, object]:
    content = b"class CanonicalStrategy:\n    pass\n"
    return {
        "caller_identity": "canonical-api-test",
        "idempotency_key": idempotency_key,
        "display_name": "Canonical Strategy",
        "archive_snapshot_digest": sha256(b"archive").hexdigest(),
        "source_entry_key": "strategies/canonical.py",
        "source_strategy_key": "external-strategy-1",
        "current_version_id": "external-version-2",
        "versions": [
            {
                "source_strategy_key": "external-strategy-1",
                "version_id": "external-version-1",
                "version_number": 1,
                "artifact_base64": base64.b64encode(b"old\n").decode("ascii"),
            },
            {
                "source_strategy_key": "external-strategy-1",
                "version_id": "external-version-2",
                "version_number": 2,
                "artifact_base64": base64.b64encode(content).decode("ascii"),
            },
        ],
    }


def _draft_payload(kind: str) -> dict[str, object]:
    payload_by_kind: dict[str, dict[str, object]] = {
        "DIVERSITY": {
            "rules": [
                {
                    "rule_key": "family-diversity",
                    "algorithm": "pairwise-correlation-v1",
                    "metric": "return_correlation",
                    "operator": "<=",
                    "threshold": 0.8,
                }
            ]
        },
        "QUALITY_QUALIFICATION": {
            "minimum_score": 50,
            "required_window_gates": [
                {
                    "gate_key": "positive-return",
                    "metric": "net_return_after_cost",
                    "operator": ">",
                    "threshold": 0,
                }
            ],
        },
        "SCORING": {
            "window_aggregation": "MEAN",
            "components": [
                {
                    "component_key": "profit",
                    "metric": "net_return_after_cost",
                    "weight": 1,
                    "direction": "maximize",
                    "minimum": -1,
                    "maximum": 1,
                }
            ]
        },
        "RESEARCH_AGGREGATE": {"assembly_key": "production-research-v13"},
    }
    return {
        "actor_identity": "canonical-p0-operator",
        "idempotency_key": f"draft-{kind.lower()}",
        "profile_key": f"profile-{kind.lower()}",
        "scope_key": "production-research-v13",
        "workflow_key": "research",
        "schema_json": {"type": "object", "additionalProperties": False},
        "payload_json": payload_by_kind[kind],
        "adapter_identity": f"adapter-{kind.lower()}",
        "adapter_digest": _HEX,
        "dependencies": [],
    }


def _count(engine, table) -> int:
    with engine.connect() as raw:
        connection = raw.execution_options(
            schema_translate_map={CANONICAL_BUSINESS_SCHEMA: None}
        )
        return int(
            connection.execute(select(func.count()).select_from(table)).scalar_one()
        )


def _seed_ready_bundle(engine) -> dict[str, object]:
    """Create only immutable control-plane/market evidence for API activation tests."""

    schema = {"type": "object", "additionalProperties": False}

    with engine.begin() as raw:
        connection = raw.execution_options(
            schema_translate_map={CANONICAL_BUSINESS_SCHEMA: None}
        )

        def draft(kind: str, payload: dict[str, object], *, dependencies=()):
            return create_configuration_draft(
                connection,
                profile_key=f"api-ready-{kind.lower()}",
                configuration_kind=kind,
                scope_key="production-research-v13",
                workflow_key="research",
                schema_json=schema,
                payload_json=payload,
                adapter_identity=f"api-{kind.lower()}-adapter-v1",
                adapter_digest=_HEX,
                dependencies=dependencies,
            )

        def dependency(item, kind: str) -> ConfigurationDependencyInput:
            return ConfigurationDependencyInput(
                version_id=item.version_id,
                expected_kind=kind,
                relation_key=f"snapshot:{kind.lower()}",
            )

        target = draft(
            "TARGET",
            {
                "targets": [
                    {
                        "target_key": "api-btc-5m",
                        "instrument": "BTC-USDT-SWAP",
                        "pair": "BTC/USDT:USDT",
                        "timeframe": "5m",
                        "data_kind": "futures",
                    }
                ]
            },
        )
        window = draft(
            "WINDOW",
            {
                "windows": [
                    {
                        "window_key": "api-required",
                        "required": True,
                        "start_at": _NOW.isoformat(),
                        "end_at": (_NOW + timedelta(hours=6)).isoformat(),
                        "coverage": {"minimum_closed_candles": 72},
                    }
                ]
            },
        )
        generation = draft(
            "GENERATION",
            {
                "allocations": [
                    {
                        "target_key": "api-btc-5m",
                        "allocation_count": 2,
                        "candidate_cap": 3,
                    }
                ]
            },
            dependencies=(dependency(target, "TARGET"),),
        )
        diversity = draft("DIVERSITY", _draft_payload("DIVERSITY")["payload_json"])
        quality = draft(
            "QUALITY_QUALIFICATION",
            _draft_payload("QUALITY_QUALIFICATION")["payload_json"],
        )
        scoring = draft("SCORING", _draft_payload("SCORING")["payload_json"])
        inputs = {
            "TARGET": target,
            "WINDOW": window,
            "GENERATION": generation,
            "DIVERSITY": diversity,
            "QUALITY_QUALIFICATION": quality,
            "SCORING": scoring,
        }
        snapshots = {
            kind: validate_configuration_version(
                connection,
                version_id=item.version_id,
                adapter_manifest_digest=_MANIFEST_DIGEST,
            )
            for kind, item in inputs.items()
        }
        aggregate = draft(
            "RESEARCH_AGGREGATE",
            _draft_payload("RESEARCH_AGGREGATE")["payload_json"],
            dependencies=tuple(
                dependency(inputs[kind], kind) for kind in inputs
            ),
        )
        snapshots["RESEARCH_AGGREGATE"] = validate_configuration_version(
            connection,
            version_id=aggregate.version_id,
            adapter_manifest_digest=_MANIFEST_DIGEST,
        )
        snapshot_ids = {
            kind: snapshots[kind].snapshot_id for kind in P0_CONFIGURATION_KINDS
        }
        target_id = connection.execute(
            select(RESEARCH_TARGETS_TABLE.c.id).where(
                RESEARCH_TARGETS_TABLE.c.target_snapshot_id
                == snapshots["TARGET"].snapshot_id
            )
        ).scalar_one()
        _profile_id, market_version_id, _digest = create_market_profile_draft(
            connection,
            profile_key="api-ready-market",
            scope_key="production-research-v13",
            payload={"source": "isolated-fixture", "network_access": "NONE"},
        )
        validate_market_profile(connection, version_id=market_version_id)
        evidence = accept_market_artifact(
            connection,
            locator="fixtures/api-BTC-USDT-SWAP-5m.parquet",
            content=b"isolated canonical API market fixture",
            media_type="application/x-parquet",
            inspector_identity="canonical-api-fixture-inspector-v1",
            facts=MarketInspectionFacts(
                row_count=72,
                first_open_at=_NOW,
                last_close_at=_NOW + timedelta(hours=6),
                gap_count=0,
                duplicate_count=0,
                null_count=0,
                monotonic=True,
            ),
        )
        market_snapshot = seal_market_snapshot(
            connection,
            market_profile_version_id=market_version_id,
            members=(
                (
                    evidence.artifact_id,
                    evidence.receipt_id,
                    target_id,
                    _NOW,
                    _NOW + timedelta(hours=6),
                ),
            ),
        )
    return {
        "scope_key": "production-research-v13",
        "workflow_key": "research",
        "snapshot_ids": {kind: str(value) for kind, value in snapshot_ids.items()},
        "market_snapshot_id": str(market_snapshot.snapshot_id),
    }


def test_factory_is_standalone_and_exact_routes_are_frozen() -> None:
    source = python_inspect.getsource(canonical_api)
    assert "from app.main" not in source
    assert "import app.main" not in source
    assert "from app.db.session" not in source

    engine, client = _client()
    try:
        route_contract = {
            (route.path, method)
            for route in client.app.routes
            for method in getattr(route, "methods", set())
        }
        expected = {
            (f"{API_PREFIX}/submissions", "POST"),
            (f"{API_PREFIX}/strategies", "GET"),
            (f"{API_PREFIX}/strategies/{{strategy_id}}", "GET"),
            (f"{API_PREFIX}/configurations", "GET"),
            (f"{API_PREFIX}/configurations/{{kind}}/drafts", "POST"),
            (
                f"{API_PREFIX}/configurations/{{kind}}/{{version_id}}/validate",
                "POST",
            ),
            (f"{API_PREFIX}/research-bundles/preview", "POST"),
            (f"{API_PREFIX}/research-bundles/{{bundle_id}}/activate", "POST"),
            (f"{API_PREFIX}/market-data", "GET"),
            (f"{API_PREFIX}/market-data/snapshots/{{snapshot_id}}", "GET"),
            (f"{API_PREFIX}/readiness/research", "GET"),
            (f"{API_PREFIX}/readiness/runtime", "GET"),
            (f"{API_PREFIX}/optimizations", "GET"),
        }
        canonical_routes = {
            item for item in route_contract if item[0].startswith(API_PREFIX)
        }
        assert canonical_routes == expected
        assert all(not path.startswith("/api/v1") for path, _method in route_contract)
        schema = client.app.openapi()
        operation_ids = [
            operation["operationId"]
            for path in schema["paths"].values()
            for method, operation in path.items()
            if method in {"get", "post", "put", "patch", "delete"}
        ]
        assert len(operation_ids) == 13
        assert len(set(operation_ids)) == len(operation_ids)
    finally:
        client.close()
        engine.dispose()


def test_reader_and_control_factories_are_never_crossed() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as connection:
        install_canonical_genesis(connection, installer_identity="api-role-test")
    opens = {"reader": 0, "control": 0}

    @contextmanager
    def reader_factory():
        opens["reader"] += 1
        with engine.connect() as connection:
            yield connection

    @contextmanager
    def control_factory():
        opens["control"] += 1
        with engine.connect() as connection:
            yield connection

    client = TestClient(
        create_canonical_v13_app(
            reader_connection_factory=reader_factory,
            control_connection_factory=control_factory,
        ),
        raise_server_exceptions=False,
    )
    try:
        assert client.get(f"{API_PREFIX}/strategies").status_code == 200
        assert opens == {"reader": 1, "control": 0}
        assert client.post(
            f"{API_PREFIX}/submissions", json=_submission_payload()
        ).status_code == 201
        assert opens == {"reader": 1, "control": 1}
    finally:
        client.close()
        engine.dispose()


def test_true_empty_projections_are_stable_and_never_fake_ready() -> None:
    engine, client = _client()
    try:
        assert client.get(f"{API_PREFIX}/strategies").json() == {
            "status": "EMPTY",
            "items": [],
        }
        configurations = client.get(f"{API_PREFIX}/configurations").json()
        assert configurations["status"] == "UNSET"
        assert configurations["items"] == []
        assert len(configurations["unset_kinds"]) == 7

        market = client.get(f"{API_PREFIX}/market-data").json()
        assert market == {
            "status": "MARKET_SNAPSHOT_UNSET",
            "profile_count": 0,
            "validated_profile_count": 0,
            "artifact_count": 0,
            "accepted_receipt_count": 0,
            "snapshots": [],
        }
        research = client.get(f"{API_PREFIX}/readiness/research").json()
        assert research["status"] == "BLOCKED"
        assert research["reason_codes"] == ["RESEARCH_BUNDLE_UNSET"]
        assert research["target_count"] is None
        assert research["total_candidate_count"] is None

        runtime = client.get(f"{API_PREFIX}/readiness/runtime").json()
        assert runtime["status"] == "BLOCKED"
        assert runtime["reason_codes"] == [
            "TRADING_DISABLED",
            "ACTIVE_DEPLOYMENT_UNSET",
        ]
        optimizations = client.get(f"{API_PREFIX}/optimizations").json()
        assert optimizations == {"status": "PENDING_FIRST_BACKTEST", "items": []}
        assert "READY" not in str(
            {"research": research, "runtime": runtime, "market": market}
        )
        assert "LEGACY_INCOMPLETE" not in str(
            {"research": research, "runtime": runtime, "market": market}
        )
    finally:
        client.close()
        engine.dispose()


def test_submission_write_and_read_projection_distinguish_all_status_layers() -> None:
    engine, client = _client()
    try:
        first = client.post(f"{API_PREFIX}/submissions", json=_submission_payload())
        assert first.status_code == 201
        receipt = first.json()
        assert receipt["intake_status"] == "INTAKE_ACCEPTED"
        assert receipt["catalog_status"] == "DRAFT"
        assert receipt["validation_status"] == "UNVALIDATED"
        assert receipt["qualification_status"] == "NOT_EVALUATED"
        assert receipt["execution_authorized"] is False
        assert receipt["idempotent_replay"] is False

        replay = client.post(f"{API_PREFIX}/submissions", json=_submission_payload())
        assert replay.status_code == 201
        assert replay.json()["strategy_id"] == receipt["strategy_id"]
        assert replay.json()["idempotent_replay"] is True

        catalog = client.get(f"{API_PREFIX}/strategies").json()
        assert catalog["status"] == "AVAILABLE"
        assert len(catalog["items"]) == 1
        item = catalog["items"][0]
        assert item["intake_status"] == "INTAKE_ACCEPTED"
        assert item["validation_status"] == "UNVALIDATED"
        assert item["qualification_status"] == "NOT_EVALUATED"
        assert item["execution_authorized"] is False

        detail = client.get(
            f"{API_PREFIX}/strategies/{receipt['strategy_id']}"
        ).json()
        assert detail == item
        assert _count(engine, STRATEGIES_TABLE) == 1
        assert _count(engine, VALIDATION_ATTEMPTS_TABLE) == 0
        assert _count(engine, SIGNALS_TABLE) == 0
        assert _count(engine, ORDERS_TABLE) == 0
    finally:
        client.close()
        engine.dispose()


def test_invalid_submission_is_stable_blocked_and_atomic_noop() -> None:
    engine, client = _client()
    try:
        payload = _submission_payload()
        payload["versions"][1]["artifact_base64"] = "not base64!"
        response = client.post(f"{API_PREFIX}/submissions", json=payload)
        assert response.status_code == 422
        assert response.json() == {
            "status": "BLOCKED",
            "error": {
                "code": "BLOCKED_INVALID_COMMAND_DTO",
                "detail": "artifact_base64 must be canonical base64",
            },
        }
        assert _count(engine, STRATEGIES_TABLE) == 0

        malformed = client.post(f"{API_PREFIX}/submissions", json={})
        assert malformed.status_code == 422
        assert malformed.json()["error"]["code"] == "BLOCKED_INVALID_COMMAND_DTO"
        assert _count(engine, STRATEGIES_TABLE) == 0
    finally:
        client.close()
        engine.dispose()


def test_database_uniqueness_race_is_a_stable_redacted_conflict(monkeypatch) -> None:
    engine, client = _client()

    def concurrent_conflict(*_args, **_kwargs):
        raise IntegrityError(
            "private database statement",
            {"private": "parameter"},
            RuntimeError("private constraint detail"),
        )

    monkeypatch.setattr(canonical_api, "controlled_submit_latest", concurrent_conflict)
    try:
        response = client.post(f"{API_PREFIX}/submissions", json=_submission_payload())
        assert response.status_code == 409
        assert response.json() == {
            "status": "BLOCKED",
            "error": {
                "code": "BLOCKED_CANONICAL_CONCURRENT_CONFLICT",
                "detail": "canonical state changed concurrently; re-read before retrying",
            },
        }
        assert "private" not in response.text
        assert _count(engine, STRATEGIES_TABLE) == 0
        recovered = client.get(f"{API_PREFIX}/strategies")
        assert recovered.status_code == 200
        assert recovered.json() == {"status": "EMPTY", "items": []}
    finally:
        client.close()
        engine.dispose()


def test_config_command_and_projection_use_kind_specific_contract() -> None:
    engine, client = _client()
    try:
        draft = client.post(
            f"{API_PREFIX}/configurations/DIVERSITY/drafts",
            json=_draft_payload("DIVERSITY"),
        )
        assert draft.status_code == 201, draft.text
        draft_body = draft.json()
        assert draft_body["configuration_kind"] == "DIVERSITY"
        assert draft_body["lifecycle_status"] == "DRAFT"
        assert draft_body["idempotent_replay"] is False
        draft_replay = client.post(
            f"{API_PREFIX}/configurations/DIVERSITY/drafts",
            json=_draft_payload("DIVERSITY"),
        )
        assert draft_replay.status_code == 201
        assert draft_replay.json()["version_id"] == draft_body["version_id"]
        assert draft_replay.json()["receipt_digest"] == draft_body["receipt_digest"]
        assert draft_replay.json()["idempotent_replay"] is True
        drifted_command = _draft_payload("DIVERSITY")
        drifted_command["profile_key"] = "another-profile"
        drifted = client.post(
            f"{API_PREFIX}/configurations/DIVERSITY/drafts",
            json=drifted_command,
        )
        assert drifted.status_code == 409
        assert drifted.json()["error"]["code"] == "BLOCKED_IDEMPOTENCY_KEY_REUSE"

        wrong_route = client.post(
            f"{API_PREFIX}/configurations/SCORING/{draft_body['version_id']}/validate",
            json={
                "actor_identity": "canonical-p0-operator",
                "idempotency_key": "validate-diversity-wrong-route",
                "adapter_manifest_digest": "b" * 64,
            },
        )
        assert wrong_route.status_code == 409
        assert wrong_route.json()["error"]["code"] == (
            "BLOCKED_CONFIGURATION_KIND_MISMATCH"
        )
        rolled_back = client.get(f"{API_PREFIX}/configurations").json()
        rolled_back_version = rolled_back["items"][0]["versions"][0]
        assert rolled_back_version["lifecycle_status"] == "DRAFT"
        assert rolled_back_version["snapshot_id"] is None
        assert _count(engine, CONFIGURATION_SNAPSHOTS_TABLE) == 0

        validated = client.post(
            f"{API_PREFIX}/configurations/DIVERSITY/{draft_body['version_id']}/validate",
            json={
                "actor_identity": "canonical-p0-operator",
                "idempotency_key": "validate-diversity-v1",
                "adapter_manifest_digest": "b" * 64,
            },
        )
        assert validated.status_code == 200, validated.text
        assert validated.json()["lifecycle_status"] == "VALIDATED"
        assert validated.json()["idempotent_replay"] is False
        replay = client.post(
            f"{API_PREFIX}/configurations/DIVERSITY/{draft_body['version_id']}/validate",
            json={
                "actor_identity": "canonical-p0-operator",
                "idempotency_key": "validate-diversity-v1",
                "adapter_manifest_digest": "b" * 64,
            },
        )
        assert replay.status_code == 200
        assert replay.json()["snapshot_id"] == validated.json()["snapshot_id"]
        assert replay.json()["receipt_digest"] == validated.json()["receipt_digest"]
        assert replay.json()["idempotent_replay"] is True

        catalog = client.get(f"{API_PREFIX}/configurations").json()
        assert catalog["status"] == "AVAILABLE"
        assert catalog["configured_kinds"] == ["DIVERSITY"]
        version = catalog["items"][0]["versions"][0]
        assert version["schema_json"] == {
            "type": "object",
            "additionalProperties": False,
        }
        assert version["payload_json"] == _draft_payload("DIVERSITY")["payload_json"]
        assert version["lifecycle_status"] == "VALIDATED"
        assert version["snapshot_id"] == validated.json()["snapshot_id"]

        assert _count(engine, CONFIGURATION_PROFILES_TABLE) == 1
        assert _count(engine, CONFIGURATION_VERSIONS_TABLE) == 1
        assert _count(engine, CONFIGURATION_SNAPSHOTS_TABLE) == 1
        assert _count(engine, IDEMPOTENCY_RECEIPTS_TABLE) == 2
        assert _count(engine, AUDIT_EVENTS_TABLE) == 2
    finally:
        client.close()
        engine.dispose()


def test_arbitrary_generic_payload_cannot_validate_or_fake_readiness() -> None:
    engine, client = _client()
    try:
        payload = _draft_payload("SCORING")
        payload["payload_json"] = {"members": [{"anything": "goes"}]}
        draft = client.post(
            f"{API_PREFIX}/configurations/SCORING/drafts", json=payload
        )
        assert draft.status_code == 201
        validate = client.post(
            f"{API_PREFIX}/configurations/SCORING/{draft.json()['version_id']}/validate",
            json={
                "actor_identity": "canonical-p0-operator",
                "idempotency_key": "validate-invalid-scoring",
                "adapter_manifest_digest": "b" * 64,
            },
        )
        assert validate.status_code == 422
        assert validate.json()["status"] == "BLOCKED"
        assert _count(engine, CONFIGURATION_BUNDLES_TABLE) == 0
        readiness = client.get(f"{API_PREFIX}/readiness/research").json()
        assert readiness["status"] == "BLOCKED"
    finally:
        client.close()
        engine.dispose()


def test_bundle_preview_blocked_and_path_identity_drift_is_noop() -> None:
    engine, client = _client()
    try:
        command = {
            "scope_key": "production-research-v13",
            "workflow_key": "research",
            "snapshot_ids": {},
            "market_snapshot_id": None,
        }
        preview = client.post(
            f"{API_PREFIX}/research-bundles/preview", json=command
        )
        assert preview.status_code == 200
        body = preview.json()
        assert body["status"] == "BLOCKED"
        assert "TARGET_SNAPSHOT_UNSET" in body["reason_codes"]
        assert "MARKET_SNAPSHOT_UNSET" in body["reason_codes"]
        assert body["bundle_digest"] is None
        assert body["prospective_bundle_id"] is None

        activation = client.post(
            f"{API_PREFIX}/research-bundles/{uuid4()}/activate",
            json={
                **command,
                "actor_identity": "canonical-control-writer",
                "expected_bundle_digest": "c" * 64,
            },
        )
        assert activation.status_code == 409
        assert activation.json()["status"] == "BLOCKED"
        assert _count(engine, CONFIGURATION_BUNDLES_TABLE) == 0
        assert _count(engine, CONFIGURATION_ACTIVATIONS_TABLE) == 0
        assert _count(engine, VALIDATION_ATTEMPTS_TABLE) == 0
        assert _count(engine, SIGNALS_TABLE) == 0
        assert _count(engine, ORDERS_TABLE) == 0
    finally:
        client.close()
        engine.dispose()


def test_ready_bundle_path_id_drift_rolls_back_then_exact_id_activates() -> None:
    engine, client = _client()
    try:
        command = _seed_ready_bundle(engine)
        preview = client.post(
            f"{API_PREFIX}/research-bundles/preview", json=command
        )
        assert preview.status_code == 200, preview.text
        body = preview.json()
        assert body["status"] == "READY"
        assert body["bundle_digest"]
        prospective_bundle_id = body["prospective_bundle_id"]
        assert prospective_bundle_id

        activation_command = {
            **command,
            "actor_identity": "canonical-control-writer",
            "expected_bundle_digest": body["bundle_digest"],
        }
        drift = client.post(
            f"{API_PREFIX}/research-bundles/{uuid4()}/activate",
            json=activation_command,
        )
        assert drift.status_code == 409
        assert drift.json()["error"]["code"] == "BLOCKED_BUNDLE_ID_DRIFT"
        assert _count(engine, CONFIGURATION_BUNDLES_TABLE) == 0
        assert _count(engine, CONFIGURATION_ACTIVATIONS_TABLE) == 0

        exact = client.post(
            f"{API_PREFIX}/research-bundles/{prospective_bundle_id}/activate",
            json=activation_command,
        )
        assert exact.status_code == 200, exact.text
        assert exact.json()["configuration_bundle_id"] == prospective_bundle_id
        assert exact.json()["created_bundle"] is True
        assert _count(engine, CONFIGURATION_BUNDLES_TABLE) == 1
        assert _count(engine, CONFIGURATION_ACTIVATIONS_TABLE) == 1
        assert _count(engine, VALIDATION_ATTEMPTS_TABLE) == 0
        assert _count(engine, SIGNALS_TABLE) == 0
        assert _count(engine, ORDERS_TABLE) == 0
        readiness = client.get(
            f"{API_PREFIX}/readiness/research",
            params={
                "scope_key": command["scope_key"],
                "workflow_key": command["workflow_key"],
            },
        )
        assert readiness.status_code == 200
        assert readiness.json()["status"] == "PENDING_FIRST_BACKTEST"
        assert readiness.json()["reason_codes"] == ["PENDING_FIRST_BACKTEST"]
    finally:
        client.close()
        engine.dispose()


def test_wrong_database_identity_and_missing_resources_fail_closed() -> None:
    engine, client = _client()
    try:
        missing = client.get(f"{API_PREFIX}/strategies/{uuid4()}")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "BLOCKED_STRATEGY_NOT_FOUND"
        missing_market = client.get(
            f"{API_PREFIX}/market-data/snapshots/{uuid4()}"
        )
        assert missing_market.status_code == 404
        assert missing_market.json()["error"]["code"] == (
            "BLOCKED_MARKET_SNAPSHOT_NOT_FOUND"
        )
    finally:
        client.close()
        engine.dispose()

    wrong_engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @contextmanager
    def wrong_factory():
        with wrong_engine.connect() as connection:
            yield connection

    wrong_client = TestClient(
        create_canonical_v13_app(
            reader_connection_factory=wrong_factory,
            control_connection_factory=wrong_factory,
        ),
        raise_server_exceptions=False,
    )
    try:
        response = wrong_client.get(f"{API_PREFIX}/strategies")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == (
            "BLOCKED_WRONG_CANONICAL_DATABASE"
        )
    finally:
        wrong_client.close()
        wrong_engine.dispose()


def test_api_does_not_expose_execution_actions_or_create_execution_rows() -> None:
    engine, client = _client()
    try:
        for path in (
            f"{API_PREFIX}/backtests",
            f"{API_PREFIX}/runtime/start",
            f"{API_PREFIX}/signals",
            f"{API_PREFIX}/orders",
        ):
            assert client.post(path, json={}).status_code == 404
        assert _count(engine, VALIDATION_ATTEMPTS_TABLE) == 0
        assert _count(engine, OPTIMIZATION_RUNS_TABLE) == 0
        assert _count(engine, DEPLOYMENTS_TABLE) == 0
        assert _count(engine, RUNTIME_INSTANCES_TABLE) == 0
        assert _count(engine, SIGNALS_TABLE) == 0
        assert _count(engine, ORDERS_TABLE) == 0
    finally:
        client.close()
        engine.dispose()
