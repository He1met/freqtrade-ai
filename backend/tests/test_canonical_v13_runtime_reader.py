from __future__ import annotations

import inspect

import pytest
from sqlalchemy import create_engine, func, select

from app.canonical_v13.bundles import (
    activate_research_bundle,
    preview_research_bundle,
)
from app.canonical_v13.genesis import install_canonical_genesis
from app.canonical_v13.manifest import CANONICAL_BUSINESS_SCHEMA
from app.canonical_v13.models import CANONICAL_TABLES, CONFIGURATION_BUNDLES_TABLE
from app.canonical_v13.runtime_reader import (
    CanonicalFrozenReaderBlocked,
    read_frozen_research_bundle,
    resolve_active_research_binding,
)
from tests.test_canonical_v13_bundles import _freeze_bundle_inputs


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
        install_canonical_genesis(raw, installer_identity="phase6-reader-test")
    connection = raw.execution_options(
        schema_translate_map={CANONICAL_BUSINESS_SCHEMA: None}
    )
    try:
        yield connection
    finally:
        raw.close()
        engine.dispose()


def _count(connection, table_name: str) -> int:
    return int(
        connection.execute(
            select(func.count()).select_from(CANONICAL_TABLES[table_name])
        ).scalar_one()
    )


def _execution_counts(connection) -> dict[str, int]:
    return {name: _count(connection, name) for name in EXECUTION_TABLE_NAMES}


def _activate_fixture(connection, *, window_utc_z: bool = False):
    snapshot_ids, market_snapshot_id = _freeze_bundle_inputs(
        connection, window_utc_z=window_utc_z
    )
    preview = preview_research_bundle(
        connection,
        scope_key="isolated-research",
        workflow_key="RESEARCH",
        snapshot_ids=snapshot_ids,
        market_snapshot_id=market_snapshot_id,
    )
    activation = activate_research_bundle(
        connection,
        scope_key="isolated-research",
        workflow_key="RESEARCH",
        snapshot_ids=snapshot_ids,
        market_snapshot_id=market_snapshot_id,
        actor_identity="phase6-isolated-operator",
        expected_bundle_digest=preview.bundle_digest,
        expected_bundle_id=preview.prospective_bundle_id,
    )
    return activation


def test_missing_active_bundle_fails_closed_and_writes_nothing(
    canonical_connection,
) -> None:
    before = _execution_counts(canonical_connection)
    with pytest.raises(CanonicalFrozenReaderBlocked) as raised:
        resolve_active_research_binding(
            canonical_connection,
            scope_key="isolated-research",
            workflow_key="RESEARCH",
        )
    after = _execution_counts(canonical_connection)

    assert raised.value.code == "RESEARCH_BUNDLE_UNSET"
    assert before == after
    assert set(after.values()) == {0}


def test_control_resolver_and_explicit_frozen_reader_have_separate_contracts(
    canonical_connection,
) -> None:
    with canonical_connection.begin():
        activation = _activate_fixture(canonical_connection)
        before = _execution_counts(canonical_connection)
        binding = resolve_active_research_binding(
            canonical_connection,
            scope_key="isolated-research",
            workflow_key="RESEARCH",
        )
        frozen = read_frozen_research_bundle(
            canonical_connection,
            configuration_bundle_id=binding.configuration_bundle_id,
            expected_bundle_digest=binding.configuration_bundle_digest,
        )
        after = _execution_counts(canonical_connection)

    assert binding.configuration_bundle_id == activation.configuration_bundle_id
    assert frozen.status == "PENDING_FIRST_BACKTEST"
    assert frozen.reason_codes == ("PENDING_FIRST_BACKTEST", "TRADING_DISABLED")
    assert len(frozen.configurations) == 7
    assert [target.target_key for target in frozen.targets] == ["fixture-btc-5m"]
    assert frozen.allocations[0].allocation_count == 2
    assert frozen.allocations[0].candidate_cap == 3
    assert frozen.windows[0].window_key == "fixture-required"
    assert frozen.windows[0].minimum_closed_candles == 72
    assert frozen.capability["exchange_access"] == "NONE"
    assert frozen.capability["order_submission"] == "DISABLED"
    assert before == after
    assert set(after.values()) == {0}


def test_reader_normalizes_z_window_payload_to_frozen_member_digest(
    canonical_connection,
) -> None:
    with canonical_connection.begin():
        activation = _activate_fixture(canonical_connection, window_utc_z=True)
        frozen = read_frozen_research_bundle(
            canonical_connection,
            configuration_bundle_id=activation.configuration_bundle_id,
            expected_bundle_digest=activation.bundle_digest,
        )

    assert frozen.windows[0].start_at.endswith("+00:00")
    assert frozen.windows[0].end_at.endswith("+00:00")


def test_reader_rejects_digest_and_capability_drift(canonical_connection) -> None:
    with canonical_connection.begin():
        activation = _activate_fixture(canonical_connection)
    with pytest.raises(CanonicalFrozenReaderBlocked) as digest_drift:
        read_frozen_research_bundle(
            canonical_connection,
            configuration_bundle_id=activation.configuration_bundle_id,
            expected_bundle_digest="0" * 64,
        )
    assert digest_drift.value.code == "RESEARCH_BUNDLE_DIGEST_DRIFT"
    canonical_connection.rollback()

    with canonical_connection.begin():
        canonical_connection.execute(
            CONFIGURATION_BUNDLES_TABLE.update()
            .where(
                CONFIGURATION_BUNDLES_TABLE.c.id
                == activation.configuration_bundle_id
            )
            .values(capability_json={"trading": "ENABLED"})
        )
    with pytest.raises(CanonicalFrozenReaderBlocked) as capability_drift:
        read_frozen_research_bundle(
            canonical_connection,
            configuration_bundle_id=activation.configuration_bundle_id,
            expected_bundle_digest=activation.bundle_digest,
        )
    assert capability_drift.value.code == "RESEARCH_BUNDLE_CAPABILITY_DRIFT"


def test_reader_source_has_no_legacy_owner_or_activation_mutation_dependency() -> None:
    import app.canonical_v13.runtime_reader as module

    source = inspect.getsource(module)
    forbidden = (
        "app.models",
        "app.repositories",
        "owner_resolver",
        "activate_research_bundle",
        "freqtrade_ai_design_lab",
        "credential",
        "exchange_client",
        "order_writer",
    )
    assert all(token not in source for token in forbidden)
