from __future__ import annotations

from contextlib import contextmanager
import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.pool import StaticPool

from app.canonical_v13.api import API_PREFIX, create_canonical_v13_app
from app.canonical_v13.control_plane import (
    ConfigurationDependencyInput,
    create_configuration_draft,
    validate_configuration_version,
)
from app.canonical_v13.genesis import install_canonical_genesis
from app.canonical_v13.manifest import CANONICAL_BUSINESS_SCHEMA
from app.canonical_v13.models import (
    AUDIT_EVENTS_TABLE,
    CONFIGURATION_ACTIVATIONS_TABLE,
    CONFIGURATION_BUNDLES_TABLE,
    CONFIGURATION_SNAPSHOTS_TABLE,
    CONFIGURATION_VERSIONS_TABLE,
    IDEMPOTENCY_RECEIPTS_TABLE,
    ORDERS_TABLE,
    RUNTIME_INSTANCES_TABLE,
    SIGNALS_TABLE,
    VALIDATION_ATTEMPTS_TABLE,
)


ROOT = Path(__file__).resolve().parents[2]
WINDOW_SCRIPT = ROOT / "scripts" / "canonical_v13_market_window_rollout.py"
P0_SCRIPT = ROOT / "scripts" / "canonical_v13_p0_rollout.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed_initial_p0(engine) -> None:
    p0 = _load(P0_SCRIPT, "canonical_v13_p0_seed")
    with engine.connect() as raw:
        connection = raw.execution_options(
            schema_translate_map={CANONICAL_BUSINESS_SCHEMA: None}
        )
        drafts = {}
        with connection.begin():
            for kind in p0.KINDS:
                dependency_kinds = (
                    ("TARGET",)
                    if kind == "GENERATION"
                    else p0.KINDS[:6]
                    if kind == "RESEARCH_AGGREGATE"
                    else ()
                )
                draft = create_configuration_draft(
                    connection,
                    profile_key=f"production-v13-{kind.lower().replace('_', '-')}",
                    configuration_kind=kind,
                    scope_key=p0.SCOPE,
                    workflow_key=p0.WORKFLOW,
                    schema_json=p0.SCHEMA,
                    payload_json=p0.p0_payloads()[kind],
                    adapter_identity=p0.ADAPTER_IDENTITY,
                    adapter_digest=p0.ADAPTER_DIGEST,
                    dependencies=tuple(
                        ConfigurationDependencyInput(
                            version_id=drafts[item].version_id,
                            expected_kind=item,
                            relation_key=f"snapshot:{item.lower()}",
                        )
                        for item in dependency_kinds
                    ),
                )
                drafts[kind] = draft
                validate_configuration_version(
                    connection,
                    version_id=draft.version_id,
                    adapter_manifest_digest=p0.ADAPTER_MANIFEST_DIGEST,
                )


def _client():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as connection:
        install_canonical_genesis(connection, installer_identity="window-rollout-test")
    _seed_initial_p0(engine)

    @contextmanager
    def factory():
        with engine.connect() as connection:
            yield connection

    client = TestClient(
        create_canonical_v13_app(
            reader_connection_factory=factory,
            control_connection_factory=factory,
        ),
        raise_server_exceptions=False,
    )
    return engine, client


def _count(engine, table) -> int:
    with engine.connect() as raw:
        connection = raw.execution_options(
            schema_translate_map={CANONICAL_BUSINESS_SCHEMA: None}
        )
        return int(
            connection.execute(select(func.count()).select_from(table)).scalar_one()
        )


def test_plan_requires_explicit_aligned_utc_end_and_has_no_execution_capability() -> None:
    rollout = _load(WINDOW_SCRIPT, "canonical_v13_window_plan")
    plan = rollout.plan("2026-08-14T10:45:00+00:00")
    window = plan["window_payload"]["windows"][0]
    assert window == {
        "window_key": "required-recent-30d",
        "required": True,
        "start_at": "2026-07-15T10:45:00Z",
        "end_at": "2026-08-14T10:45:00Z",
        "coverage": {
            "minimum_closed_candles": 2880,
            "warmup_closed_candles": 400,
            "integrity_margin_closed_candles": 8,
            "freshness_max_age_seconds": 3600,
        },
    }
    assert plan["market_snapshot_id"] is None
    assert plan["trading_capability"] == "TRADING_DISABLED"
    assert plan["execution_side_effects"] == 0
    with pytest.raises(
        rollout.MarketWindowRolloutBlocked,
        match="BLOCKED_WINDOW_END_AT_TIMEZONE_UNSET",
    ):
        rollout.plan("2026-08-14T10:45:00")
    with pytest.raises(
        rollout.MarketWindowRolloutBlocked,
        match="BLOCKED_WINDOW_END_AT_NOT_15M_ALIGNED",
    ):
        rollout.plan("2026-08-14T10:46:00Z")


