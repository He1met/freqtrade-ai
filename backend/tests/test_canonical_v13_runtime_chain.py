from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.canonical_v13.deployment_approval import approve_demo_deployment
from app.canonical_v13.deployment_control import (
    CanonicalDeploymentBlocked,
    confirm_production_demo_runtime_observation,
    confirm_production_demo_runtime_stop_observation,
    create_demo_deployment,
    disable_demo_deployment,
    launch_demo_runtime,
)
from app.canonical_v13.accounting import post_simulated_ledger_entry
from app.canonical_v13.execution_common import CanonicalExecutionChainBlocked
from app.canonical_v13.fill_service import record_simulated_fill
from app.canonical_v13.order_service import record_simulated_order
from app.canonical_v13.reconciliation import reconcile_simulated_chain
from app.canonical_v13.risk_service import (
    create_simulated_intent,
    decide_simulated_risk,
)
from app.canonical_v13.signal_service import record_simulated_signal
from app.canonical_v13.models import (
    DEPLOYMENTS_TABLE,
    DEPLOYMENT_APPROVALS_TABLE,
    FILLS_TABLE,
    LEDGER_ENTRIES_TABLE,
    ORDERS_TABLE,
    QUALIFICATION_DECISIONS_TABLE,
    RECONCILIATION_RUNS_TABLE,
    RISK_DECISIONS_TABLE,
    RUNTIME_INSTANCES_TABLE,
    RUNTIME_RECEIPTS_TABLE,
    SIGNALS_TABLE,
    TRADE_INTENTS_TABLE,
    VALIDATION_PLANS_TABLE,
)
from app.canonical_v13.research_evaluation import qualify_target, score_target
from app.canonical_v13.runtime_contract import (
    CanonicalRuntimeContractBlocked,
    FrozenRuntimeLaunchSpec,
    assess_runtime_observation,
    build_runtime_observation_receipt,
)
from tests.test_canonical_v13_research_evaluation import (
    NOW,
    _passing_metrics,
    _validated_attempt,
    canonical_connection,
)


def _count(connection, table) -> int:
    return int(connection.execute(select(func.count()).select_from(table)).scalar_one())


def _qualified(connection):
    plan_id, attempt_id = _validated_attempt(
        connection, metrics_by_window=_passing_metrics()
    )
    score_target(
        connection,
        validation_plan_id=plan_id,
        validation_attempt_id=attempt_id,
        scorer_identity="isolated-scorer-v1",
    )
    decision = qualify_target(
        connection,
        validation_plan_id=plan_id,
        validation_attempt_id=attempt_id,
        qualifier_identity="isolated-qualifier-v1",
    )
    return plan_id, decision


class TestLauncher:
    evidence_class = "TEST_SIMULATED"

    def launch(self, spec: FrozenRuntimeLaunchSpec):
        return build_runtime_observation_receipt(
            runtime_instance_id=uuid4(),
            launch_spec=spec,
            status="HEALTHY",
            observed_at=NOW,
            evidence_class=self.evidence_class,
        )


class CountingLauncher(TestLauncher):
    def __init__(self) -> None:
        self.calls = 0

    def launch(self, spec: FrozenRuntimeLaunchSpec):
        self.calls += 1
        return super().launch(spec)


def _runtime_fixture(connection):
    plan_id, decision = _qualified(connection)
    approval = approve_demo_deployment(
        connection,
        qualification_decision_id=decision.qualification_decision_id,
        actor_identity="isolated-human-approver",
        reason="isolated contract acceptance only",
    )
    deployment = create_demo_deployment(
        connection, deployment_approval_id=approval.deployment_approval_id
    )
    runtime_id = launch_demo_runtime(
        connection,
        deployment_id=deployment.deployment_id,
        runtime_identity="isolated-long-lived-runtime-fixture",
        image_digest="f" * 64,
        service_account="canonical_runtime_reader",
        credential_reference="test-reference-never-resolved",
        launcher=TestLauncher(),
    )
    plan = (
        connection.execute(
            select(VALIDATION_PLANS_TABLE).where(VALIDATION_PLANS_TABLE.c.id == plan_id)
        )
        .mappings()
        .one()
    )
    return plan, decision, deployment, runtime_id


