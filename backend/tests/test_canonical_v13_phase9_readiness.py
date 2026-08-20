from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import func, select

from app.canonical_v13.models import QUALIFICATION_DECISIONS_TABLE
from app.canonical_v13.phase9_readiness import (
    EXECUTION_DOMAIN_TABLE_NAMES,
    CanonicalPhase9ReadinessBlocked,
    inspect_phase9_readiness,
)
from app.canonical_v13.phase9_topology import (
    PHASE9_SERVICE_SPECS,
    CanonicalPhase9TopologyBlocked,
    phase9_topology_digest,
    validate_phase9_topology,
)
from app.canonical_v13.phase9_schema_upgrade import (
    PHASE9_UNIQUE_CONSTRAINTS,
    CanonicalPhase9SchemaUpgradeBlocked,
    render_phase9_uniqueness_rollback_sql,
    render_phase9_uniqueness_upgrade_sql,
    verify_phase9_schema_upgrade,
)
from app.canonical_v13.research_evaluation import qualify_target, score_target
from tests.test_canonical_v13_research_evaluation import (
    _passing_metrics,
    _validated_attempt,
    canonical_connection,
)


def _qualified(connection):
    plan_id, attempt_id = _validated_attempt(
        connection, metrics_by_window=_passing_metrics()
    )
    score_target(
        connection,
        validation_plan_id=plan_id,
        validation_attempt_id=attempt_id,
        scorer_identity="phase9-readiness-test-scorer",
    )
    return qualify_target(
        connection,
        validation_plan_id=plan_id,
        validation_attempt_id=attempt_id,
        qualifier_identity="phase9-readiness-test-qualifier",
    )


def test_phase9_topology_is_exact_and_digest_stable() -> None:
    validate_phase9_topology()
    assert phase9_topology_digest() == phase9_topology_digest()
    assert len(phase9_topology_digest()) == 64
    assert {
        spec.process_identity for spec in PHASE9_SERVICE_SPECS.values()
    } == {
        "canonical-v13-control-activation-v1",
        "canonical-v13-ephemeral-research-v1",
        "canonical-v13-long-lived-runtime-v1",
        "canonical-v13-order-writer-v1",
    }
    assert PHASE9_SERVICE_SPECS["ephemeral_research"].network_policy == "NONE"
    assert PHASE9_SERVICE_SPECS["ephemeral_research"].keep_alive is False
    assert PHASE9_SERVICE_SPECS["long_lived_runtime"].order_writer_capability is False
    assert PHASE9_SERVICE_SPECS["order_writer"].order_writer_capability is True


def test_phase9_topology_blocks_runtime_order_writer_capability() -> None:
    drifted = dict(PHASE9_SERVICE_SPECS)
    drifted["long_lived_runtime"] = replace(
        drifted["long_lived_runtime"], order_writer_capability=True
    )
    with pytest.raises(
        CanonicalPhase9TopologyBlocked,
        match="BLOCKED_LONG_LIVED_RUNTIME_CAPABILITY_DRIFT",
    ):
        validate_phase9_topology(drifted)


def test_current_empty_execution_domain_is_blocked_without_qualified_handoff(
    canonical_connection,
) -> None:
    before = canonical_connection.execute(
        select(func.count()).select_from(QUALIFICATION_DECISIONS_TABLE)
    ).scalar_one()
    receipt = inspect_phase9_readiness(canonical_connection)
    after = canonical_connection.execute(
        select(func.count()).select_from(QUALIFICATION_DECISIONS_TABLE)
    ).scalar_one()

    assert receipt.status == "BLOCKED"
    assert receipt.reason_codes == ("CURRENT_CANONICAL_QUALIFIED_UNSET",)
    assert receipt.handoff is None
    assert receipt.qualification_status_counts == {}
    assert tuple(receipt.execution_domain_counts) == EXECUTION_DOMAIN_TABLE_NAMES
    assert set(receipt.execution_domain_counts.values()) == {0}
    assert before == after == 0


def test_exact_qualified_handoff_is_ready_and_receipt_replays_byte_identically(
    canonical_connection,
) -> None:
    with canonical_connection.begin():
        qualification = _qualified(canonical_connection)
        first = inspect_phase9_readiness(canonical_connection)
        repeated = inspect_phase9_readiness(canonical_connection)

    assert first.status == "READY"
    assert first.reason_codes == ()
    assert first.qualification_status_counts == {"QUALIFIED": 1}
    assert first.handoff is not None
    assert (
        first.handoff.qualification_decision_id
        == qualification.qualification_decision_id
    )
    assert (
        first.handoff.qualification_decision_digest
        == qualification.decision_digest
    )
    assert first == repeated
    assert len(first.receipt_digest) == 64


def test_later_stages_cannot_be_called_ready_from_qualification_alone(
    canonical_connection,
) -> None:
    with canonical_connection.begin():
        _qualified(canonical_connection)
        soak = inspect_phase9_readiness(
            canonical_connection, stage="NO_ORDER_SOAK"
        )
        shadow = inspect_phase9_readiness(
            canonical_connection, stage="SIGNAL_RISK_SHADOW"
        )

    assert soak.status == "BLOCKED"
    assert soak.reason_codes == (
        "DEPLOYMENT_APPROVALS_EVIDENCE_UNSET",
        "DEPLOYMENTS_EVIDENCE_UNSET",
        "RUNTIME_INSTANCES_EVIDENCE_UNSET",
        "RUNTIME_RECEIPTS_EVIDENCE_UNSET",
    )
    assert shadow.status == "BLOCKED"
    assert shadow.reason_codes[-3:] == (
        "SIGNALS_EVIDENCE_UNSET",
        "TRADE_INTENTS_EVIDENCE_UNSET",
        "RISK_DECISIONS_EVIDENCE_UNSET",
    )


def test_unknown_phase9_stage_fails_closed(canonical_connection) -> None:
    with pytest.raises(CanonicalPhase9ReadinessBlocked, match="BLOCKED_PHASE9_STAGE"):
        inspect_phase9_readiness(canonical_connection, stage="DEMO_CANARY")


def test_phase9_uniqueness_upgrade_is_additive_and_exact() -> None:
    upgrade = render_phase9_uniqueness_upgrade_sql()
    rollback = render_phase9_uniqueness_rollback_sql()
    for constraint, (table, columns) in PHASE9_UNIQUE_CONSTRAINTS.items():
        assert (
            f"ALTER TABLE strategy_platform_v13.{table} ADD CONSTRAINT "
            f"{constraint} UNIQUE ({', '.join(columns)})"
        ) in upgrade
        assert (
            f"ALTER TABLE strategy_platform_v13.{table} DROP CONSTRAINT "
            f"{constraint}"
        ) in rollback
    assert "INSERT " not in upgrade.upper()
    assert "UPDATE " not in upgrade.upper()
    assert "DELETE " not in upgrade.upper()
    assert "TRUNCATE " not in upgrade.upper()


def test_phase9_schema_upgrade_requires_postgresql(canonical_connection) -> None:
    with pytest.raises(
        CanonicalPhase9SchemaUpgradeBlocked, match="BLOCKED_POSTGRESQL_REQUIRED"
    ):
        verify_phase9_schema_upgrade(canonical_connection)