def test_apply_is_cross_layer_audited_idempotent_and_remains_market_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rollout = _load(WINDOW_SCRIPT, "canonical_v13_window_apply")
    engine, client = _client()

    def request(path: str, *, body=None):
        response = (
            client.post(API_PREFIX + path, json=body)
            if body is not None
            else client.get(API_PREFIX + path)
        )
        assert response.status_code < 400, response.text
        return response.json()

    monkeypatch.setattr(
        rollout,
        "_health",
        lambda: {"status": "HEALTHY", "trading_capability": "TRADING_DISABLED"},
    )
    monkeypatch.setattr(rollout, "_request", request)
    try:
        first = rollout.apply("2026-08-14T10:45:00Z")
        replay = rollout.apply("2026-08-14T10:45:00Z")
        assert first["status"] == "WINDOW_AND_AGGREGATE_VALIDATED_MARKET_BLOCKED"
        assert first["bundle_preview"]["reason_codes"] == ["MARKET_SNAPSHOT_UNSET"]
        assert first["bundle_preview"]["bundle_digest"] is None
        assert first["bundle_preview"]["prospective_bundle_id"] is None
        assert replay["window"]["draft"]["idempotent_replay"] is True
        assert replay["window"]["snapshot"]["idempotent_replay"] is True
        assert replay["research_aggregate"]["draft"]["idempotent_replay"] is True
        assert replay["research_aggregate"]["snapshot"]["idempotent_replay"] is True
        assert replay["snapshot_ids"] == first["snapshot_ids"]
        assert _count(engine, CONFIGURATION_VERSIONS_TABLE) == 9
        assert _count(engine, CONFIGURATION_SNAPSHOTS_TABLE) == 9
        assert _count(engine, IDEMPOTENCY_RECEIPTS_TABLE) == 4
        assert _count(engine, AUDIT_EVENTS_TABLE) == 4
        for table in (
            CONFIGURATION_BUNDLES_TABLE,
            CONFIGURATION_ACTIVATIONS_TABLE,
            VALIDATION_ATTEMPTS_TABLE,
            RUNTIME_INSTANCES_TABLE,
            SIGNALS_TABLE,
            ORDERS_TABLE,
        ):
            assert _count(engine, table) == 0
    finally:
        client.close()
        engine.dispose()


def test_apply_blocks_unreviewed_latest_draft_before_writing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rollout = _load(WINDOW_SCRIPT, "canonical_v13_window_ambiguous")
    catalog = {
        "status": "AVAILABLE",
        "items": [
            {
                "profile_key": rollout.PROFILE_KEYS[kind],
                "configuration_kind": kind,
                "scope_key": rollout.SCOPE,
                "workflow_key": rollout.WORKFLOW,
                "versions": [
                    {
                        "version_id": f"version-{kind}",
                        "version_number": 1,
                        "lifecycle_status": "DRAFT" if kind == "WINDOW" else "VALIDATED",
                        "snapshot_id": None if kind == "WINDOW" else f"snapshot-{kind}",
                        "snapshot_digest": None if kind == "WINDOW" else "a" * 64,
                    }
                ],
            }
            for kind in (*rollout.DEPENDENCY_KINDS, "RESEARCH_AGGREGATE")
        ],
    }
    posts: list[str] = []

    def request(path: str, *, body=None):
        if body is not None:
            posts.append(path)
        return catalog

    monkeypatch.setattr(
        rollout,
        "_health",
        lambda: {"status": "HEALTHY", "trading_capability": "TRADING_DISABLED"},
    )
    monkeypatch.setattr(rollout, "_request", request)
    with pytest.raises(
        rollout.MarketWindowRolloutBlocked,
        match="BLOCKED_WINDOW_LATEST_VERSION_NOT_VALIDATED",
    ):
        rollout.apply("2026-08-14T10:45:00Z")
    assert posts == []