def test_simulator_runtime_never_activates_deployment_or_becomes_production_ready(
    canonical_connection,
):
    with canonical_connection.begin():
        _plan, decision, deployment, runtime_id = _runtime_fixture(canonical_connection)
        deployment_status = canonical_connection.execute(
            select(DEPLOYMENTS_TABLE.c.status).where(
                DEPLOYMENTS_TABLE.c.id == deployment.deployment_id
            )
        ).scalar_one()
        runtime = (
            canonical_connection.execute(
                select(RUNTIME_INSTANCES_TABLE).where(
                    RUNTIME_INSTANCES_TABLE.c.id == runtime_id
                )
            )
            .mappings()
            .one()
        )
    assert decision.status == "QUALIFIED"
    assert deployment_status == "PENDING"
    assert runtime["status"] == "HEALTHY"
    assert runtime["order_writer_capability"] is False

    spec = FrozenRuntimeLaunchSpec(
        deployment_id=deployment.deployment_id,
        approval_id=uuid4(),
        qualification_decision_id=decision.qualification_decision_id,
        strategy_version_id=uuid4(),
        configuration_bundle_id=uuid4(),
        configuration_bundle_digest="1" * 64,
        market_snapshot_id=uuid4(),
        market_snapshot_digest="2" * 64,
        deployment_capability_digest="3" * 64,
        runtime_identity="isolated-long-lived-runtime-fixture",
        image_digest="f" * 64,
        service_account="canonical_runtime_reader",
        network_policy="DEMO_EXCHANGE_ONLY",
        credential_reference="test-reference-never-resolved",
    )
    receipt = build_runtime_observation_receipt(
        runtime_instance_id=runtime_id,
        launch_spec=spec,
        status="HEALTHY",
        observed_at=NOW,
        evidence_class="TEST_SIMULATED",
    )
    status, reasons = assess_runtime_observation(
        receipt,
        evaluated_at=NOW + timedelta(minutes=1),
        maximum_heartbeat_age=timedelta(minutes=5),
    )
    assert status == "BLOCKED"
    assert reasons == ("RUNTIME_EVIDENCE_NOT_PRODUCTION",)

    with pytest.raises(
        CanonicalRuntimeContractBlocked, match="BLOCKED_RUNTIME_CAPABILITY_DRIFT"
    ):
        assess_runtime_observation(
            build_runtime_observation_receipt(
                runtime_instance_id=runtime_id,
                launch_spec=FrozenRuntimeLaunchSpec(
                    **{
                        **spec.__dict__,
                        "service_account": "canonical_validation_writer",
                    }
                ),
                status="HEALTHY",
                observed_at=NOW,
                evidence_class="TEST_SIMULATED",
            ),
            evaluated_at=NOW,
            maximum_heartbeat_age=timedelta(minutes=5),
        )


def test_supervisor_observation_confirms_runtime_without_launching_inside_db_transaction(
    canonical_connection,
) -> None:
    with canonical_connection.begin():
        _plan_id, decision = _qualified(canonical_connection)
        approval = approve_demo_deployment(
            canonical_connection,
            qualification_decision_id=decision.qualification_decision_id,
            actor_identity="phase9-human-approver",
            reason="reviewed production Demo runtime",
        )
        deployment = create_demo_deployment(
            canonical_connection,
            deployment_approval_id=approval.deployment_approval_id,
        )
        persisted_deployment = (
            canonical_connection.execute(
                select(DEPLOYMENTS_TABLE).where(
                    DEPLOYMENTS_TABLE.c.id == deployment.deployment_id
                )
            )
            .mappings()
            .one()
        )
        runtime_id = uuid4()
        spec = FrozenRuntimeLaunchSpec(
            deployment_id=deployment.deployment_id,
            approval_id=approval.deployment_approval_id,
            qualification_decision_id=decision.qualification_decision_id,
            strategy_version_id=persisted_deployment["strategy_version_id"],
            configuration_bundle_id=persisted_deployment["configuration_bundle_id"],
            configuration_bundle_digest=persisted_deployment[
                "configuration_bundle_digest"
            ],
            market_snapshot_id=persisted_deployment["market_snapshot_id"],
            market_snapshot_digest=persisted_deployment["market_snapshot_digest"],
            deployment_capability_digest=deployment.capability_digest,
            runtime_identity="canonical-v13-long-lived-runtime-v1",
            image_digest="f" * 64,
            service_account="canonical_runtime_reader",
            network_policy="DEMO_EXCHANGE_ONLY",
            credential_reference="keychain:freqtrade-ai/v13/okx-demo",
        )
        receipt = build_runtime_observation_receipt(
            runtime_instance_id=runtime_id,
            launch_spec=spec,
            status="HEALTHY",
            observed_at=NOW,
            evidence_class="PRODUCTION_DEMO_RUNTIME",
        )
        confirmed = confirm_production_demo_runtime_observation(
            canonical_connection,
            deployment_id=deployment.deployment_id,
            runtime_identity=spec.runtime_identity,
            image_digest=spec.image_digest,
            credential_reference=spec.credential_reference or "",
            receipt=receipt,
            evaluated_at=NOW + timedelta(seconds=1),
        )
        replayed = confirm_production_demo_runtime_observation(
            canonical_connection,
            deployment_id=deployment.deployment_id,
            runtime_identity=spec.runtime_identity,
            image_digest=spec.image_digest,
            credential_reference=spec.credential_reference or "",
            receipt=receipt,
            evaluated_at=NOW + timedelta(seconds=1),
        )
        status = canonical_connection.execute(
            select(DEPLOYMENTS_TABLE.c.status).where(
                DEPLOYMENTS_TABLE.c.id == deployment.deployment_id
            )
        ).scalar_one()

    assert confirmed == replayed == runtime_id
    assert status == "ACTIVE"


