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
from app.canonical_v13.acceptance_signal_trigger import (
    SOURCE_KIND as ACCEPTANCE_SOURCE_KIND,
    build_acceptance_worker_receipt,
    issue_acceptance_signal_trigger,
    persist_acceptance_signal,
)
from app.canonical_v13.accounting import (
    post_production_demo_ledger_entry,
    post_simulated_ledger_entry,
)
from app.canonical_v13.deployment_approval import approve_demo_deployment
from app.canonical_v13.deployment_control import (
    CanonicalDeploymentBlocked,
    create_demo_deployment,
    disable_demo_deployment,
    launch_demo_runtime,
)
from app.canonical_v13.fill_service import (
    record_production_demo_fill,
    record_simulated_fill,
)
from app.canonical_v13.models import (
    AUDIT_EVENTS_TABLE,
    DEPLOYMENTS_TABLE,
    EXECUTION_CANARY_RISK_POLICIES_TABLE,
    EXECUTION_CANARY_PROBE_RECEIPTS_TABLE,
    ORDERS_TABLE,
    QUALIFICATION_DECISIONS_TABLE,
    RECONCILIATION_RUNS_TABLE,
    RISK_DECISIONS_TABLE,
    RUNTIME_INSTANCES_TABLE,
    RUNTIME_RECEIPTS_TABLE,
    SIGNALS_TABLE,
)
from app.canonical_v13.order_service import record_simulated_order
from app.canonical_v13.phase9_execution_authority import (
    authorize_demo_risk_budget,
    decide_central_demo_risk,
    decide_signal_risk_shadow,
    record_redacted_demo_attestation,
)
from app.canonical_v13.phase9_order_writer import (
    _claim_dispatch,
    _persist_exchange_receipt,
    canary_client_order_id,
    prepare_demo_order,
    release_demo_order_writer_lease,
)
from app.canonical_v13.phase9_production_runtime import ReleaseBoundReceiptSeal
from app.canonical_v13.phase9_recovery_acceptance import (
    CanonicalPhase9RecoveryAcceptanceBlocked,
    Phase9RecoveryAcceptance,
    record_phase9_recovery_acceptance,
)
from app.canonical_v13.phase9_readiness import (
    EXECUTION_DOMAIN_TABLE_NAMES,
    CanonicalPhase9ReadinessBlocked,
    Phase9QualificationHandoff,
    _canonical_order_writer_lease_digest,
    inspect_phase9_readiness,
)
from app.canonical_v13.phase9_canary_policy import terminate_canary_risk_policy
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
from tests.test_canonical_v13_acceptance_signal_trigger import (
    NOW as ACCEPTANCE_NOW,
    _refresh_runtime as _refresh_acceptance_runtime,
    _seed_runtime as _seed_acceptance_runtime,
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


def test_order_writer_lease_digest_is_database_timezone_independent() -> None:
    expires_utc = datetime(2026, 8, 25, 6, 30, 21, tzinfo=timezone.utc)
    common = {
        "holder_identity": "canonical-v13-order-writer-v1",
        "holder_token_digest": "a" * 64,
        "generation": 1,
    }
    expected = _json_digest(
        {
            "execution_target": "OKX_DEMO",
            **common,
            "expires_at": expires_utc.isoformat(),
        }
    )

    assert (
        _canonical_order_writer_lease_digest(
            {**common, "expires_at": expires_utc}
        )
        == expected
    )
    assert (
        _canonical_order_writer_lease_digest(
            {
                **common,
                "expires_at": expires_utc.astimezone(
                    timezone(timedelta(hours=8))
                ),
            }
        )
        == expected
    )


def _seed_runtime_for_deployment(
    connection,
    handoff: Phase9QualificationHandoff,
    deployment_id,
):
    runtime_id = launch_demo_runtime(
        connection,
        deployment_id=deployment_id,
        runtime_identity=f"phase9-runtime-{handoff.qualification_decision_id}",
        image_digest="f" * 64,
        service_account="canonical_runtime_reader",
        credential_reference="test-reference-never-resolved",
        launcher=_TestLauncher(),
    )
    connection.execute(
        DEPLOYMENTS_TABLE.update()
        .where(DEPLOYMENTS_TABLE.c.id == deployment_id)
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
    return runtime["id"]


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
    runtime_id = _seed_runtime_for_deployment(
        connection,
        handoff,
        deployment.deployment_id,
    )
    return deployment.deployment_id, runtime_id


def _seed_stage_b(
    connection,
    handoff,
    deployment_id,
    runtime_id,
    *,
    base_time=datetime(2026, 8, 21, tzinfo=timezone.utc),
):
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
            "shadow_case": 1,
        },
        evaluated_at=base_time + timedelta(seconds=1),
    )
    exchange_body = {
        "instId": "BTC-USDT-SWAP",
        "tdMode": "isolated",
        "clOrdId": "v13readiness000000000000000001",
        "side": "buy",
        "posSide": "long",
        "ordType": "post_only",
        "sz": "1",
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
            "notional": "10",
            "exchange_body": exchange_body,
        },
    )
    risk_id = decide_signal_risk_shadow(
        connection,
        trade_intent_id=intent_id,
        evaluated_at=base_time + timedelta(seconds=3),
    ).risk_decision_id
    return [risk_id]


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


