from __future__ import annotations

import importlib.util
from pathlib import Path

from sqlalchemy import create_engine

from app.canonical_v13.bundles import preview_research_bundle
from app.canonical_v13.control_plane import (
    ConfigurationDependencyInput,
    create_configuration_draft,
    validate_configuration_version,
)
from app.canonical_v13.genesis import install_canonical_genesis
from app.canonical_v13.manifest import CANONICAL_BUSINESS_SCHEMA


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "canonical_v13_p0_rollout.py"


def _load_rollout():
    spec = importlib.util.spec_from_file_location("canonical_v13_p0_rollout_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_initial_p0_contract_is_dynamic_explicit_and_no_trade() -> None:
    rollout = _load_rollout()
    plan = rollout.plan()
    payloads = plan["payloads"]
    assert plan["api_root"] == "http://127.0.0.1:8011/api/canonical-v13"
    assert plan["trading_capability"] == "TRADING_DISABLED"
    assert plan["execution_side_effects"] == 0
    assert tuple(payloads) == rollout.KINDS
    assert payloads["TARGET"]["targets"] == [
        {
            "target_key": "btc-usdt-swap-15m",
            "instrument": "BTC-USDT-SWAP",
            "pair": "BTC/USDT:USDT",
            "timeframe": "15m",
            "data_kind": "futures",
        }
    ]
    assert payloads["GENERATION"]["allocations"] == [
        {
            "target_key": "btc-usdt-swap-15m",
            "allocation_count": 6,
            "candidate_cap": 6,
        }
    ]
    assert len(payloads["WINDOW"]["windows"]) == 1
    assert payloads["WINDOW"]["windows"][0]["required"] is True


def test_quality_diversity_and_scoring_match_frozen_semantics() -> None:
    rollout = _load_rollout()
    payloads = rollout.p0_payloads()
    diversity = {rule["metric"] for rule in payloads["DIVERSITY"]["rules"]}
    assert diversity == {
        "code_digest_duplicate_count",
        "strategy_family_duplicate_count",
        "target_window_duplicate_count",
    }
    gates = {
        gate["metric"]: (gate["operator"], gate["threshold"])
        for gate in payloads["QUALITY_QUALIFICATION"]["required_window_gates"]
    }
    assert payloads["QUALITY_QUALIFICATION"]["minimum_score"] == 50
    assert gates == {
        "trade_count": (">=", 30),
        "net_return_after_cost": (">", 0),
        "maximum_drawdown": ("<=", 0.15),
        "fee_rate": (">=", 0.0005),
        "slippage_rate": (">=", 0.0002),
        "lookahead_failure_count": ("==", 0),
    }
    components = {
        item["component_key"]: item["weight"]
        for item in payloads["SCORING"]["components"]
    }
    assert components == {
        "profit": 0.35,
        "risk": 0.25,
        "stability": 0.15,
        "quality": 0.25,
    }
    assert sum(components.values()) == 1.0
    assert "mandatory-validations" in payloads["RESEARCH_AGGREGATE"]["assembly_key"]


def test_initial_p0_payloads_freeze_but_bundle_stays_blocked_without_market() -> None:
    rollout = _load_rollout()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    raw = engine.connect()
    try:
        with raw.begin():
            install_canonical_genesis(raw, installer_identity="p0-rollout-test")
        connection = raw.execution_options(
            schema_translate_map={CANONICAL_BUSINESS_SCHEMA: None}
        )
        drafts = {}
        snapshots = {}
        with connection.begin():
            for kind in rollout.KINDS:
                dependencies = tuple(
                    ConfigurationDependencyInput(
                        version_id=drafts[item].version_id,
                        expected_kind=item,
                        relation_key=f"snapshot:{item.lower()}",
                    )
                    for item in (
                        ("TARGET",)
                        if kind == "GENERATION"
                        else rollout.KINDS[:6]
                        if kind == "RESEARCH_AGGREGATE"
                        else ()
                    )
                )
                draft = create_configuration_draft(
                    connection,
                    profile_key=f"production-v13-{kind.lower()}",
                    configuration_kind=kind,
                    scope_key=rollout.SCOPE,
                    workflow_key=rollout.WORKFLOW,
                    schema_json=rollout.SCHEMA,
                    payload_json=rollout.p0_payloads()[kind],
                    adapter_identity=rollout.ADAPTER_IDENTITY,
                    adapter_digest=rollout.ADAPTER_DIGEST,
                    dependencies=dependencies,
                )
                drafts[kind] = draft
                snapshots[kind] = validate_configuration_version(
                    connection,
                    version_id=draft.version_id,
                    adapter_manifest_digest=rollout.ADAPTER_MANIFEST_DIGEST,
                )
            preview = preview_research_bundle(
                connection,
                scope_key=rollout.SCOPE,
                workflow_key=rollout.WORKFLOW,
                snapshot_ids={
                    kind: snapshots[kind].snapshot_id for kind in rollout.KINDS
                },
                market_snapshot_id=None,
            )
        assert snapshots["TARGET"].target_count == 1
        assert snapshots["GENERATION"].total_candidate_count == 6
        assert preview.status == "BLOCKED"
        assert "MARKET_SNAPSHOT_UNSET" in preview.reason_codes
        assert preview.bundle_digest is None
    finally:
        raw.close()
        engine.dispose()