def test_supervisor_stop_observation_is_immutable_and_replay_safe(
    canonical_connection,
) -> None:
    with canonical_connection.begin():
        _plan_id, decision = _qualified(canonical_connection)
        approval = approve_demo_deployment(
            canonical_connection,
            qualification_decision_id=decision.qualification_decision_id,
            actor_identity="phase9-human-approver",
            reason="reviewed production Demo runtime",
        )
        deployment = create_demo_deployment(
            canonical_connection,
            deployment_approval_id=approval.deployment_approval_id,
        )
        row = canonical_connection.execute(
            select(DEPLOYMENTS_TABLE).where(
                DEPLOYMENTS_TABLE.c.id == deployment.deployment_id
            )
        ).mappings().one()
        runtime_id = uuid4()
        spec = FrozenRuntimeLaunchSpec(
            deployment_id=deployment.deployment_id,
            approval_id=approval.deployment_approval_id,
            qualification_decision_id=decision.qualification_decision_id,
            strategy_version_id=row["strategy_version_id"],
            configuration_bundle_id=row["configuration_bundle_id"],
            configuration_bundle_digest=row["configuration_bundle_digest"],
            market_snapshot_id=row["market_snapshot_id"],
            market_snapshot_digest=row["market_snapshot_digest"],
            deployment_capability_digest=deployment.capability_digest,
            runtime_identity="canonical-v13-long-lived-runtime-v1",
            image_digest="f" * 64,
            service_account="canonical_runtime_reader",
            network_policy="DEMO_EXCHANGE_ONLY",
            credential_reference="none:public-okx-market-only",
        )
        running = build_runtime_observation_receipt(
            runtime_instance_id=runtime_id,
            launch_spec=spec,
            status="HEALTHY",
            observed_at=NOW,
            evidence_class="PRODUCTION_DEMO_RUNTIME",
        )
        confirm_production_demo_runtime_observation(
            canonical_connection,
            deployment_id=deployment.deployment_id,
            runtime_identity=spec.runtime_identity,
            image_digest=spec.image_digest,
            credential_reference=spec.credential_reference or "",
            receipt=running,
            evaluated_at=NOW + timedelta(seconds=1),
        )
        stopped = build_runtime_observation_receipt(
            runtime_instance_id=runtime_id,
            launch_spec=spec,
            status="STOPPED",
            observed_at=NOW + timedelta(seconds=2),
            evidence_class="PRODUCTION_DEMO_RUNTIME_STOP",
        )
        with pytest.raises(
            CanonicalDeploymentBlocked, match="BLOCKED_RUNTIME_STOP_RECEIPT_DRIFT"
        ):
            confirm_production_demo_runtime_stop_observation(
                canonical_connection,
                deployment_id=deployment.deployment_id,
                receipt=replace(stopped, receipt_digest="0" * 64),
                evaluated_at=NOW + timedelta(seconds=3),
            )
        first = confirm_production_demo_runtime_stop_observation(
            canonical_connection,
            deployment_id=deployment.deployment_id,
            receipt=stopped,
            evaluated_at=NOW + timedelta(seconds=3),
        )
        replay = confirm_production_demo_runtime_stop_observation(
            canonical_connection,
            deployment_id=deployment.deployment_id,
            receipt=stopped,
            evaluated_at=NOW + timedelta(seconds=4),
        )
        resumed = build_runtime_observation_receipt(
            runtime_instance_id=runtime_id,
            launch_spec=spec,
            status="HEALTHY",
            observed_at=NOW + timedelta(seconds=5),
            evidence_class="PRODUCTION_DEMO_RUNTIME",
        )
        assert (
            confirm_production_demo_runtime_observation(
                canonical_connection,
                deployment_id=deployment.deployment_id,
                runtime_identity=spec.runtime_identity,
                image_digest=spec.image_digest,
                credential_reference=spec.credential_reference or "",
                receipt=resumed,
                evaluated_at=NOW + timedelta(seconds=6),
            )
            == runtime_id
        )
        runtime_status = canonical_connection.execute(
            select(RUNTIME_INSTANCES_TABLE.c.status).where(
                RUNTIME_INSTANCES_TABLE.c.id == runtime_id
            )
        ).scalar_one()
        receipt_count = _count(canonical_connection, RUNTIME_RECEIPTS_TABLE)

    assert first.status == replay.status == "STOPPED"
    assert runtime_status == "HEALTHY"
    assert first.repeat_noop is False
    assert replay.repeat_noop is True
    assert replay.receipt_digest == first.receipt_digest
    assert receipt_count == 3


