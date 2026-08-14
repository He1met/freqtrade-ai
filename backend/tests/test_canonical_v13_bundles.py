from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select

from app.canonical_v13.bundles import (
    CanonicalBundleBlocked,
    activate_research_bundle,
    preview_research_bundle,
)
from app.canonical_v13.control_plane import (
    ConfigurationDependencyInput,
    create_configuration_draft,
    validate_configuration_version,
)
from app.canonical_v13.genesis import install_canonical_genesis
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
    CANONICAL_TABLES,
    CONFIGURATION_ACTIVATIONS_TABLE,
    CONFIGURATION_BUNDLE_MEMBERS_TABLE,
    CONFIGURATION_BUNDLES_TABLE,
    RESEARCH_TARGETS_TABLE,
)


NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)
SCHEMA = {"type": "object", "additionalProperties": False}
ADAPTER_DIGEST = "a" * 64
MANIFEST_DIGEST = "b" * 64
EXECUTION_TABLE_NAMES = (
    "validation_plans",
    "validation_plan_windows",
    "validation_attempts",
    "validation_window_results",
    "target_scores",
    "qualification_decisions",
    "qualification_window_evidence",
    "optimization_runs",
    "optimization_trials",
    "deployment_approvals",
    "deployments",
    "runtime_instances",
    "runtime_receipts",
    "signals",
    "trade_intents",
    "risk_decisions",
    "orders",
    "fills",
    "ledger_entries",
    "reconciliation_runs",
    "reconciliation_items",
)


@pytest.fixture
def canonical_connection():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    raw = engine.connect()
    with raw.begin():
        install_canonical_genesis(raw, installer_identity="phase3-bundle-test")
    connection = raw.execution_options(
        schema_translate_map={CANONICAL_BUSINESS_SCHEMA: None}
    )
    try:
        yield connection
    finally:
        raw.close()
        engine.dispose()


def _count(connection, table) -> int:
    return int(connection.execute(select(func.count()).select_from(table)).scalar_one())


def _draft(connection, kind, payload, *, dependencies=()):
    return create_configuration_draft(
        connection,
        profile_key=f"phase3-bundle-{kind.lower()}",
        configuration_kind=kind,
        scope_key="isolated-research",
        workflow_key="RESEARCH",
        schema_json=SCHEMA,
        payload_json=payload,
        adapter_identity=f"{kind.lower()}-adapter-v1",
        adapter_digest=ADAPTER_DIGEST,
        dependencies=dependencies,
    )


def _dependency(draft, kind):
    return ConfigurationDependencyInput(
        version_id=draft.version_id,
        expected_kind=kind,
        relation_key=f"snapshot:{kind.lower()}",
    )