def test_qualification_handoff_allows_only_terminal_historical_deployment_lineage(
    canonical_connection,
) -> None:
    with canonical_connection.begin():
        historical = _qualified(canonical_connection)
        successor = _qualified(canonical_connection)
        historical_handoff = _handoff(canonical_connection, historical)
        deployment_id, runtime_id = _seed_stage_a(
            canonical_connection, historical_handoff
        )
        blocked = inspect_phase9_readiness(
            canonical_connection,
            qualification_handoff=_handoff(canonical_connection, successor),
        )
        successor_approval = approve_demo_deployment(
            canonical_connection,
            qualification_decision_id=successor.qualification_decision_id,
            actor_identity="operator:phase9-test",
            reason="approved successor remains blocked until old disable",
        )
        with pytest.raises(
            CanonicalDeploymentBlocked,
            match="BLOCKED_NONTERMINAL_DEPLOYMENT_PRESENT",
        ):
            create_demo_deployment(
                canonical_connection,
                deployment_approval_id=successor_approval.deployment_approval_id,
            )
        with pytest.raises(
            CanonicalDeploymentBlocked,
            match="BLOCKED_DEPLOYMENT_RUNTIME_NOT_STOPPED",
        ):
            disable_demo_deployment(
                canonical_connection,
                deployment_id=deployment_id,
                superseded_by_qualification_decision_id=(
                    successor.qualification_decision_id
                ),
                actor_identity="operator:phase9-test",
                reason="supersede stopped historical lineage",
            )
        canonical_connection.execute(
            RUNTIME_INSTANCES_TABLE.update()
            .where(RUNTIME_INSTANCES_TABLE.c.id == runtime_id)
            .values(status="STOPPED")
        )
        disabled = disable_demo_deployment(
            canonical_connection,
            deployment_id=deployment_id,
            superseded_by_qualification_decision_id=(
                successor.qualification_decision_id
            ),
            actor_identity="operator:phase9-test",
            reason="supersede stopped historical lineage",
        )
        repeated = disable_demo_deployment(
            canonical_connection,
            deployment_id=deployment_id,
            superseded_by_qualification_decision_id=(
                successor.qualification_decision_id
            ),
            actor_identity="operator:phase9-test",
            reason="supersede stopped historical lineage",
        )
        ready = inspect_phase9_readiness(
            canonical_connection,
            qualification_handoff=_handoff(canonical_connection, successor),
        )

    assert blocked.status == "BLOCKED"
    assert blocked.reason_codes == ("NONTERMINAL_DEPLOYMENT_PRESENT=1",)
    assert disabled.status == "DISABLED"
    assert disabled.repeat_noop is False
    assert repeated.receipt_digest == disabled.receipt_digest
    assert repeated.repeat_noop is True
    assert ready.status == "READY"
    assert ready.reason_codes == ()
    assert ready.execution_domain_counts["deployment_approvals"] == 2
    assert ready.execution_domain_counts["deployments"] == 1
    assert ready.execution_domain_counts["runtime_instances"] == 1
    assert ready.execution_domain_counts["runtime_receipts"] == 1


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