def test_supervisor_observation_rolls_stable_runtime_to_new_accepted_plan(
    canonical_connection,
) -> None:
    with canonical_connection.begin():
        _plan_id, decision = _qualified(canonical_connection)
        approval = approve_demo_deployment(
            canonical_connection,
            qualification_decision_id=decision.qualification_decision_id,
            actor_identity="phase9-human-approver",
            reason="reviewed production Demo runtime rollover",
        )
        deployment = create_demo_deployment(
            canonical_connection,
            deployment_approval_id=approval.deployment_approval_id,
        )
        persisted = canonical_connection.execute(
            select(DEPLOYMENTS_TABLE).where(
                DEPLOYMENTS_TABLE.c.id == deployment.deployment_id
            )
        ).mappings().one()
        runtime_id = uuid4()

        def observe(image_digest: str, observed_at):
            spec = FrozenRuntimeLaunchSpec(
                deployment_id=deployment.deployment_id,
                approval_id=approval.deployment_approval_id,
                qualification_decision_id=decision.qualification_decision_id,
                strategy_version_id=persisted["strategy_version_id"],
                configuration_bundle_id=persisted["configuration_bundle_id"],
                configuration_bundle_digest=persisted["configuration_bundle_digest"],
                market_snapshot_id=persisted["market_snapshot_id"],
                market_snapshot_digest=persisted["market_snapshot_digest"],
                deployment_capability_digest=deployment.capability_digest,
                runtime_identity="canonical-v13-long-lived-runtime-v1",
                image_digest=image_digest,
                service_account="canonical_runtime_reader",
                network_policy="DEMO_EXCHANGE_ONLY",
                credential_reference="none:public-okx-market-only",
            )
            receipt = build_runtime_observation_receipt(
                runtime_instance_id=runtime_id,
                launch_spec=spec,
                status="HEALTHY",
                observed_at=observed_at,
                evidence_class="PRODUCTION_DEMO_RUNTIME",
            )
            return confirm_production_demo_runtime_observation(
                canonical_connection,
                deployment_id=deployment.deployment_id,
                runtime_identity=spec.runtime_identity,
                image_digest=image_digest,
                credential_reference=spec.credential_reference or "",
                receipt=receipt,
                evaluated_at=observed_at + timedelta(seconds=1),
            )

        assert observe("f" * 64, NOW) == runtime_id
        assert observe("e" * 64, NOW + timedelta(seconds=2)) == runtime_id
        assert observe("e" * 64, NOW + timedelta(seconds=2)) == runtime_id
        current = canonical_connection.execute(
            select(RUNTIME_INSTANCES_TABLE).where(
                RUNTIME_INSTANCES_TABLE.c.id == runtime_id
            )
        ).mappings().one()
        receipts = canonical_connection.execute(
            select(RUNTIME_RECEIPTS_TABLE.c.launch_spec_digest).where(
                RUNTIME_RECEIPTS_TABLE.c.runtime_instance_id == runtime_id
            )
        ).scalars().all()

    assert current["image_digest"] == "e" * 64
    assert len(receipts) == 2
    assert len(set(receipts)) == 2