def _freeze_bundle_inputs(
    connection,
    *,
    minimum_closed_candles: int = 72,
    market_row_count: int = 72,
    window_utc_z: bool = False,
):
    target = _draft(
        connection,
        "TARGET",
        {
            "targets": [
                {
                    "target_key": "fixture-btc-5m",
                    "instrument": "BTC-USDT-SWAP",
                    "pair": "BTC/USDT:USDT",
                    "timeframe": "5m",
                    "data_kind": "futures",
                }
            ]
        },
    )
    target_snapshot = validate_configuration_version(
        connection,
        version_id=target.version_id,
        adapter_manifest_digest=MANIFEST_DIGEST,
    )
    window = _draft(
        connection,
        "WINDOW",
        {
            "windows": [
                {
                    "window_key": "fixture-required",
                    "required": True,
                    "start_at": (
                        NOW.isoformat().replace("+00:00", "Z")
                        if window_utc_z
                        else NOW.isoformat()
                    ),
                    "end_at": (
                        (NOW + timedelta(hours=6)).isoformat().replace("+00:00", "Z")
                        if window_utc_z
                        else (NOW + timedelta(hours=6)).isoformat()
                    ),
                    "coverage": {
                        "minimum_closed_candles": minimum_closed_candles
                    },
                }
            ]
        },
    )
    window_snapshot = validate_configuration_version(
        connection,
        version_id=window.version_id,
        adapter_manifest_digest=MANIFEST_DIGEST,
    )
    generation = _draft(
        connection,
        "GENERATION",
        {
            "allocations": [
                {
                    "target_key": "fixture-btc-5m",
                    "allocation_count": 2,
                    "candidate_cap": 3,
                }
            ]
        },
        dependencies=(_dependency(target, "TARGET"),),
    )
    generation_snapshot = validate_configuration_version(
        connection,
        version_id=generation.version_id,
        adapter_manifest_digest=MANIFEST_DIGEST,
    )
    drafts = {"TARGET": target, "WINDOW": window, "GENERATION": generation}
    snapshots = {
        "TARGET": target_snapshot,
        "WINDOW": window_snapshot,
        "GENERATION": generation_snapshot,
    }
    for kind in ("DIVERSITY", "QUALITY_QUALIFICATION", "SCORING"):
        payload = {
            "DIVERSITY": {
                "rules": [
                    {
                        "rule_key": "fixture-family",
                        "algorithm": "fixture-correlation-v1",
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
                        "gate_key": "fixture-minimum-trades",
                        "metric": "trade_count",
                        "operator": ">=",
                        "threshold": 1,
                    }
                ],
            },
            "SCORING": {
                "window_aggregation": "MINIMUM",
                "components": [
                    {
                        "component_key": "fixture-profit-factor",
                        "metric": "profit_factor",
                        "weight": 1.0,
                        "direction": "maximize",
                        "minimum": 0.0,
                        "maximum": 3.0,
                    }
                ]
            },
        }[kind]
        draft = _draft(
            connection,
            kind,
            payload,
        )
        drafts[kind] = draft
        snapshots[kind] = validate_configuration_version(
            connection,
            version_id=draft.version_id,
            adapter_manifest_digest=MANIFEST_DIGEST,
        )
    aggregate = _draft(
        connection,
        "RESEARCH_AGGREGATE",
        {"assembly_key": "fixture-explicit"},
        dependencies=tuple(_dependency(drafts[kind], kind) for kind in drafts),
    )
    aggregate_snapshot = validate_configuration_version(
        connection,
        version_id=aggregate.version_id,
        adapter_manifest_digest=MANIFEST_DIGEST,
    )
    snapshots["RESEARCH_AGGREGATE"] = aggregate_snapshot
    snapshot_ids = {kind: snapshots[kind].snapshot_id for kind in P0_CONFIGURATION_KINDS}

    target_id = connection.execute(
        select(RESEARCH_TARGETS_TABLE.c.id).where(
            RESEARCH_TARGETS_TABLE.c.target_snapshot_id == target_snapshot.snapshot_id
        )
    ).scalar_one()
    _profile_id, market_version_id, _digest = create_market_profile_draft(
        connection,
        profile_key="fixture-market",
        scope_key="isolated-research",
        payload={"source": "isolated-fixture", "network_access": "NONE"},
    )
    validate_market_profile(connection, version_id=market_version_id)
    evidence = accept_market_artifact(
        connection,
        locator="fixtures/BTC-USDT-SWAP-5m.parquet",
        content=b"isolated canonical market fixture",
        media_type="application/x-parquet",
        inspector_identity="phase3-fixture-inspector-v1",
        facts=MarketInspectionFacts(
            row_count=market_row_count,
            first_open_at=NOW,
            last_close_at=NOW + timedelta(hours=6),
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
                NOW,
                NOW + timedelta(hours=6),
            ),
        ),
    )
    return snapshot_ids, market_snapshot.snapshot_id


def _execution_counts(connection):
    return {
        name: _count(connection, CANONICAL_TABLES[name])
        for name in EXECUTION_TABLE_NAMES
    }