def test_no_order_soak_prevents_an_unrelated_second_active_runtime(
    canonical_connection,
) -> None:
    with canonical_connection.begin():
        selected = _qualified(canonical_connection)
        selected_handoff = _handoff(canonical_connection, selected)
        _seed_stage_a(canonical_connection, selected_handoff)
        unrelated = _qualified(canonical_connection)
        with pytest.raises(
            CanonicalDeploymentBlocked,
            match="BLOCKED_NONTERMINAL_DEPLOYMENT_PRESENT",
        ):
            _seed_stage_a(canonical_connection, _handoff(canonical_connection, unrelated))
        receipt = inspect_phase9_readiness(
            canonical_connection,
            qualification_handoff=selected_handoff,
            stage="NO_ORDER_SOAK",
            evaluated_at=datetime(2026, 8, 21, tzinfo=timezone.utc)
            + timedelta(seconds=20),
        )

    assert receipt.status == "READY"
    assert receipt.reason_codes == ()


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


def test_shadow_requires_one_receipt_with_accepted_and_rejected_checks(
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
    assert receipt.lineage_evidence_counts["signals"] == 1
    assert receipt.lineage_evidence_counts["trade_intents"] == 1
    assert receipt.lineage_evidence_counts["risk_decisions"] == 1
    assert receipt.execution_domain_counts["execution_canary_probe_receipts"] == 0
    assert receipt.execution_domain_counts["execution_canary_risk_policies"] == 0
    assert receipt.execution_domain_counts["execution_risk_budget_authorizations"] == 0
    assert receipt.execution_domain_counts["execution_risk_reservations"] == 0
    assert receipt.execution_domain_counts["orders"] == 0


def test_shadow_selects_acceptance_signal_when_natural_signal_coexists(
    canonical_connection,
) -> None:
    with canonical_connection.begin():
        approval, deployment, spec, runtime_id, image_id, qualification = (
            _seed_acceptance_runtime(canonical_connection)
        )
        handoff = _handoff(
            canonical_connection,
            SimpleNamespace(qualification_decision_id=qualification["id"]),
        )
        issued = issue_acceptance_signal_trigger(
            canonical_connection,
            qualification_decision_id=qualification["id"],
            deployment_approval_id=approval.deployment_approval_id,
            deployment_id=deployment.deployment_id,
            runtime_instance_id=runtime_id,
            runtime_image_acceptance_id=image_id,
            actor_identity="operator:isolated",
            idempotency_key="readiness-natural-acceptance-coexistence",
            issued_at=ACCEPTANCE_NOW + timedelta(seconds=30),
        )
        _refresh_acceptance_runtime(
            canonical_connection,
            deployment=deployment,
            spec=spec,
            runtime_id=runtime_id,
            observed_at=issued.scheduled_at,
        )
        record_production_demo_signal(
            canonical_connection,
            deployment_id=deployment.deployment_id,
            runtime_instance_id=runtime_id,
            research_target_id=handoff.research_target_id,
            signal_json={
                "evidence_class": "PRODUCTION_OKX_DEMO",
                "natural_signal": True,
                "allow_real_funds": False,
                "configuration_bundle_digest": handoff.configuration_bundle_digest,
                "market_snapshot_digest": handoff.market_snapshot_digest,
                "side": "buy",
                "coincident_natural_evidence": True,
            },
            evaluated_at=issued.scheduled_at,
        )
        seal = ReleaseBoundReceiptSeal(
            "d" * 64, "secret-safe-signing-key-" + "x" * 48
        )
        worker = build_acceptance_worker_receipt(
            canonical_connection,
            trigger_id=issued.trigger_id,
            plan_digest="e" * 64,
            observed_at=issued.scheduled_at,
            signer=seal,
        )
        persisted = persist_acceptance_signal(
            canonical_connection,
            trigger_id=issued.trigger_id,
            worker_receipt=worker,
            verifier=seal,
            persisted_at=issued.scheduled_at,
        )
        intent_id = create_production_demo_intent(
            canonical_connection,
            signal_id=persisted.signal_id,
            intent_json={
                "contract": "canonical-v13-demo-trade-intent-v1",
                "execution_target": "OKX_DEMO",
                "allow_real_funds": False,
                "acceptance_only": True,
                "source_kind": ACCEPTANCE_SOURCE_KIND,
                "signal_digest": persisted.signal_digest,
                "instrument": "BTC-USDT-SWAP",
                "notional": "1",
                "exchange_body": {
                    "instId": "BTC-USDT-SWAP",
                    "tdMode": "isolated",
                    "side": "buy",
                    "posSide": "long",
                },
            },
        )
        decide_signal_risk_shadow(
            canonical_connection,
            trade_intent_id=intent_id,
            evaluated_at=issued.scheduled_at + timedelta(seconds=1),
        )
        receipt = inspect_phase9_readiness(
            canonical_connection,
            qualification_handoff=handoff,
            stage="SIGNAL_RISK_SHADOW",
            evaluated_at=issued.scheduled_at + timedelta(seconds=20),
        )

    assert receipt.status == "READY"
    assert receipt.reason_codes == ()
    assert receipt.lineage_evidence_counts["signals"] == 2
    assert receipt.lineage_evidence_counts["acceptance_signal_triggers"] == 1
    assert receipt.lineage_evidence_counts["trade_intents"] == 1
    assert receipt.lineage_evidence_counts["risk_decisions"] == 1
    assert receipt.execution_domain_counts["orders"] == 0


def test_shadow_digest_drift_blocks_readiness(canonical_connection) -> None:
    with canonical_connection.begin():
        qualification = _qualified(canonical_connection)
        handoff = _handoff(canonical_connection, qualification)
        deployment_id, runtime_id = _seed_stage_a(canonical_connection, handoff)
        risk_ids = _seed_stage_b(
            canonical_connection, handoff, deployment_id, runtime_id
        )
        canonical_connection.execute(
            RISK_DECISIONS_TABLE.update()
            .where(RISK_DECISIONS_TABLE.c.id == risk_ids[0])
            .values(decision_digest="0" * 64)
        )
        receipt = inspect_phase9_readiness(
            canonical_connection,
            qualification_handoff=handoff,
            stage="SIGNAL_RISK_SHADOW",
            evaluated_at=datetime(2026, 8, 21, tzinfo=timezone.utc)
            + timedelta(seconds=20),
        )

    assert receipt.status == "BLOCKED"
    assert "EXACT_SINGLE_SHADOW_DECISION_RECEIPT_REQUIRED" in receipt.reason_codes


def _historical_fixture_canary_and_recovery_chain(
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
            evidence=replace(
                acceptance_evidence,
                observed_at=acceptance_evidence.observed_at + timedelta(seconds=1),
            ),
            actor_identity="canonical-phase9-recovery-operator",
        )
        assert repeated_acceptance["repeat_noop"] is True
        assert (
            repeated_acceptance["receipt_digest"]
            == recovery_acceptance["receipt_digest"]
        )
        with pytest.raises(
            CanonicalPhase9RecoveryAcceptanceBlocked,
            match="BLOCKED_RECOVERY_REPLAY_DRIFT",
        ):
            record_phase9_recovery_acceptance(
                canonical_connection,
                evidence=replace(
                    acceptance_evidence,
                    observed_at=acceptance_evidence.observed_at
                    + timedelta(seconds=2),
                ),
                actor_identity="different-phase9-recovery-operator",
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


def test_shadow_acceptance_cannot_satisfy_canary_execution_authority(
    canonical_connection,
) -> None:
    with canonical_connection.begin():
        qualification = _qualified(canonical_connection)
        handoff = _handoff(canonical_connection, qualification)
        deployment_id, runtime_id = _seed_stage_a(canonical_connection, handoff)
        _seed_stage_b(canonical_connection, handoff, deployment_id, runtime_id)
        canary = inspect_phase9_readiness(
            canonical_connection,
            qualification_handoff=handoff,
            stage="OKX_DEMO_CANARY",
            evaluated_at=datetime(2026, 8, 21, tzinfo=timezone.utc)
            + timedelta(seconds=20),
        )

    assert canary.status == "BLOCKED"
    assert "EXACT_RISK_ACCEPTED_EVIDENCE_UNSET" in canary.reason_codes
    assert "EXACT_SINGLE_EXECUTION_RISK_RESERVATION_REQUIRED" in canary.reason_codes
    assert canary.execution_domain_counts["execution_canary_probe_receipts"] == 0
    assert canary.execution_domain_counts["orders"] == 0


def test_canary_readiness_recomputes_sealed_policy_and_execution_reservation(
    canonical_connection,
) -> None:
    from tests.test_canonical_v13_phase9_canary_policy import _authorize, _fixture

    with canonical_connection.begin():
        decision, approval, probe, probe_receipt = _fixture(canonical_connection)
        handoff = Phase9QualificationHandoff(
            qualification_decision_id=decision["id"],
            qualification_decision_digest=decision["decision_digest"],
            strategy_version_id=decision["strategy_version_id"],
            research_target_id=decision["research_target_id"],
            configuration_bundle_id=decision["configuration_bundle_id"],
            configuration_bundle_digest=decision["configuration_bundle_digest"],
            market_snapshot_id=decision["market_snapshot_id"],
            market_snapshot_digest=decision["market_snapshot_digest"],
            validation_plan_id=decision["validation_plan_id"],
            validation_plan_digest=decision["validation_plan_digest"],
        )
        deployment = (
            canonical_connection.execute(select(DEPLOYMENTS_TABLE)).mappings().one()
        )
        runtime_id = canonical_connection.execute(
            select(RUNTIME_INSTANCES_TABLE.c.id)
        ).scalar_one()
        _seed_stage_b(
            canonical_connection,
            handoff,
            deployment["id"],
            runtime_id,
            base_time=probe.observed_at,
        )
        policy = _authorize(canonical_connection, decision, approval, probe_receipt)
        persisted_policy = (
            canonical_connection.execute(
                select(EXECUTION_CANARY_RISK_POLICIES_TABLE).where(
                    EXECUTION_CANARY_RISK_POLICIES_TABLE.c.id == policy.policy_id
                )
            )
            .mappings()
            .one()
        )
        budget = authorize_demo_risk_budget(
            canonical_connection,
            deployment_approval_id=approval.deployment_approval_id,
            actor_identity="phase9-human-policy-owner",
            reason="freeze exact one-shot canary policy",
            policy_source_receipt_digest=policy.receipt_digest,
            evaluated_at=probe.observed_at,
        )
        signal_id = record_production_demo_signal(
            canonical_connection,
            deployment_id=deployment["id"],
            runtime_instance_id=runtime_id,
            research_target_id=handoff.research_target_id,
            signal_json={
                "evidence_class": "PRODUCTION_OKX_DEMO",
                "natural_signal": True,
                "allow_real_funds": False,
                "configuration_bundle_digest": handoff.configuration_bundle_digest,
                "market_snapshot_digest": handoff.market_snapshot_digest,
                "side": "buy",
                "execution_case": 1,
            },
            evaluated_at=probe.observed_at + timedelta(seconds=3),
        )
        signal_digest = canonical_connection.execute(
            select(SIGNALS_TABLE.c.signal_digest).where(SIGNALS_TABLE.c.id == signal_id)
        ).scalar_one()
        execution_body = {
            "instId": "BTC-USDT-SWAP",
            "tdMode": "isolated",
            "side": "buy",
            "posSide": "long",
            "ordType": "limit",
            "sz": str(persisted_policy["minimum_contract_size"]),
            "px": str(persisted_policy["limit_price"]),
        }
        intent_id = create_production_demo_intent(
            canonical_connection,
            signal_id=signal_id,
            intent_mode="EXECUTION",
            intent_json={
                "contract": "canonical-v13-demo-trade-intent-v1",
                "execution_target": "OKX_DEMO",
                "allow_real_funds": False,
                "signal_digest": signal_digest,
                "instrument": "BTC-USDT-SWAP",
                "notional": str(
                    Decimal(str(persisted_policy["minimum_contract_size"]))
                    * Decimal(str(persisted_policy["contract_value"]))
                    * Decimal(str(persisted_policy["mark_price"]))
                ),
                "exchange_body": execution_body,
            },
        )
        execution = decide_central_demo_risk(
            canonical_connection,
            trade_intent_id=intent_id,
            risk_budget_authorization_id=budget.authorization_id,
            evaluated_at=probe.observed_at + timedelta(seconds=4),
        )
        receipt = inspect_phase9_readiness(
            canonical_connection,
            qualification_handoff=handoff,
            stage="OKX_DEMO_CANARY",
            evaluated_at=probe.observed_at + timedelta(seconds=20),
        )
        from tests.test_canonical_v13_phase9_order_writer import FakeTransport

        prepared = prepare_demo_order(
            canonical_connection,
            risk_decision_id=execution.risk_decision_id,
            attestation_id=persisted_policy["execution_attestation_id"],
            writer_identity="canonical_order_writer",
            holder_identity="canonical-v13-order-writer-v1",
            holder_token_digest="f" * 64,
            idempotency_key="phase9-readiness-exact-post",
            order_request=execution_body,
            evaluated_at=probe.observed_at + timedelta(seconds=5),
        )
        transport = FakeTransport()
        guard = transport.dispatch_guard(
            instrument=execution_body["instId"],
            limit_price=execution_body["px"],
            effective_leverage=str(persisted_policy["effective_leverage"]),
            minimum_size=execution_body["sz"],
        )
        guard = replace(
            guard,
            account_fingerprint_digest=probe.account_fingerprint_digest,
            credential_generation_digest=probe.credential_generation_digest,
            leverage_digest=_json_digest(
                {
                    "execution_target": "OKX_DEMO",
                    "resource": "leverage",
                    "source": "okx_demo_rest",
                    "authenticated": True,
                    "observed_at": guard.leverage_observed_at.isoformat(),
                    "expires_at": guard.leverage_expires_at.isoformat(),
                    "facts": {
                        "instrument": "BTC-USDT-SWAP",
                        "account_fingerprint_digest": probe.account_fingerprint_digest,
                        "long": guard.effective_leverage,
                        "short": guard.current_short_leverage,
                    },
                }
            ),
        )
        _claim_dispatch(
            canonical_connection,
            order_id=prepared.order_id,
            holder_identity="canonical-v13-order-writer-v1",
            holder_token_digest="f" * 64,
            lease_generation=prepared.lease_generation,
            guard=guard,
            evaluated_at=probe.observed_at + timedelta(seconds=6),
        )
        order = _persist_exchange_receipt(
            canonical_connection,
            order_id=prepared.order_id,
            exchange_order_id="readiness-demo-order",
            safe_response={
                "ordId": "readiness-demo-order",
                "clOrdId": canary_client_order_id(execution.risk_decision_id),
                "sCode": "0",
            },
            outcome_mode="POST",
        )
        fill_id = record_production_demo_fill(
            canonical_connection,
            order_id=prepared.order_id,
            exchange_fill_id="readiness-demo-fill",
            fill_json={
                "contract": "canonical-v13-okx-demo-fill-evidence-v1",
                "evidence_class": "PRODUCTION_OKX_DEMO",
                "allow_real_funds": False,
                "instrument": "BTC-USDT-SWAP",
                "exchange_order_id": order.exchange_order_id,
                "exchange_fill_id": "readiness-demo-fill",
                "bill_id": "readiness-demo-bill",
                "price": execution_body["px"],
                "size": "1",
                "fee": "-0.01",
                "timestamp": "1787292000000",
                "side": "buy",
                "position_side": "long",
                "requested_size": execution_body["sz"],
            },
        )
        ledger_id = post_production_demo_ledger_entry(
            canonical_connection,
            fill_id=fill_id,
            entry_key="okx-demo-fill:readiness-demo-fill:long-contracts",
            asset="BTC-USDT-SWAP",
            amount=Decimal("1"),
            entry_type="OKX_DEMO_LONG_FILL_CONTRACTS",
        )
        run_id = reconcile_production_demo_chain(
            canonical_connection,
            order_id=prepared.order_id,
            fill_id=fill_id,
            ledger_entry_id=ledger_id,
        )
        completed = inspect_phase9_readiness(
            canonical_connection,
            qualification_handoff=handoff,
            stage="OKX_DEMO_CANARY",
            # The sealed probe and attestation are expired here; the claim-time
            # windows, not current time, remain the acceptance authority.
            evaluated_at=probe.observed_at + timedelta(minutes=2),
        )
        terminated = terminate_canary_risk_policy(
            canonical_connection,
            policy_id=policy.policy_id,
            reconciliation_run_id=run_id,
            actor_identity="phase9-human-policy-owner",
            evaluated_at=probe.observed_at + timedelta(minutes=2, seconds=1),
        )
        release_demo_order_writer_lease(
            canonical_connection,
            holder_identity="canonical-v13-order-writer-v1",
            holder_token_digest="f" * 64,
            evaluated_at=probe.observed_at + timedelta(minutes=2, seconds=2),
        )
        runtime_receipt_digest = canonical_connection.execute(
            select(RUNTIME_RECEIPTS_TABLE.c.receipt_digest)
            .order_by(RUNTIME_RECEIPTS_TABLE.c.observed_at.desc())
            .limit(1)
        ).scalar_one()
        recovery_at = probe.observed_at + timedelta(minutes=2, seconds=3)
        recovery_evidence = Phase9RecoveryAcceptance(
            qualification_decision_id=handoff.qualification_decision_id,
            runtime_restart=build_lifecycle_receipt(
                service_key="long_lived_runtime",
                action="RESTART",
                status="CONFIRMED",
                generation=2,
                observed_at=recovery_at,
                plan_digest="a" * 64,
            ),
            runtime_recovery=build_lifecycle_receipt(
                service_key="long_lived_runtime",
                action="RECOVER",
                status="NO_OP",
                generation=2,
                observed_at=recovery_at,
                plan_digest="a" * 64,
            ),
            writer_stop=build_lifecycle_receipt(
                service_key="order_writer",
                action="STOP",
                status="STOPPED",
                generation=1,
                observed_at=recovery_at,
                plan_digest="b" * 64,
            ),
            order_replay_receipt_digest=order.receipt_digest,
            observability_receipt_digest=runtime_receipt_digest,
            policy_termination_receipt_digest=terminated.termination_digest,
            active_supervisor_lease_count=0,
            zombie_process_count=0,
            observed_at=recovery_at,
        )
        record_phase9_recovery_acceptance(
            canonical_connection,
            evidence=recovery_evidence,
            actor_identity="canonical-phase9-recovery-operator",
        )
        recovery = inspect_phase9_readiness(
            canonical_connection,
            qualification_handoff=handoff,
            stage="RECOVERY_SOAK",
            evaluated_at=recovery_at + timedelta(seconds=1),
        )
        stray_run_id = uuid4()
        stray_scope_digest = "1" * 64
        canonical_connection.execute(
            RECONCILIATION_RUNS_TABLE.insert().values(
                id=stray_run_id,
                status="SUCCEEDED",
                scope_digest=stray_scope_digest,
                receipt_digest=_json_digest(
                    {
                        "run_id": str(stray_run_id),
                        "scope_digest": stray_scope_digest,
                    }
                ),
                created_at=recovery_at,
                completed_at=recovery_at,
            )
        )
        stray = inspect_phase9_readiness(
            canonical_connection,
            qualification_handoff=handoff,
            stage="RECOVERY_SOAK",
            evaluated_at=recovery_at + timedelta(seconds=1),
        )
        canonical_connection.execute(
            EXECUTION_CANARY_PROBE_RECEIPTS_TABLE.update()
            .where(
                EXECUTION_CANARY_PROBE_RECEIPTS_TABLE.c.id
                == probe_receipt.probe_receipt_id
            )
            .values(instrument_digest="0" * 64)
        )
        drifted = inspect_phase9_readiness(
            canonical_connection,
            qualification_handoff=handoff,
            stage="OKX_DEMO_CANARY",
            evaluated_at=probe.observed_at + timedelta(seconds=20),
        )

    assert execution.status == "RISK_ACCEPTED", execution.reason_code
    assert receipt.lineage_evidence_counts["execution_canary_probe_receipts"] == 1
    assert receipt.lineage_evidence_counts["execution_canary_risk_policies"] == 1
    assert receipt.lineage_evidence_counts["execution_risk_reservations"] == 1
    assert "CANONICAL_RISK_POLICY_LINEAGE_UNSET" not in receipt.reason_codes
    assert receipt.status == "BLOCKED"
    assert "EXACT_SINGLE_OKX_DEMO_ORDER_EVIDENCE_UNSET" in receipt.reason_codes
    assert completed.status == "READY", completed.reason_codes
    assert "CANONICAL_RISK_POLICY_PROBE_VALIDATION_BLOCKED" not in (
        completed.reason_codes
    )
    assert "EXACT_OKX_DEMO_ATTESTATION_EVIDENCE_UNSET" not in completed.reason_codes
    assert recovery.status == "READY", recovery.reason_codes
    assert recovery.lineage_evidence_counts["reconciliation_runs"] == 1
    assert recovery.lineage_evidence_counts["recovery_acceptance_receipts"] == 1
    assert stray.status == "BLOCKED"
    assert "UNRELATED_RECONCILIATION_RUNS_EVIDENCE_PRESENT" in stray.reason_codes
    assert "CANONICAL_RISK_POLICY_PROBE_VALIDATION_BLOCKED" in drifted.reason_codes


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