def test_stopped_runtime_identity_rolls_to_a_new_deployment_without_rewriting_history(
    canonical_connection,
) -> None:
    runtime_identity = "canonical-v13-long-lived-runtime-v1"
    with canonical_connection.begin():
        _first_plan_id, first_decision = _qualified(canonical_connection)
        first_approval = approve_demo_deployment(
            canonical_connection,
            qualification_decision_id=first_decision.qualification_decision_id,
            actor_identity="phase9-rollover-approver",
            reason="first acceptance-only runtime",
        )
        first_deployment = create_demo_deployment(
            canonical_connection,
            deployment_approval_id=first_approval.deployment_approval_id,
        )
        first_row = canonical_connection.execute(
            select(DEPLOYMENTS_TABLE).where(
                DEPLOYMENTS_TABLE.c.id == first_deployment.deployment_id
            )
        ).mappings().one()
        first_runtime_id = uuid4()
        first_spec = FrozenRuntimeLaunchSpec(
            deployment_id=first_deployment.deployment_id,
            approval_id=first_approval.deployment_approval_id,
            qualification_decision_id=first_decision.qualification_decision_id,
            strategy_version_id=first_row["strategy_version_id"],
            configuration_bundle_id=first_row["configuration_bundle_id"],
            configuration_bundle_digest=first_row["configuration_bundle_digest"],
            market_snapshot_id=first_row["market_snapshot_id"],
            market_snapshot_digest=first_row["market_snapshot_digest"],
            deployment_capability_digest=first_deployment.capability_digest,
            runtime_identity=runtime_identity,
            image_digest="f" * 64,
            service_account="canonical_runtime_reader",
            network_policy="DEMO_EXCHANGE_ONLY",
            credential_reference="none:public-okx-market-only",
        )
        first_running = build_runtime_observation_receipt(
            runtime_instance_id=first_runtime_id,
            launch_spec=first_spec,
            status="HEALTHY",
            observed_at=NOW,
            evidence_class="PRODUCTION_DEMO_RUNTIME",
        )
        confirm_production_demo_runtime_observation(
            canonical_connection,
            deployment_id=first_deployment.deployment_id,
            runtime_identity=runtime_identity,
            image_digest=first_spec.image_digest,
            credential_reference=first_spec.credential_reference or "",
            receipt=first_running,
            evaluated_at=NOW + timedelta(seconds=1),
        )
        first_stopped = build_runtime_observation_receipt(
            runtime_instance_id=first_runtime_id,
            launch_spec=first_spec,
            status="STOPPED",
            observed_at=NOW + timedelta(seconds=2),
            evidence_class="PRODUCTION_DEMO_RUNTIME_STOP",
        )
        confirm_production_demo_runtime_stop_observation(
            canonical_connection,
            deployment_id=first_deployment.deployment_id,
            receipt=first_stopped,
            evaluated_at=NOW + timedelta(seconds=3),
        )

        _second_plan_id, second_decision = _qualified(canonical_connection)
        second_approval = approve_demo_deployment(
            canonical_connection,
            qualification_decision_id=second_decision.qualification_decision_id,
            actor_identity="phase9-rollover-approver",
            reason="second acceptance-only runtime",
        )
        disable_demo_deployment(
            canonical_connection,
            deployment_id=first_deployment.deployment_id,
            superseded_by_qualification_decision_id=(
                second_decision.qualification_decision_id
            ),
            actor_identity="phase9-rollover-operator",
            reason="preserve stopped runtime and roll to exact new qualification",
        )
        second_deployment = create_demo_deployment(
            canonical_connection,
            deployment_approval_id=second_approval.deployment_approval_id,
        )
        second_row = canonical_connection.execute(
            select(DEPLOYMENTS_TABLE).where(
                DEPLOYMENTS_TABLE.c.id == second_deployment.deployment_id
            )
        ).mappings().one()
        second_runtime_id = uuid4()
        second_spec = FrozenRuntimeLaunchSpec(
            deployment_id=second_deployment.deployment_id,
            approval_id=second_approval.deployment_approval_id,
            qualification_decision_id=second_decision.qualification_decision_id,
            strategy_version_id=second_row["strategy_version_id"],
            configuration_bundle_id=second_row["configuration_bundle_id"],
            configuration_bundle_digest=second_row["configuration_bundle_digest"],
            market_snapshot_id=second_row["market_snapshot_id"],
            market_snapshot_digest=second_row["market_snapshot_digest"],
            deployment_capability_digest=second_deployment.capability_digest,
            runtime_identity=runtime_identity,
            image_digest="e" * 64,
            service_account="canonical_runtime_reader",
            network_policy="DEMO_EXCHANGE_ONLY",
            credential_reference="none:public-okx-market-only",
        )
        second_running = build_runtime_observation_receipt(
            runtime_instance_id=second_runtime_id,
            launch_spec=second_spec,
            status="HEALTHY",
            observed_at=NOW + timedelta(seconds=5),
            evidence_class="PRODUCTION_DEMO_RUNTIME",
        )
        assert (
            confirm_production_demo_runtime_observation(
                canonical_connection,
                deployment_id=second_deployment.deployment_id,
                runtime_identity=runtime_identity,
                image_digest=second_spec.image_digest,
                credential_reference=second_spec.credential_reference or "",
                receipt=second_running,
                evaluated_at=NOW + timedelta(seconds=6),
            )
            == second_runtime_id
        )
        rows = canonical_connection.execute(
            select(RUNTIME_INSTANCES_TABLE).where(
                RUNTIME_INSTANCES_TABLE.c.runtime_identity == runtime_identity
            ).order_by(RUNTIME_INSTANCES_TABLE.c.created_at)
        ).mappings().all()

    assert [row["id"] for row in rows] == [first_runtime_id, second_runtime_id]
    assert [row["deployment_id"] for row in rows] == [
        first_deployment.deployment_id,
        second_deployment.deployment_id,
    ]
    assert [row["status"] for row in rows] == ["STOPPED", "HEALTHY"]


