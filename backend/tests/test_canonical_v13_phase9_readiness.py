# ruff: noqa: F401, F811
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import pytest
from app.canonical_v13.accounting import (
    post_production_demo_ledger_entry,
    post_simulated_ledger_entry,
)
from app.canonical_v13.deployment_approval import approve_demo_deployment
from app.canonical_v13.deployment_control import (
    create_demo_deployment,
    launch_demo_runtime,
)
from app.canonical_v13.fill_service import (
    record_production_demo_fill,
    record_simulated_fill,
)
from app.canonical_v13.models import (
    DEPLOYMENTS_TABLE,
    ORDERS_TABLE,
    QUALIFICATION_DECISIONS_TABLE,
    RUNTIME_INSTANCES_TABLE,
    RUNTIME_RECEIPTS_TABLE,
    SIGNALS_TABLE,
)
from app.canonical_v13.order_service import record_simulated_order
from app.canonical_v13.phase9_execution_authority import (
    authorize_demo_risk_budget,
    decide_central_demo_risk,
    record_redacted_demo_attestation,
)
from app.canonical_v13.phase9_order_writer import (
    prepare_demo_order,
    release_demo_order_writer_lease,
)
from app.canonical_v13.phase9_recovery_acceptance import (
    Phase9RecoveryAcceptance,
    record_phase9_recovery_acceptance,
)
from app.canonical_v13.phase9_readiness import (
    EXECUTION_DOMAIN_TABLE_NAMES,
    CanonicalPhase9ReadinessBlocked,
    Phase9QualificationHandoff,
    inspect_phase9_readiness,
)
from app.canonical_v13.phase9_schema_upgrade import (
    PHASE9_UNIQUE_CONSTRAINTS,
    CanonicalPhase9SchemaUpgradeBlocked,
    render_phase9_uniqueness_rollback_sql,
    render_phase9_uniqueness_upgrade_sql,
    verify_phase9_schema_upgrade,
)
from app.canonical_v13.phase9_topology import (
    PHASE9_SERVICE_SPECS,
    CanonicalPhase9TopologyBlocked,
    phase9_topology_digest,
    validate_phase9_topology,
)
from app.canonical_v13.phase9_runtime_supervisor import build_lifecycle_receipt
from app.canonical_v13.reconciliation import (
    reconcile_production_demo_chain,
    reconcile_simulated_chain,
)
from app.canonical_v13.research_evaluation import qualify_target, score_target
from app.canonical_v13.risk_service import (
    create_production_demo_intent,
    create_simulated_intent,
    decide_simulated_risk,
)
from app.canonical_v13.runtime_contract import (
    FrozenRuntimeLaunchSpec,
    build_runtime_observation_receipt,
)
from app.canonical_v13.signal_service import (
    record_production_demo_signal,
    record_simulated_signal,
)
from sqlalchemy import func, select
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


def _handoff(connection, qualification) -> Phase9QualificationHandoff:
    row = (
        connection.execute(
            select(QUALIFICATION_DECISIONS_TABLE).where(
                QUALIFICATION_DECISIONS_TABLE.c.id
                == qualification.qualification_decision_id
            )
        )
        .mappings()
        .one()
    )
    return Phase9QualificationHandoff(
        qualification_decision_id=row["id"],
        qualification_decision_digest=row["decision_digest"],
        strategy_version_id=row["strategy_version_id"],
        research_target_id=row["research_target_id"],
        configuration_bundle_id=row["configuration_bundle_id"],
        configuration_bundle_digest=row["configuration_bundle_digest"],
        market_snapshot_id=row["market_snapshot_id"],
        market_snapshot_digest=row["market_snapshot_digest"],
        validation_plan_id=row["validation_plan_id"],
        validation_plan_digest=row["validation_plan_digest"],
    )


class _TestLauncher:
    evidence_class = "TEST_SIMULATED"

    def launch(self, spec: FrozenRuntimeLaunchSpec):
        return build_runtime_observation_receipt(
            runtime_instance_id=uuid4(),
            launch_spec=spec,
            status="HEALTHY",
            observed_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
            evidence_class=self.evidence_class,
        )