def test_preview_is_deterministic_ready_and_strictly_read_only(
    canonical_connection,
) -> None:
    with canonical_connection.begin():
        snapshot_ids, market_snapshot_id = _freeze_bundle_inputs(canonical_connection)
        before = {
            "bundles": _count(canonical_connection, CONFIGURATION_BUNDLES_TABLE),
            "members": _count(canonical_connection, CONFIGURATION_BUNDLE_MEMBERS_TABLE),
            "activations": _count(canonical_connection, CONFIGURATION_ACTIVATIONS_TABLE),
            "audits": _count(canonical_connection, AUDIT_EVENTS_TABLE),
        }
        first = preview_research_bundle(
            canonical_connection,
            scope_key="isolated-research",
            workflow_key="RESEARCH",
            snapshot_ids=snapshot_ids,
            market_snapshot_id=market_snapshot_id,
        )
        second = preview_research_bundle(
            canonical_connection,
            scope_key="isolated-research",
            workflow_key="RESEARCH",
            snapshot_ids=snapshot_ids,
            market_snapshot_id=market_snapshot_id,
        )
        after = {
            "bundles": _count(canonical_connection, CONFIGURATION_BUNDLES_TABLE),
            "members": _count(canonical_connection, CONFIGURATION_BUNDLE_MEMBERS_TABLE),
            "activations": _count(canonical_connection, CONFIGURATION_ACTIVATIONS_TABLE),
            "audits": _count(canonical_connection, AUDIT_EVENTS_TABLE),
        }

    assert first.status == "READY"
    assert first.reason_codes == ()
    assert first.bundle_digest == second.bundle_digest
    assert first.prospective_bundle_id == second.prospective_bundle_id
    assert first.target_count == 1
    assert first.total_candidate_count == 2
    assert first.capability_json["trading"] == "TRADING_DISABLED"
    assert before == after == {"bundles": 0, "members": 0, "activations": 0, "audits": 0}


def test_activation_only_materializes_bundle_pointer_and_is_idempotent(
    canonical_connection,
) -> None:
    with canonical_connection.begin():
        snapshot_ids, market_snapshot_id = _freeze_bundle_inputs(canonical_connection)
        execution_before = _execution_counts(canonical_connection)
        preview = preview_research_bundle(
            canonical_connection,
            scope_key="isolated-research",
            workflow_key="RESEARCH",
            snapshot_ids=snapshot_ids,
            market_snapshot_id=market_snapshot_id,
        )
        first = activate_research_bundle(
            canonical_connection,
            scope_key="isolated-research",
            workflow_key="RESEARCH",
            snapshot_ids=snapshot_ids,
            market_snapshot_id=market_snapshot_id,
            actor_identity="isolated-phase3-operator",
            expected_bundle_digest=preview.bundle_digest,
            expected_bundle_id=preview.prospective_bundle_id,
        )
        repeated = activate_research_bundle(
            canonical_connection,
            scope_key="isolated-research",
            workflow_key="RESEARCH",
            snapshot_ids=snapshot_ids,
            market_snapshot_id=market_snapshot_id,
            actor_identity="isolated-phase3-operator",
            expected_bundle_digest=preview.bundle_digest,
            expected_bundle_id=preview.prospective_bundle_id,
        )
        execution_after = _execution_counts(canonical_connection)

    assert first.repeat_noop is False
    assert first.created_bundle is True
    assert repeated.repeat_noop is True
    assert repeated.configuration_bundle_id == first.configuration_bundle_id
    assert repeated.configuration_activation_id == first.configuration_activation_id
    assert _count(canonical_connection, CONFIGURATION_BUNDLES_TABLE) == 1
    assert _count(canonical_connection, CONFIGURATION_BUNDLE_MEMBERS_TABLE) == 7
    assert _count(canonical_connection, CONFIGURATION_ACTIVATIONS_TABLE) == 1
    assert _count(canonical_connection, AUDIT_EVENTS_TABLE) == 1
    assert execution_before == execution_after
    assert set(execution_after.values()) == {0}