def test_writer_separated_simulated_chain_and_reconciliation(canonical_connection):
    with canonical_connection.begin():
        plan, _decision, deployment, runtime_id = _runtime_fixture(canonical_connection)
        signal_id = record_simulated_signal(
            canonical_connection,
            deployment_id=deployment.deployment_id,
            runtime_instance_id=runtime_id,
            research_target_id=plan["research_target_id"],
            signal_json={"evidence_class": "TEST_SIMULATED", "side": "buy"},
        )
        assert (
            record_simulated_signal(
                canonical_connection,
                deployment_id=deployment.deployment_id,
                runtime_instance_id=runtime_id,
                research_target_id=plan["research_target_id"],
                signal_json={"evidence_class": "TEST_SIMULATED", "side": "buy"},
            )
            == signal_id
        )
        intent_id = create_simulated_intent(
            canonical_connection,
            signal_id=signal_id,
            intent_json={
                "evidence_class": "TEST_SIMULATED",
                "quantity": "0.001",
            },
        )
        assert (
            create_simulated_intent(
                canonical_connection,
                signal_id=signal_id,
                intent_json={
                    "evidence_class": "TEST_SIMULATED",
                    "quantity": "0.001",
                },
            )
            == intent_id
        )
        risk_id = decide_simulated_risk(
            canonical_connection,
            trade_intent_id=intent_id,
            accepted=True,
            policy_snapshot_digest="3" * 64,
        )
        assert (
            decide_simulated_risk(
                canonical_connection,
                trade_intent_id=intent_id,
                accepted=True,
                policy_snapshot_digest="3" * 64,
            )
            == risk_id
        )
        order_id = record_simulated_order(
            canonical_connection,
            risk_decision_id=risk_id,
            writer_identity="canonical_order_writer",
            idempotency_key="isolated-order-one",
            outcome="ACCEPTED",
        )
        fill_id = record_simulated_fill(
            canonical_connection,
            order_id=order_id,
            exchange_fill_id="isolated-fill-one",
            fill_json={"evidence_class": "TEST_SIMULATED", "amount": "0.001"},
        )
        ledger_id = post_simulated_ledger_entry(
            canonical_connection,
            fill_id=fill_id,
            entry_key="isolated-ledger-one",
            asset="BTC",
            amount=Decimal("0.001"),
            entry_type="DEMO_FILL",
        )
        reconciliation_id = reconcile_simulated_chain(
            canonical_connection,
            order_id=order_id,
            fill_id=fill_id,
            ledger_entry_id=ledger_id,
        )
        assert (
            record_simulated_fill(
                canonical_connection,
                order_id=order_id,
                exchange_fill_id="isolated-fill-one",
                fill_json={"evidence_class": "TEST_SIMULATED", "amount": "0.001"},
            )
            == fill_id
        )
        assert (
            post_simulated_ledger_entry(
                canonical_connection,
                fill_id=fill_id,
                entry_key="isolated-ledger-one",
                asset="BTC",
                amount=Decimal("0.001"),
                entry_type="DEMO_FILL",
            )
            == ledger_id
        )
        assert (
            reconcile_simulated_chain(
                canonical_connection,
                order_id=order_id,
                fill_id=fill_id,
                ledger_entry_id=ledger_id,
            )
            == reconciliation_id
        )
    assert reconciliation_id
    for table in (
        SIGNALS_TABLE,
        TRADE_INTENTS_TABLE,
        RISK_DECISIONS_TABLE,
        ORDERS_TABLE,
        FILLS_TABLE,
        LEDGER_ENTRIES_TABLE,
        RECONCILIATION_RUNS_TABLE,
    ):
        assert _count(canonical_connection, table) == 1