def _json_digest(value: object) -> str:
    return sha256(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()


def _seed_stage_a(connection, handoff: Phase9QualificationHandoff):
    approval = approve_demo_deployment(
        connection,
        qualification_decision_id=handoff.qualification_decision_id,
        actor_identity="phase9-human-approver",
        reason="explicit phase9 test approval",
    )
    deployment = create_demo_deployment(
        connection, deployment_approval_id=approval.deployment_approval_id
    )
    runtime_id = launch_demo_runtime(
        connection,
        deployment_id=deployment.deployment_id,
        runtime_identity=f"phase9-runtime-{handoff.qualification_decision_id}",
        image_digest="f" * 64,
        service_account="canonical_runtime_reader",
        credential_reference="test-reference-never-resolved",
        launcher=_TestLauncher(),
    )
    connection.execute(
        DEPLOYMENTS_TABLE.update()
        .where(DEPLOYMENTS_TABLE.c.id == deployment.deployment_id)
        .values(status="ACTIVE")
    )
    runtime = (
        connection.execute(
            select(RUNTIME_INSTANCES_TABLE).where(
                RUNTIME_INSTANCES_TABLE.c.id == runtime_id
            )
        )
        .mappings()
        .one()
    )
    receipt = (
        connection.execute(
            select(RUNTIME_RECEIPTS_TABLE).where(
                RUNTIME_RECEIPTS_TABLE.c.runtime_instance_id == runtime_id
            )
        )
        .mappings()
        .one()
    )
    observation = dict(receipt["observation_json"])
    observation["evidence_class"] = "PRODUCTION_DEMO_RUNTIME"
    observation_digest = _json_digest(observation)
    receipt_digest = _json_digest(
        {"contract": "canonical-v13-runtime-observation-v1", **observation}
    )
    connection.execute(
        RUNTIME_RECEIPTS_TABLE.update()
        .where(RUNTIME_RECEIPTS_TABLE.c.id == receipt["id"])
        .values(
            evidence_class="PRODUCTION_DEMO_RUNTIME",
            observation_json=observation,
            observation_digest=observation_digest,
            receipt_digest=receipt_digest,
            observed_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
        )
    )
    return deployment.deployment_id, runtime["id"]


def _seed_stage_b(connection, handoff, deployment_id, runtime_id):
    approval_id = connection.execute(
        select(DEPLOYMENTS_TABLE.c.deployment_approval_id).where(
            DEPLOYMENTS_TABLE.c.id == deployment_id
        )
    ).scalar_one()
    from tests.test_canonical_v13_phase9_execution_authority import (
        _risk_policy_source,
    )

    source_receipt = _risk_policy_source(
        connection,
        SimpleNamespace(deployment_approval_id=approval_id),
        policy_digest="a" * 64,
        accepted_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
    )
    budget = authorize_demo_risk_budget(
        connection,
        deployment_approval_id=approval_id,
        actor_identity="phase9-human-approver",
        reason="exact Phase 9 test risk budget",
        policy_source_receipt_digest=source_receipt,
        evaluated_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
    )
    risk_ids = []
    for index, size in enumerate(("1", "2"), start=1):
        signal_id = record_production_demo_signal(
            connection,
            deployment_id=deployment_id,
            runtime_instance_id=runtime_id,
            research_target_id=handoff.research_target_id,
            signal_json={
                "evidence_class": "PRODUCTION_OKX_DEMO",
                "natural_signal": True,
                "allow_real_funds": False,
                "configuration_bundle_digest": handoff.configuration_bundle_digest,
                "market_snapshot_digest": handoff.market_snapshot_digest,
                "side": "buy",
                "shadow_case": index,
            },
            evaluated_at=datetime(2026, 8, 21, tzinfo=timezone.utc)
            + timedelta(seconds=index),
        )
        exchange_body = {
            "instId": "BTC-USDT-SWAP",
            "tdMode": "isolated",
            "clOrdId": f"v13readiness{index:018d}",
            "side": "buy",
            "posSide": "long",
            "ordType": "post_only",
            "sz": size,
            "px": "10000",
        }
        signal_digest = connection.execute(
            select(SIGNALS_TABLE.c.signal_digest).where(SIGNALS_TABLE.c.id == signal_id)
        ).scalar_one()
        intent_id = create_production_demo_intent(
            connection,
            signal_id=signal_id,
            intent_json={
                "contract": "canonical-v13-demo-trade-intent-v1",
                "execution_target": "OKX_DEMO",
                "allow_real_funds": False,
                "signal_digest": signal_digest,
                "instrument": "BTC-USDT-SWAP",
                "notional": str(Decimal(size) * Decimal(10)),
                "exchange_body": exchange_body,
            },
        )
        risk_ids.append(
            decide_central_demo_risk(
                connection,
                trade_intent_id=intent_id,
                risk_budget_authorization_id=budget.authorization_id,
                evaluated_at=datetime(2026, 8, 21, tzinfo=timezone.utc)
                + timedelta(seconds=index + 2),
            ).risk_decision_id
        )
    return risk_ids


def test_phase9_topology_is_exact_and_digest_stable() -> None:
    validate_phase9_topology()
    assert phase9_topology_digest() == phase9_topology_digest()
    assert len(phase9_topology_digest()) == 64
    assert {spec.process_identity for spec in PHASE9_SERVICE_SPECS.values()} == {
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


def test_explicit_missing_qualification_handoff_is_blocked_read_only(
    canonical_connection,
) -> None:
    missing = Phase9QualificationHandoff(
        qualification_decision_id=uuid4(),
        qualification_decision_digest="0" * 64,
        strategy_version_id=uuid4(),
        research_target_id=uuid4(),
        configuration_bundle_id=uuid4(),
        configuration_bundle_digest="1" * 64,
        market_snapshot_id=uuid4(),
        market_snapshot_digest="2" * 64,
        validation_plan_id=uuid4(),
        validation_plan_digest="3" * 64,
    )
    before = canonical_connection.execute(
        select(func.count()).select_from(QUALIFICATION_DECISIONS_TABLE)
    ).scalar_one()
    receipt = inspect_phase9_readiness(
        canonical_connection, qualification_handoff=missing
    )
    after = canonical_connection.execute(
        select(func.count()).select_from(QUALIFICATION_DECISIONS_TABLE)
    ).scalar_one()

    assert receipt.status == "BLOCKED"
    assert receipt.reason_codes == ("EXACT_QUALIFICATION_DECISION_NOT_FOUND",)
    assert receipt.handoff is None
    assert receipt.qualification_status_counts == {}
    assert tuple(receipt.execution_domain_counts) == EXECUTION_DOMAIN_TABLE_NAMES
    assert set(receipt.execution_domain_counts.values()) == {0}
    assert set(receipt.lineage_evidence_counts.values()) == {0}
    assert before == after == 0


def test_exact_qualified_handoff_is_ready_and_receipt_replays_byte_identically(
    canonical_connection,
) -> None:
    with canonical_connection.begin():
        qualification = _qualified(canonical_connection)
        handoff = _handoff(canonical_connection, qualification)
        first = inspect_phase9_readiness(
            canonical_connection, qualification_handoff=handoff
        )
        repeated = inspect_phase9_readiness(
            canonical_connection, qualification_handoff=handoff
        )

    assert first.status == "READY"
    assert first.reason_codes == ()
    assert first.qualification_status_counts == {"QUALIFIED": 1}
    assert first.handoff is not None
    assert (
        first.handoff.qualification_decision_id
        == qualification.qualification_decision_id
    )
    assert first.handoff.qualification_decision_digest == qualification.decision_digest
    assert first == repeated
    assert len(first.receipt_digest) == 64


def test_multiple_qualified_rows_do_not_make_explicit_handoff_ambiguous(
    canonical_connection,
) -> None:
    with canonical_connection.begin():
        selected = _qualified(canonical_connection)
        _qualified(canonical_connection)
        receipt = inspect_phase9_readiness(
            canonical_connection,
            qualification_handoff=_handoff(canonical_connection, selected),
        )

    assert receipt.status == "READY"
    assert receipt.reason_codes == ()
    assert receipt.qualification_status_counts == {"QUALIFIED": 2}
    assert (
        receipt.handoff.qualification_decision_id == selected.qualification_decision_id
    )


def test_wrong_exact_lineage_digest_is_blocked_and_never_echoed_as_verified(
    canonical_connection,
) -> None:
    with canonical_connection.begin():
        qualification = _qualified(canonical_connection)
        drifted = replace(
            _handoff(canonical_connection, qualification),
            configuration_bundle_digest="f" * 64,
        )
        receipt = inspect_phase9_readiness(
            canonical_connection, qualification_handoff=drifted
        )

    assert receipt.status == "BLOCKED"
    assert receipt.handoff is None
    assert receipt.reason_codes == (
        "EXACT_QUALIFICATION_HANDOFF_CONFIGURATION_BUNDLE_DIGEST_MISMATCH",
    )


def test_unrelated_approval_cannot_satisfy_selected_handoff(
    canonical_connection,
) -> None:
    with canonical_connection.begin():
        selected = _qualified(canonical_connection)
        unrelated = _qualified(canonical_connection)
        approve_demo_deployment(
            canonical_connection,
            qualification_decision_id=unrelated.qualification_decision_id,
            actor_identity="unrelated-human",
            reason="must not satisfy selected lineage",
        )
        receipt = inspect_phase9_readiness(
            canonical_connection,
            qualification_handoff=_handoff(canonical_connection, selected),
            stage="NO_ORDER_SOAK",
            evaluated_at=datetime(2026, 8, 21, tzinfo=timezone.utc)
            + timedelta(seconds=20),
        )

    assert receipt.status == "BLOCKED"
    assert receipt.reason_codes == (
        "EXACT_APPROVED_DEPLOYMENT_APPROVAL_EVIDENCE_UNSET",
    )
    assert receipt.execution_domain_counts["deployment_approvals"] == 1
    assert receipt.lineage_evidence_counts["deployment_approvals"] == 0


def test_no_order_soak_requires_exact_production_runtime_and_replays(
    canonical_connection,
) -> None:
    with canonical_connection.begin():
        qualification = _qualified(canonical_connection)
        handoff = _handoff(canonical_connection, qualification)
        _seed_stage_a(canonical_connection, handoff)
        first = inspect_phase9_readiness(
            canonical_connection,
            qualification_handoff=handoff,
            stage="NO_ORDER_SOAK",
            evaluated_at=datetime(2026, 8, 21, tzinfo=timezone.utc)
            + timedelta(seconds=20),
        )
        repeated = inspect_phase9_readiness(
            canonical_connection,
            qualification_handoff=handoff,
            stage="NO_ORDER_SOAK",
            evaluated_at=datetime(2026, 8, 21, tzinfo=timezone.utc)
            + timedelta(seconds=20),
        )

    assert first.status == "READY"
    assert first.reason_codes == ()
    assert first.lineage_evidence_counts["runtime_instances"] == 1
    assert first.lineage_evidence_counts["runtime_receipts"] == 1
    assert first.execution_domain_counts["orders"] == 0
    assert first == repeated


def test_no_order_soak_blocks_an_unrelated_second_active_runtime(
    canonical_connection,
) -> None:
    with canonical_connection.begin():
        selected = _qualified(canonical_connection)
        selected_handoff = _handoff(canonical_connection, selected)
        _seed_stage_a(canonical_connection, selected_handoff)
        unrelated = _qualified(canonical_connection)
        _seed_stage_a(canonical_connection, _handoff(canonical_connection, unrelated))
        receipt = inspect_phase9_readiness(
            canonical_connection,
            qualification_handoff=selected_handoff,
            stage="NO_ORDER_SOAK",
            evaluated_at=datetime(2026, 8, 21, tzinfo=timezone.utc)
            + timedelta(seconds=20),
        )

    assert receipt.status == "BLOCKED"
    assert receipt.reason_codes == (
        "EXACT_ACTIVE_DEPLOYMENT_NOT_GLOBALLY_UNIQUE",
        "EXACT_HEALTHY_RUNTIME_NOT_GLOBALLY_UNIQUE",
    )


def test_no_order_soak_rejects_a_stale_runtime_heartbeat(canonical_connection) -> None:
    with canonical_connection.begin():
        qualification = _qualified(canonical_connection)
        handoff = _handoff(canonical_connection, qualification)
        _deployment_id, runtime_id = _seed_stage_a(canonical_connection, handoff)
        canonical_connection.execute(
            RUNTIME_RECEIPTS_TABLE.update()
            .where(RUNTIME_RECEIPTS_TABLE.c.runtime_instance_id == runtime_id)
            .values(observed_at=datetime(2026, 8, 20, tzinfo=timezone.utc))
        )
        receipt = inspect_phase9_readiness(
            canonical_connection,
            qualification_handoff=handoff,
            stage="NO_ORDER_SOAK",
            evaluated_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
        )

    assert receipt.status == "BLOCKED"
    assert "EXACT_PRODUCTION_RUNTIME_RECEIPT_EVIDENCE_UNSET" in receipt.reason_codes


def test_shadow_requires_exact_accepted_and_rejected_risk_with_zero_orders(
    canonical_connection,
) -> None:
    with canonical_connection.begin():
        qualification = _qualified(canonical_connection)
        handoff = _handoff(canonical_connection, qualification)
        deployment_id, runtime_id = _seed_stage_a(canonical_connection, handoff)
        _seed_stage_b(canonical_connection, handoff, deployment_id, runtime_id)
        receipt = inspect_phase9_readiness(
            canonical_connection,
            qualification_handoff=handoff,
            stage="SIGNAL_RISK_SHADOW",
            evaluated_at=datetime(2026, 8, 21, tzinfo=timezone.utc)
            + timedelta(seconds=20),
        )

    assert receipt.status == "READY"
    assert receipt.reason_codes == ()
    assert receipt.lineage_evidence_counts["signals"] == 2
    assert receipt.lineage_evidence_counts["risk_decisions"] == 2
    assert receipt.execution_domain_counts["orders"] == 0


def test_canary_and_recovery_prove_exact_single_writer_and_lifecycle_chain(
    canonical_connection,
) -> None:
    with canonical_connection.begin():
        qualification = _qualified(canonical_connection)
        handoff = _handoff(canonical_connection, qualification)
        deployment_id, runtime_id = _seed_stage_a(canonical_connection, handoff)
        accepted_risk_id, _rejected_risk_id = _seed_stage_b(
            canonical_connection, handoff, deployment_id, runtime_id
        )
        attestation = record_redacted_demo_attestation(
            canonical_connection,
            deployment_id=deployment_id,
            instrument="BTC-USDT-SWAP",
            account_fingerprint_digest="c" * 64,
            credential_generation_digest="d" * 64,
            permissions={"read": True, "trade": True, "withdraw": False},
            observed_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
            expires_at=datetime(2026, 8, 21, tzinfo=timezone.utc)
            + timedelta(seconds=60),
            evaluated_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
        )
        prepared = prepare_demo_order(
            canonical_connection,
            risk_decision_id=accepted_risk_id,
            attestation_id=attestation.attestation_id,
            writer_identity="canonical_order_writer",
            holder_identity="canonical-v13-order-writer-v1",
            holder_token_digest="e" * 64,
            idempotency_key="phase9-canary-persisted-chain",
            order_request={
                "instId": "BTC-USDT-SWAP",
                "tdMode": "isolated",
                "clOrdId": "v13readiness000000000000000001",
                "side": "buy",
                "posSide": "long",
                "ordType": "post_only",
                "sz": "1",
                "px": "10000",
            },
            evaluated_at=datetime(2026, 8, 21, tzinfo=timezone.utc)
            + timedelta(seconds=10),
        )
        order_id = prepared.order_id
        canonical_connection.execute(
            ORDERS_TABLE.update()
            .where(ORDERS_TABLE.c.id == order_id)
            .values(
                exchange_order_id="redacted-demo-order-identity",
                status="ACCEPTED",
                receipt_digest="f" * 64,
            )
        )
        fill_id = record_production_demo_fill(
            canonical_connection,
            order_id=order_id,
            exchange_fill_id="phase9-demo-fill",
            fill_json={
                "evidence_class": "PRODUCTION_OKX_DEMO",
                "allow_real_funds": False,
                "exchange_order_id": "redacted-demo-order-identity",
                "exchange_fill_id": "phase9-demo-fill",
                "amount": "0.001",
            },
        )
        ledger_id = post_production_demo_ledger_entry(
            canonical_connection,
            fill_id=fill_id,
            entry_key="phase9-demo-ledger",
            asset="BTC",
            amount=Decimal("0.001"),
            entry_type="DEMO_FILL",
        )
        reconcile_production_demo_chain(
            canonical_connection,
            order_id=order_id,
            fill_id=fill_id,
            ledger_entry_id=ledger_id,
        )
        canary = inspect_phase9_readiness(
            canonical_connection,
            qualification_handoff=handoff,
            stage="OKX_DEMO_CANARY",
            evaluated_at=datetime(2026, 8, 21, tzinfo=timezone.utc)
            + timedelta(seconds=20),
        )
        released = release_demo_order_writer_lease(
            canonical_connection,
            holder_identity="canonical-v13-order-writer-v1",
            holder_token_digest="e" * 64,
            evaluated_at=datetime(2026, 8, 21, tzinfo=timezone.utc)
            + timedelta(seconds=21),
        )
        assert released["repeat_noop"] is False
        runtime_receipt_digest = canonical_connection.execute(
            select(RUNTIME_RECEIPTS_TABLE.c.receipt_digest)
            .order_by(RUNTIME_RECEIPTS_TABLE.c.observed_at.desc())
            .limit(1)
        ).scalar_one()
        observed_at = datetime(2026, 8, 21, tzinfo=timezone.utc) + timedelta(seconds=22)
        acceptance_evidence = Phase9RecoveryAcceptance(
            qualification_decision_id=handoff.qualification_decision_id,
            runtime_restart=build_lifecycle_receipt(
                service_key="long_lived_runtime",
                action="RESTART",
                status="CONFIRMED",
                generation=1,
                observed_at=observed_at,
                plan_digest="a" * 64,
            ),
            runtime_recovery=build_lifecycle_receipt(
                service_key="long_lived_runtime",
                action="RECOVER",
                status="NO_OP",
                generation=1,
                observed_at=observed_at,
                plan_digest="a" * 64,
                details={"orphan_cleaned": False, "bootstrap_required": False},
            ),
            writer_stop=build_lifecycle_receipt(
                service_key="order_writer",
                action="STOP",
                status="STOPPED",
                generation=1,
                observed_at=observed_at,
                plan_digest="b" * 64,
            ),
            order_replay_receipt_digest="f" * 64,
            observability_receipt_digest=runtime_receipt_digest,
            active_supervisor_lease_count=0,
            zombie_process_count=0,
            observed_at=observed_at,
        )
        recovery_acceptance = record_phase9_recovery_acceptance(
            canonical_connection,
            evidence=acceptance_evidence,
            actor_identity="canonical-phase9-recovery-operator",
        )
        assert recovery_acceptance["repeat_noop"] is False
        repeated_acceptance = record_phase9_recovery_acceptance(
            canonical_connection,
            evidence=acceptance_evidence,
            actor_identity="canonical-phase9-recovery-operator",
        )
        assert repeated_acceptance["repeat_noop"] is True
        assert (
            repeated_acceptance["receipt_digest"]
            == recovery_acceptance["receipt_digest"]
        )
        recovery = inspect_phase9_readiness(
            canonical_connection,
            qualification_handoff=handoff,
            stage="RECOVERY_SOAK",
            evaluated_at=datetime(2026, 8, 21, tzinfo=timezone.utc)
            + timedelta(seconds=20),
        )

    assert canary.lineage_evidence_counts["orders"] == 1
    assert canary.lineage_evidence_counts["fills"] == 1
    assert canary.lineage_evidence_counts["ledger_entries"] == 1
    assert canary.lineage_evidence_counts["reconciliation_items"] == 1
    assert canary.status == "READY", canary.reason_codes
    assert canary.reason_codes == ()
    assert recovery.status == "READY", recovery.reason_codes
    assert recovery.reason_codes == ()
    assert recovery.lineage_evidence_counts["recovery_acceptance_receipts"] == 1


def test_unknown_phase9_stage_fails_closed(canonical_connection) -> None:
    missing = Phase9QualificationHandoff(
        qualification_decision_id=uuid4(),
        qualification_decision_digest="0" * 64,
        strategy_version_id=uuid4(),
        research_target_id=uuid4(),
        configuration_bundle_id=uuid4(),
        configuration_bundle_digest="1" * 64,
        market_snapshot_id=uuid4(),
        market_snapshot_digest="2" * 64,
        validation_plan_id=uuid4(),
        validation_plan_digest="3" * 64,
    )
    with pytest.raises(CanonicalPhase9ReadinessBlocked, match="BLOCKED_PHASE9_STAGE"):
        inspect_phase9_readiness(
            canonical_connection,
            qualification_handoff=missing,
            stage="DEMO_CANARY",
        )


def test_phase9_uniqueness_upgrade_is_additive_and_exact() -> None:
    upgrade = render_phase9_uniqueness_upgrade_sql()
    rollback = render_phase9_uniqueness_rollback_sql()
    for constraint, (table, columns) in PHASE9_UNIQUE_CONSTRAINTS.items():
        assert (
            f"ALTER TABLE strategy_platform_v13.{table} ADD CONSTRAINT "
            f"{constraint} UNIQUE ({', '.join(columns)})"
        ) in upgrade
        assert (
            f"ALTER TABLE strategy_platform_v13.{table} DROP CONSTRAINT {constraint}"
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