def test_blocked_preview_and_digest_drift_cannot_write_control_rows(
    canonical_connection,
) -> None:
    with canonical_connection.begin():
        snapshot_ids, market_snapshot_id = _freeze_bundle_inputs(canonical_connection)
        blocked = preview_research_bundle(
            canonical_connection,
            scope_key="isolated-research",
            workflow_key="RESEARCH",
            snapshot_ids={
                kind: snapshot_id
                for kind, snapshot_id in snapshot_ids.items()
                if kind != "SCORING"
            },
            market_snapshot_id=None,
        )
        ready = preview_research_bundle(
            canonical_connection,
            scope_key="isolated-research",
            workflow_key="RESEARCH",
            snapshot_ids=snapshot_ids,
            market_snapshot_id=market_snapshot_id,
        )
    assert blocked.status == "BLOCKED"
    assert "SCORING_SNAPSHOT_UNSET" in blocked.reason_codes
    assert "MARKET_SNAPSHOT_UNSET" in blocked.reason_codes

    with pytest.raises(CanonicalBundleBlocked) as raised:
        with canonical_connection.begin():
            activate_research_bundle(
                canonical_connection,
                scope_key="isolated-research",
                workflow_key="RESEARCH",
                snapshot_ids=snapshot_ids,
                market_snapshot_id=market_snapshot_id,
                actor_identity="isolated-phase3-operator",
                expected_bundle_digest="0" * 64,
                expected_bundle_id=ready.prospective_bundle_id,
            )
    assert raised.value.code == "BLOCKED_PREVIEW_DIGEST_DRIFT"
    assert _count(canonical_connection, CONFIGURATION_BUNDLES_TABLE) == 0
    assert _count(canonical_connection, CONFIGURATION_ACTIVATIONS_TABLE) == 0
    assert _count(canonical_connection, AUDIT_EVENTS_TABLE) == 0
    canonical_connection.rollback()

    with pytest.raises(CanonicalBundleBlocked) as identity_drift:
        with canonical_connection.begin():
            activate_research_bundle(
                canonical_connection,
                scope_key="isolated-research",
                workflow_key="RESEARCH",
                snapshot_ids=snapshot_ids,
                market_snapshot_id=market_snapshot_id,
                actor_identity="isolated-phase3-operator",
                expected_bundle_digest=ready.bundle_digest,
                expected_bundle_id=market_snapshot_id,
            )
    assert identity_drift.value.code == "BLOCKED_BUNDLE_ID_DRIFT"
    assert _count(canonical_connection, CONFIGURATION_BUNDLES_TABLE) == 0


def test_preview_rejects_configuration_scope_drift(canonical_connection) -> None:
    with canonical_connection.begin():
        snapshot_ids, market_snapshot_id = _freeze_bundle_inputs(canonical_connection)
        preview = preview_research_bundle(
            canonical_connection,
            scope_key="another-scope",
            workflow_key="RESEARCH",
            snapshot_ids=snapshot_ids,
            market_snapshot_id=market_snapshot_id,
        )
    assert preview.status == "BLOCKED"
    assert set(preview.reason_codes) >= {
        f"{kind}_SCOPE_MISMATCH" for kind in P0_CONFIGURATION_KINDS
    }
    assert preview.bundle_digest is None
    assert _count(canonical_connection, CONFIGURATION_BUNDLES_TABLE) == 0


def test_preview_joins_required_window_minimum_candles_to_market_inspection(
    canonical_connection,
) -> None:
    with canonical_connection.begin():
        snapshot_ids, market_snapshot_id = _freeze_bundle_inputs(
            canonical_connection,
            minimum_closed_candles=73,
            market_row_count=72,
        )
        preview = preview_research_bundle(
            canonical_connection,
            scope_key="isolated-research",
            workflow_key="RESEARCH",
            snapshot_ids=snapshot_ids,
            market_snapshot_id=market_snapshot_id,
        )
    assert preview.status == "BLOCKED"
    assert (
        "REQUIRED_WINDOW_CANDLE_COUNT_LOW:fixture-required"
        in preview.reason_codes
    )
    assert preview.bundle_digest is None