def test_uncertain_order_is_terminally_blocked_from_fill_and_retry_is_idempotent(
    canonical_connection,
):
    with canonical_connection.begin():
        plan, _decision, deployment, runtime_id = _runtime_fixture(canonical_connection)
        signal_id = record_simulated_signal(
            canonical_connection,
            deployment_id=deployment.deployment_id,
            runtime_instance_id=runtime_id,
            research_target_id=plan["research_target_id"],
            signal_json={"evidence_class": "TEST_SIMULATED", "side": "buy"},
        )
        intent_id = create_simulated_intent(
            canonical_connection,
            signal_id=signal_id,
            intent_json={"evidence_class": "TEST_SIMULATED", "quantity": "0.001"},
        )
        risk_id = decide_simulated_risk(
            canonical_connection,
            trade_intent_id=intent_id,
            accepted=True,
            policy_snapshot_digest="3" * 64,
        )
        order_id = record_simulated_order(
            canonical_connection,
            risk_decision_id=risk_id,
            writer_identity="canonical_order_writer",
            idempotency_key="isolated-uncertain-order",
            outcome="UNCERTAIN",
        )
        repeated = record_simulated_order(
            canonical_connection,
            risk_decision_id=risk_id,
            writer_identity="canonical_order_writer",
            idempotency_key="isolated-uncertain-order",
            outcome="UNCERTAIN",
        )
        with pytest.raises(CanonicalExecutionChainBlocked) as blocked:
            record_simulated_fill(
                canonical_connection,
                order_id=order_id,
                exchange_fill_id="forbidden-fill",
                fill_json={"evidence_class": "TEST_SIMULATED"},
            )
    assert repeated == order_id
    assert blocked.value.code == "BLOCKED_ACCEPTED_ORDER_REQUIRED"
    assert _count(canonical_connection, ORDERS_TABLE) == 1
    assert _count(canonical_connection, FILLS_TABLE) == 0
    assert _count(canonical_connection, LEDGER_ENTRIES_TABLE) == 0


def test_approval_deployment_and_runtime_exact_replay_are_noops(
    canonical_connection,
):
    with canonical_connection.begin():
        _plan_id, decision = _qualified(canonical_connection)
        approval = approve_demo_deployment(
            canonical_connection,
            qualification_decision_id=decision.qualification_decision_id,
            actor_identity="isolated-human-approver",
            reason="isolated contract acceptance only",
        )
        repeated_approval = approve_demo_deployment(
            canonical_connection,
            qualification_decision_id=decision.qualification_decision_id,
            actor_identity="isolated-human-approver",
            reason="isolated contract acceptance only",
        )
        deployment = create_demo_deployment(
            canonical_connection,
            deployment_approval_id=approval.deployment_approval_id,
        )
        repeated_deployment = create_demo_deployment(
            canonical_connection,
            deployment_approval_id=approval.deployment_approval_id,
        )
        launcher = CountingLauncher()
        runtime_id = launch_demo_runtime(
            canonical_connection,
            deployment_id=deployment.deployment_id,
            runtime_identity="isolated-long-lived-runtime-replay",
            image_digest="f" * 64,
            service_account="canonical_runtime_reader",
            credential_reference="test-reference-never-resolved",
            launcher=launcher,
        )
        canonical_connection.execute(
            DEPLOYMENTS_TABLE.update()
            .where(DEPLOYMENTS_TABLE.c.id == deployment.deployment_id)
            .values(status="ACTIVE")
        )
        repeated_runtime_id = launch_demo_runtime(
            canonical_connection,
            deployment_id=deployment.deployment_id,
            runtime_identity="isolated-long-lived-runtime-replay",
            image_digest="f" * 64,
            service_account="canonical_runtime_reader",
            credential_reference="test-reference-never-resolved",
            launcher=launcher,
        )

    assert repeated_approval == approval
    assert repeated_deployment == deployment
    assert repeated_runtime_id == runtime_id
    assert launcher.calls == 1
    assert _count(canonical_connection, DEPLOYMENT_APPROVALS_TABLE) == 1
    assert _count(canonical_connection, DEPLOYMENTS_TABLE) == 1
    assert _count(canonical_connection, RUNTIME_INSTANCES_TABLE) == 1
    assert _count(canonical_connection, RUNTIME_RECEIPTS_TABLE) == 1


def test_noncanonical_order_writer_identity_is_blocked(canonical_connection):
    with canonical_connection.begin():
        plan, _decision, deployment, runtime_id = _runtime_fixture(canonical_connection)
        signal_id = record_simulated_signal(
            canonical_connection,
            deployment_id=deployment.deployment_id,
            runtime_instance_id=runtime_id,
            research_target_id=plan["research_target_id"],
            signal_json={"evidence_class": "TEST_SIMULATED", "side": "buy"},
        )
        intent_id = create_simulated_intent(
            canonical_connection,
            signal_id=signal_id,
            intent_json={"evidence_class": "TEST_SIMULATED", "quantity": "0.001"},
        )
        risk_id = decide_simulated_risk(
            canonical_connection,
            trade_intent_id=intent_id,
            accepted=True,
            policy_snapshot_digest="3" * 64,
        )
        with pytest.raises(
            CanonicalExecutionChainBlocked,
            match="BLOCKED_NON_CANONICAL_ORDER_WRITER",
        ):
            record_simulated_order(
                canonical_connection,
                risk_decision_id=risk_id,
                writer_identity="runtime-self-reported-writer",
                idempotency_key="forbidden-writer",
                outcome="ACCEPTED",
            )
    assert _count(canonical_connection, ORDERS_TABLE) == 0


def test_bare_active_rows_cannot_fake_runtime_ready(canonical_connection):
    from app.canonical_v13.api import _runtime_readiness

    with canonical_connection.begin():
        _plan, _decision, deployment, runtime_id = _runtime_fixture(
            canonical_connection
        )
        canonical_connection.execute(
            DEPLOYMENTS_TABLE.update()
            .where(DEPLOYMENTS_TABLE.c.id == deployment.deployment_id)
            .values(status="ACTIVE")
        )
        projection = _runtime_readiness(canonical_connection)
    assert projection.status == "BLOCKED"
    assert "RUNTIME_RECEIPT_CAPABILITY_DRIFT" in projection.reason_codes


def test_future_production_shaped_heartbeat_remains_blocked(canonical_connection):
    from app.canonical_v13.api import _runtime_readiness

    with canonical_connection.begin():
        _plan, _decision, deployment_result, runtime_id = _runtime_fixture(
            canonical_connection
        )
        deployment = (
            canonical_connection.execute(
                select(DEPLOYMENTS_TABLE).where(
                    DEPLOYMENTS_TABLE.c.id == deployment_result.deployment_id
                )
            )
            .mappings()
            .one()
        )
        approval = (
            canonical_connection.execute(
                select(DEPLOYMENT_APPROVALS_TABLE).where(
                    DEPLOYMENT_APPROVALS_TABLE.c.id
                    == deployment["deployment_approval_id"]
                )
            )
            .mappings()
            .one()
        )
        qualification = (
            canonical_connection.execute(
                select(QUALIFICATION_DECISIONS_TABLE).where(
                    QUALIFICATION_DECISIONS_TABLE.c.id
                    == approval["qualification_decision_id"]
                )
            )
            .mappings()
            .one()
        )
        runtime = (
            canonical_connection.execute(
                select(RUNTIME_INSTANCES_TABLE).where(
                    RUNTIME_INSTANCES_TABLE.c.id == runtime_id
                )
            )
            .mappings()
            .one()
        )
        spec = FrozenRuntimeLaunchSpec(
            deployment_id=deployment["id"],
            approval_id=approval["id"],
            qualification_decision_id=qualification["id"],
            strategy_version_id=deployment["strategy_version_id"],
            configuration_bundle_id=deployment["configuration_bundle_id"],
            configuration_bundle_digest=deployment["configuration_bundle_digest"],
            market_snapshot_id=deployment["market_snapshot_id"],
            market_snapshot_digest=deployment["market_snapshot_digest"],
            deployment_capability_digest=deployment["capability_digest"],
            runtime_identity=runtime["runtime_identity"],
            image_digest=runtime["image_digest"],
            service_account=runtime["service_account"],
            network_policy=runtime["network_policy"],
            credential_reference=runtime["credential_reference"],
        )
        future = NOW + timedelta(days=3650)
        receipt = build_runtime_observation_receipt(
            runtime_instance_id=runtime_id,
            launch_spec=spec,
            status="HEALTHY",
            observed_at=future,
            evidence_class="PRODUCTION_DEMO_RUNTIME",
        )
        canonical_connection.execute(
            DEPLOYMENTS_TABLE.update()
            .where(DEPLOYMENTS_TABLE.c.id == deployment["id"])
            .values(status="ACTIVE")
        )
        canonical_connection.execute(
            RUNTIME_RECEIPTS_TABLE.update()
            .where(RUNTIME_RECEIPTS_TABLE.c.runtime_instance_id == runtime_id)
            .values(
                evidence_class=receipt.evidence_class,
                observed_at=receipt.observed_at,
                observation_json={"future-test": True},
                observation_digest=receipt.observation_digest,
                receipt_digest=receipt.receipt_digest,
            )
        )
        projection = _runtime_readiness(canonical_connection)
    assert projection.status == "BLOCKED"
    assert "RUNTIME_HEARTBEAT_IN_FUTURE" in projection.reason_codes
