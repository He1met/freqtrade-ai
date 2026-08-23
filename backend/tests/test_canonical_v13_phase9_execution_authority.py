from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select

from app.canonical_v13.deployment_approval import approve_demo_deployment
from app.canonical_v13.deployment_control import (
    confirm_production_demo_runtime_observation,
    create_demo_deployment,
)
from app.canonical_v13.execution_common import CanonicalExecutionChainBlocked
from app.canonical_v13.models import (
    DEPLOYMENT_APPROVALS_TABLE,
    DEPLOYMENTS_TABLE,
    EXECUTION_CANARY_PROBE_RECEIPTS_TABLE,
    EXECUTION_CANARY_RISK_POLICIES_TABLE,
    EXECUTION_RISK_BUDGET_AUTHORIZATIONS_TABLE,
    EXECUTION_RISK_RESERVATIONS_TABLE,
    QUALIFICATION_DECISIONS_TABLE,
    RESEARCH_TARGETS_TABLE,
    RISK_DECISIONS_TABLE,
    STRATEGY_VERSIONS_TABLE,
    TRADE_INTENTS_TABLE,
)
from app.canonical_v13.phase9_execution_authority import (
    authorize_demo_risk_budget,
    decide_central_demo_risk,
    decide_signal_risk_shadow,
    record_redacted_demo_attestation,
)
from app.canonical_v13.execution_common import canonical_execution_digest
from app.canonical_v13.risk_service import create_production_demo_intent
from app.canonical_v13.runtime_contract import (
    FrozenRuntimeLaunchSpec,
    build_runtime_observation_receipt,
)
from app.canonical_v13.signal_service import record_production_demo_signal
from tests.test_canonical_v13_research_evaluation import (
    NOW,
    canonical_connection,  # noqa: F401, F811 - registers the shared fixture
)
from tests.test_canonical_v13_runtime_chain import _qualified


def _runtime_spec(connection, approval, deployment) -> FrozenRuntimeLaunchSpec:
    from app.canonical_v13.models import DEPLOYMENTS_TABLE, DEPLOYMENT_APPROVALS_TABLE

    persisted = (
        connection.execute(
            select(DEPLOYMENTS_TABLE).where(
                DEPLOYMENTS_TABLE.c.id == deployment.deployment_id
            )
        )
        .mappings()
        .one()
    )
    persisted_approval = (
        connection.execute(
            select(DEPLOYMENT_APPROVALS_TABLE).where(
                DEPLOYMENT_APPROVALS_TABLE.c.id == approval.deployment_approval_id
            )
        )
        .mappings()
        .one()
    )
    return FrozenRuntimeLaunchSpec(
        deployment_id=deployment.deployment_id,
        approval_id=approval.deployment_approval_id,
        qualification_decision_id=persisted_approval["qualification_decision_id"],
        strategy_version_id=persisted["strategy_version_id"],
        configuration_bundle_id=persisted["configuration_bundle_id"],
        configuration_bundle_digest=persisted["configuration_bundle_digest"],
        market_snapshot_id=persisted["market_snapshot_id"],
        market_snapshot_digest=persisted["market_snapshot_digest"],
        deployment_capability_digest=persisted["capability_digest"],
        runtime_identity="isolated-canonical-production-runtime",
        image_digest="a" * 64,
        service_account="canonical_runtime_reader",
        network_policy="DEMO_EXCHANGE_ONLY",
        credential_reference="keychain:freqtrade-ai/okx-demo-read",
    )


def _production_chain(
    connection,
    *,
    intent_mode="EXECUTION",
    create_intent=True,
    runtime_observed_at=NOW,
    signal_runtime_receipt_digest=None,
):
    plan_id, decision = _qualified(connection)
    approval = approve_demo_deployment(
        connection,
        qualification_decision_id=decision.qualification_decision_id,
        actor_identity="isolated-human-owner",
        reason="exact Phase 9 isolated contract",
    )
    deployment = create_demo_deployment(
        connection, deployment_approval_id=approval.deployment_approval_id
    )
    spec = _runtime_spec(connection, approval, deployment)
    receipt = build_runtime_observation_receipt(
        runtime_instance_id=uuid4(),
        launch_spec=spec,
        status="HEALTHY",
        observed_at=runtime_observed_at,
        evidence_class="PRODUCTION_DEMO_RUNTIME",
    )
    runtime_id = confirm_production_demo_runtime_observation(
        connection,
        deployment_id=deployment.deployment_id,
        runtime_identity=spec.runtime_identity,
        image_digest=spec.image_digest,
        credential_reference=spec.credential_reference,
        receipt=receipt,
        evaluated_at=runtime_observed_at,
    )
    from app.canonical_v13.models import VALIDATION_PLANS_TABLE

    plan = (
        connection.execute(
            select(VALIDATION_PLANS_TABLE).where(VALIDATION_PLANS_TABLE.c.id == plan_id)
        )
        .mappings()
        .one()
    )
    signal_payload = {
        "evidence_class": "PRODUCTION_OKX_DEMO",
        "natural_signal": True,
        "allow_real_funds": False,
        "configuration_bundle_digest": plan["configuration_bundle_digest"],
        "market_snapshot_digest": plan["market_snapshot_digest"],
        "runtime_receipt_digest": (
            signal_runtime_receipt_digest or receipt.receipt_digest
        ),
        "side": "buy",
    }
    signal_id = record_production_demo_signal(
        connection,
        deployment_id=deployment.deployment_id,
        runtime_instance_id=runtime_id,
        research_target_id=plan["research_target_id"],
        signal_json=signal_payload,
        evaluated_at=NOW + timedelta(seconds=1),
        runtime_liveness_observed_at=NOW + timedelta(seconds=1),
    )
    from app.canonical_v13.models import SIGNALS_TABLE

    signal_digest = connection.execute(
        select(SIGNALS_TABLE.c.signal_digest).where(SIGNALS_TABLE.c.id == signal_id)
    ).scalar_one()
    intent_id = (
        create_production_demo_intent(
            connection,
            signal_id=signal_id,
            intent_mode=intent_mode,
            intent_json={
            "contract": "canonical-v13-demo-trade-intent-v1",
            "execution_target": "OKX_DEMO",
            "allow_real_funds": False,
            "signal_digest": signal_digest,
            "instrument": "BTC-USDT-SWAP",
            "notional": "10",
            "exchange_body": {
                "instId": "BTC-USDT-SWAP",
                "tdMode": "isolated",
                "clOrdId": "v13canary00000000000000000001",
                "side": "buy",
                "posSide": "long",
                "ordType": "limit",
                "sz": "1",
                "px": "10000",
            },
            },
        )
        if create_intent
        else None
    )
    return approval, deployment, runtime_id, intent_id, None


def _risk_policy_source(
    connection,
    approval,
    *,
    max_notional="10",
    max_order_count=1,
    policy_digest="b" * 64,
    accepted_at=NOW,
):
    persisted_approval = (
        connection.execute(
            select(DEPLOYMENT_APPROVALS_TABLE).where(
                DEPLOYMENT_APPROVALS_TABLE.c.id == approval.deployment_approval_id
            )
        )
        .mappings()
        .one()
    )
    decision = (
        connection.execute(
            select(QUALIFICATION_DECISIONS_TABLE).where(
                QUALIFICATION_DECISIONS_TABLE.c.id
                == persisted_approval["qualification_decision_id"]
            )
        )
        .mappings()
        .one()
    )
    deployment = (
        connection.execute(
            select(DEPLOYMENTS_TABLE).where(
                DEPLOYMENTS_TABLE.c.deployment_approval_id
                == approval.deployment_approval_id
            )
        )
        .mappings()
        .one()
    )
    version = (
        connection.execute(
            select(STRATEGY_VERSIONS_TABLE).where(
                STRATEGY_VERSIONS_TABLE.c.id == decision["strategy_version_id"]
            )
        )
        .mappings()
        .one()
    )
    target = (
        connection.execute(
            select(RESEARCH_TARGETS_TABLE).where(
                RESEARCH_TARGETS_TABLE.c.id == decision["research_target_id"]
            )
        )
        .mappings()
        .one()
    )
    attestation = record_redacted_demo_attestation(
        connection,
        deployment_id=deployment["id"],
        instrument="BTC-USDT-SWAP",
        account_fingerprint_digest="c" * 64,
        credential_generation_digest="d" * 64,
        permissions={"read": True, "trade": True, "withdraw": False},
        observed_at=accepted_at,
        expires_at=accepted_at + timedelta(seconds=60),
        evaluated_at=accepted_at,
    )
    receipt_digest = canonical_execution_digest(
        {
            "qualification_decision_id": str(decision["id"]),
            "deployment_approval_id": str(approval.deployment_approval_id),
            "policy_digest": policy_digest,
            "accepted_at": accepted_at.isoformat(),
        }
    )
    probe_receipt_id = uuid4()
    connection.execute(
        EXECUTION_CANARY_PROBE_RECEIPTS_TABLE.insert().values(
            id=probe_receipt_id,
            deployment_id=deployment["id"],
            execution_attestation_id=attestation.attestation_id,
            execution_target="OKX_DEMO",
            instrument="BTC-USDT-SWAP",
            account_fingerprint_digest="c" * 64,
            credential_generation_digest="d" * 64,
            permissions_json={"read": True, "trade": True, "withdraw": False},
            simulated_trading=True,
            allow_real_funds=False,
            contract_value="0.001",
            contract_value_ccy="BTC",
            lot_size="1",
            minimum_size="1",
            tick_size="0.1",
            mark_price="10000",
            current_long_leverage="12",
            current_short_leverage="12",
            exchange_max_leverage="100",
            limit_price="10000",
            maximum_buy_contracts="2",
            long_contracts="0",
            short_contracts="0",
            active_position_count=0,
            pending_order_count=0,
            instrument_digest="1" * 64,
            instrument_observed_at=accepted_at,
            instrument_expires_at=accepted_at + timedelta(seconds=60),
            mark_price_digest="2" * 64,
            mark_price_observed_at=accepted_at,
            mark_price_expires_at=accepted_at + timedelta(seconds=60),
            account_config_digest="3" * 64,
            account_config_observed_at=accepted_at,
            account_config_expires_at=accepted_at + timedelta(seconds=60),
            leverage_digest="4" * 64,
            leverage_observed_at=accepted_at,
            leverage_expires_at=accepted_at + timedelta(seconds=60),
            exchange_max_leverage_digest="5" * 64,
            exchange_max_leverage_observed_at=accepted_at,
            exchange_max_leverage_expires_at=accepted_at + timedelta(seconds=60),
            positions_digest="8" * 64,
            positions_observed_at=accepted_at,
            positions_expires_at=accepted_at + timedelta(seconds=60),
            pending_orders_digest="9" * 64,
            pending_orders_observed_at=accepted_at,
            pending_orders_expires_at=accepted_at + timedelta(seconds=60),
            maximum_order_quantity_digest="0" * 64,
            maximum_order_quantity_observed_at=accepted_at,
            maximum_order_quantity_expires_at=accepted_at + timedelta(seconds=60),
            observed_at=accepted_at,
            expires_at=accepted_at + timedelta(seconds=60),
            safe_facts_json={},
            safe_facts_digest="6" * 64,
            receipt_digest="7" * 64,
            created_at=accepted_at,
        )
    )
    connection.execute(
        EXECUTION_CANARY_RISK_POLICIES_TABLE.insert().values(
            id=uuid4(),
            qualification_decision_id=decision["id"],
            deployment_approval_id=approval.deployment_approval_id,
            execution_attestation_id=attestation.attestation_id,
            probe_receipt_id=probe_receipt_id,
            strategy_version_id=decision["strategy_version_id"],
            strategy_artifact_id=version["artifact_id"],
            strategy_artifact_digest="a" * 64,
            research_target_id=target["id"],
            research_target_digest=target["target_digest"],
            configuration_bundle_id=decision["configuration_bundle_id"],
            configuration_bundle_digest=decision["configuration_bundle_digest"],
            market_snapshot_id=decision["market_snapshot_id"],
            market_snapshot_digest=decision["market_snapshot_digest"],
            execution_target="OKX_DEMO",
            instrument="BTC-USDT-SWAP",
            position_policy="LONG_ONLY",
            max_order_count=max_order_count,
            minimum_contract_size=Decimal("1"),
            contract_value=Decimal("0.001"),
            contract_value_ccy="BTC",
            mark_price=Decimal("10000"),
            limit_price=Decimal("10000"),
            maximum_buy_contracts=Decimal("2"),
            max_notional=Decimal(max_notional),
            strategy_max_leverage=Decimal("12"),
            exchange_max_leverage=Decimal("100"),
            effective_leverage=Decimal("12"),
            metadata_receipt_digest="e" * 64,
            mark_price_receipt_digest="f" * 64,
            attestation_digest=attestation.attestation_digest,
            actor_identity="isolated-policy-owner",
            idempotency_key=f"fixture-{policy_digest}",
            reason="isolated exact policy fixture",
            allow_real_funds=False,
            status="ACTIVE",
            observed_at=accepted_at,
            accepted_at=accepted_at,
            expires_at=accepted_at + timedelta(minutes=30),
            terminated_at=None,
            request_digest=policy_digest,
            policy_digest=policy_digest,
            receipt_digest=receipt_digest,
            termination_digest=None,
        )
    )
    return receipt_digest


def test_fresh_signed_liveness_can_use_immutable_activation_receipt(
    canonical_connection,
):
    with canonical_connection.begin():
        _production_chain(
            canonical_connection,
            runtime_observed_at=NOW - timedelta(minutes=10),
        )
        from app.canonical_v13.models import SIGNALS_TABLE

        assert canonical_connection.execute(
            select(func.count()).select_from(SIGNALS_TABLE)
        ).scalar_one() == 1


def test_signed_liveness_rejects_activation_receipt_digest_drift(
    canonical_connection,
):
    with pytest.raises(
        CanonicalExecutionChainBlocked, match="BLOCKED_SIGNAL_RUNTIME_LINEAGE"
    ):
        with canonical_connection.begin():
            _production_chain(
                canonical_connection,
                signal_runtime_receipt_digest="f" * 64,
            )


def test_exact_budget_and_central_risk_are_replay_safe(
    canonical_connection, monkeypatch
):
    with canonical_connection.begin():
        approval, _deployment, _runtime, intent_id, _launcher = _production_chain(
            canonical_connection
        )
        source_receipt = _risk_policy_source(canonical_connection, approval)
        budget = authorize_demo_risk_budget(
            canonical_connection,
            deployment_approval_id=approval.deployment_approval_id,
            actor_identity="isolated-human-owner",
            reason="exact existing formal budget fixture",
            policy_source_receipt_digest=source_receipt,
            evaluated_at=NOW,
        )
        assert (
            authorize_demo_risk_budget(
                canonical_connection,
                deployment_approval_id=approval.deployment_approval_id,
                actor_identity="isolated-human-owner",
                reason="exact existing formal budget fixture",
                policy_source_receipt_digest=source_receipt,
                evaluated_at=NOW,
            ).repeat_noop
            is True
        )
        import app.canonical_v13.phase9_execution_authority as authority_module

        events = []
        original_lock = authority_module.lock_execution_boundary

        def observed_lock(connection, *, key):
            events.append(("lock", key))
            return original_lock(connection, key=key)

        def observed_sql(_connection, _cursor, statement, _parameters, _context, _many):
            if "risk_decisions" in statement:
                events.append(("risk-select", statement))

        monkeypatch.setattr(authority_module, "lock_execution_boundary", observed_lock)
        event.listen(canonical_connection, "before_cursor_execute", observed_sql)
        decision = decide_central_demo_risk(
            canonical_connection,
            trade_intent_id=intent_id,
            risk_budget_authorization_id=budget.authorization_id,
            evaluated_at=NOW + timedelta(seconds=2),
        )
        event.remove(canonical_connection, "before_cursor_execute", observed_sql)
        assert events[0] == ("lock", f"central-risk-intent:{intent_id}")
        assert events[1][0] == "risk-select"
        assert decision.status == "RISK_ACCEPTED"
        assert decision.reason_code == "RISK_BUDGET_RESERVED"
        repeated = decide_central_demo_risk(
            canonical_connection,
            trade_intent_id=intent_id,
            risk_budget_authorization_id=budget.authorization_id,
            evaluated_at=NOW + timedelta(seconds=3),
        )
        assert repeated.repeat_noop is True
        assert repeated.risk_decision_id == decision.risk_decision_id
        assert (
            canonical_connection.execute(
                select(EXECUTION_RISK_RESERVATIONS_TABLE.c.status)
            ).scalar_one()
            == "RISK_ACCEPTED"
        )
        assert (
            canonical_connection.execute(
                select(RISK_DECISIONS_TABLE.c.status)
            ).scalar_one()
            == "RISK_ACCEPTED"
        )


def test_shadow_acceptance_is_replay_safe_and_creates_no_execution_authority(
    canonical_connection, monkeypatch
):
    with canonical_connection.begin():
        _approval, _deployment, _runtime, intent_id, _launcher = _production_chain(
            canonical_connection, intent_mode="SIGNAL_RISK_SHADOW"
        )
        import app.canonical_v13.phase9_execution_authority as authority_module

        events = []
        original_lock = authority_module.lock_execution_boundary

        def observed_lock(connection, *, key):
            events.append(("lock", key))
            return original_lock(connection, key=key)

        def observed_sql(_connection, _cursor, statement, _parameters, _context, _many):
            if "risk_decisions" in statement:
                events.append(("risk-select", statement))

        monkeypatch.setattr(authority_module, "lock_execution_boundary", observed_lock)
        event.listen(canonical_connection, "before_cursor_execute", observed_sql)
        first = decide_signal_risk_shadow(
            canonical_connection,
            trade_intent_id=intent_id,
            evaluated_at=NOW + timedelta(seconds=2),
        )
        event.remove(canonical_connection, "before_cursor_execute", observed_sql)
        assert events[0] == ("lock", f"shadow-risk-intent:{intent_id}")
        assert events[1][0] == "risk-select"
        repeated = decide_signal_risk_shadow(
            canonical_connection,
            trade_intent_id=intent_id,
            evaluated_at=NOW + timedelta(seconds=3),
        )
        persisted = canonical_connection.execute(
            select(RISK_DECISIONS_TABLE)
        ).mappings().one()

    assert first.status == "RISK_ACCEPTED"
    assert first.reason_code == "SHADOW_BASELINE_AND_COUNTERFACTUAL_VERIFIED"
    assert repeated.repeat_noop is True
    assert repeated.risk_decision_id == first.risk_decision_id
    assert persisted["decision_json"]["decision_mode"] == "SIGNAL_RISK_SHADOW"
    assert persisted["decision_json"]["order_submission_enabled"] is False
    assert persisted["decision_json"]["execution_authorized"] is False
    assert persisted["decision_json"]["checks"] == [
        {
            "check_id": "EXACT_LONG_ONLY_BASELINE",
            "input_digest": canonical_execution_digest(
                {
                    "instId": "BTC-USDT-SWAP",
                    "tdMode": "isolated",
                    "clOrdId": "v13canary00000000000000000001",
                    "side": "buy",
                    "posSide": "long",
                    "ordType": "limit",
                    "sz": "1",
                    "px": "10000",
                }
            ),
            "outcome": "ACCEPTED",
            "reason_code": "SHADOW_EXACT_TARGET_LONG_ONLY_ACCEPTED",
            "order_submission_enabled": False,
            "execution_authorized": False,
        },
        {
            "check_id": "LONG_ONLY_REJECTED_COUNTERFACTUAL",
            "input_digest": canonical_execution_digest(
                {
                    "instId": "BTC-USDT-SWAP",
                    "tdMode": "isolated",
                    "clOrdId": "v13canary00000000000000000001",
                    "side": "sell",
                    "posSide": "short",
                    "ordType": "limit",
                    "sz": "1",
                    "px": "10000",
                }
            ),
            "outcome": "REJECTED",
            "reason_code": "SHADOW_SHORT_SELL_COUNTERFACTUAL_REJECTED",
            "order_submission_enabled": False,
            "execution_authorized": False,
        },
    ]
    assert canonical_connection.execute(
        select(EXECUTION_RISK_BUDGET_AUTHORIZATIONS_TABLE)
    ).first() is None
    assert canonical_connection.execute(
        select(EXECUTION_RISK_RESERVATIONS_TABLE)
    ).first() is None


def test_same_signal_has_distinct_shadow_and_execution_authority_with_exact_replay(
    canonical_connection,
):
    with canonical_connection.begin():
        approval, _deployment, _runtime, shadow_intent_id, _launcher = (
            _production_chain(
                canonical_connection,
                intent_mode="SIGNAL_RISK_SHADOW",
            )
        )
        shadow_intent = (
            canonical_connection.execute(
                select(TRADE_INTENTS_TABLE).where(
                    TRADE_INTENTS_TABLE.c.id == shadow_intent_id
                )
            )
            .mappings()
            .one()
        )
        shadow = decide_signal_risk_shadow(
            canonical_connection,
            trade_intent_id=shadow_intent_id,
            evaluated_at=NOW + timedelta(seconds=2),
        )
        execution_payload = dict(shadow_intent["intent_json"])
        execution_payload.pop("intent_mode")
        execution_intent_id = create_production_demo_intent(
            canonical_connection,
            signal_id=shadow_intent["signal_id"],
            intent_mode="EXECUTION",
            intent_json=execution_payload,
        )
        execution_intent_replay = create_production_demo_intent(
            canonical_connection,
            signal_id=shadow_intent["signal_id"],
            intent_mode="EXECUTION",
            intent_json=execution_payload,
        )
        source_receipt = _risk_policy_source(canonical_connection, approval)
        budget = authorize_demo_risk_budget(
            canonical_connection,
            deployment_approval_id=approval.deployment_approval_id,
            actor_identity="isolated-human-owner",
            reason="one frozen execution transition",
            policy_source_receipt_digest=source_receipt,
            evaluated_at=NOW,
        )
        execution = decide_central_demo_risk(
            canonical_connection,
            trade_intent_id=execution_intent_id,
            risk_budget_authorization_id=budget.authorization_id,
            evaluated_at=NOW + timedelta(seconds=3),
        )
        execution_replay = decide_central_demo_risk(
            canonical_connection,
            trade_intent_id=execution_intent_id,
            risk_budget_authorization_id=budget.authorization_id,
            evaluated_at=NOW + timedelta(seconds=4),
        )
        intent_modes = canonical_connection.execute(
            select(TRADE_INTENTS_TABLE.c.intent_mode).order_by(
                TRADE_INTENTS_TABLE.c.intent_mode
            )
        ).scalars().all()
        decision_modes = canonical_connection.execute(
            select(RISK_DECISIONS_TABLE.c.decision_mode).order_by(
                RISK_DECISIONS_TABLE.c.decision_mode
            )
        ).scalars().all()

    assert execution_intent_id != shadow_intent_id
    assert execution_intent_replay == execution_intent_id
    assert intent_modes == ["EXECUTION", "SIGNAL_RISK_SHADOW"]
    assert decision_modes == ["EXECUTION", "SIGNAL_RISK_SHADOW"]
    assert shadow.status == "RISK_ACCEPTED"
    assert execution.status == "RISK_ACCEPTED"
    assert execution_replay.repeat_noop is True
    assert execution_replay.risk_decision_id == execution.risk_decision_id
    assert execution_replay.reservation_id == execution.reservation_id


def test_backfilled_historical_shadow_intent_replays_without_digest_rewrite(
    canonical_connection,
):
    with canonical_connection.begin():
        _approval, _deployment, _runtime, intent_id, _launcher = _production_chain(
            canonical_connection,
            intent_mode="SIGNAL_RISK_SHADOW",
        )
        persisted = (
            canonical_connection.execute(
                select(TRADE_INTENTS_TABLE).where(
                    TRADE_INTENTS_TABLE.c.id == intent_id
                )
            )
            .mappings()
            .one()
        )
        historical_payload = dict(persisted["intent_json"])
        historical_payload.pop("intent_mode")
        historical_digest = canonical_execution_digest(historical_payload)
        canonical_connection.execute(
            TRADE_INTENTS_TABLE.update()
            .where(TRADE_INTENTS_TABLE.c.id == intent_id)
            .values(
                intent_json=historical_payload,
                intent_digest=historical_digest,
            )
        )
        replayed = create_production_demo_intent(
            canonical_connection,
            signal_id=persisted["signal_id"],
            intent_mode="SIGNAL_RISK_SHADOW",
            intent_json=historical_payload,
        )
        after = (
            canonical_connection.execute(
                select(TRADE_INTENTS_TABLE).where(
                    TRADE_INTENTS_TABLE.c.id == intent_id
                )
            )
            .mappings()
            .one()
        )

    assert replayed == intent_id
    assert after["intent_mode"] == "SIGNAL_RISK_SHADOW"
    assert after["intent_json"] == historical_payload
    assert after["intent_digest"] == historical_digest


def test_shadow_receipt_tamper_and_source_drift_block_replay(canonical_connection):
    with canonical_connection.begin():
        _approval, _deployment, _runtime, intent_id, _launcher = _production_chain(
            canonical_connection, intent_mode="SIGNAL_RISK_SHADOW"
        )
        first = decide_signal_risk_shadow(
            canonical_connection,
            trade_intent_id=intent_id,
            evaluated_at=NOW + timedelta(seconds=2),
        )
        persisted = canonical_connection.execute(
            select(RISK_DECISIONS_TABLE).where(
                RISK_DECISIONS_TABLE.c.id == first.risk_decision_id
            )
        ).mappings().one()
        tampered = dict(persisted["decision_json"])
        tampered["reason_code"] = "FORGED"
        canonical_connection.execute(
            RISK_DECISIONS_TABLE.update()
            .where(RISK_DECISIONS_TABLE.c.id == first.risk_decision_id)
            .values(decision_json=tampered)
        )
        with pytest.raises(
            CanonicalExecutionChainBlocked, match="BLOCKED_SHADOW_RISK_REPLAY_DRIFT"
        ):
            decide_signal_risk_shadow(
                canonical_connection,
                trade_intent_id=intent_id,
                evaluated_at=NOW + timedelta(seconds=3),
            )


def test_shadow_source_digest_drift_blocks_before_replay(canonical_connection):
    from app.canonical_v13.models import TRADE_INTENTS_TABLE

    with canonical_connection.begin():
        _approval, _deployment, _runtime, intent_id, _launcher = _production_chain(
            canonical_connection, intent_mode="SIGNAL_RISK_SHADOW"
        )
        decide_signal_risk_shadow(
            canonical_connection,
            trade_intent_id=intent_id,
            evaluated_at=NOW + timedelta(seconds=2),
        )
        intent = canonical_connection.execute(
            select(TRADE_INTENTS_TABLE).where(TRADE_INTENTS_TABLE.c.id == intent_id)
        ).mappings().one()
        drifted = dict(intent["intent_json"])
        drifted["notional"] = "999"
        canonical_connection.execute(
            TRADE_INTENTS_TABLE.update()
            .where(TRADE_INTENTS_TABLE.c.id == intent_id)
            .values(intent_json=drifted)
        )
        with pytest.raises(
            CanonicalExecutionChainBlocked, match="BLOCKED_SHADOW_RISK_LINEAGE"
        ):
            decide_signal_risk_shadow(
                canonical_connection,
                trade_intent_id=intent_id,
                evaluated_at=NOW + timedelta(seconds=3),
            )


def test_budget_cannot_be_invented_or_replayed_with_drift(canonical_connection):
    with canonical_connection.begin():
        approval, _deployment, _runtime, _intent, _launcher = _production_chain(
            canonical_connection
        )
        first_source = _risk_policy_source(
            canonical_connection, approval, policy_digest="c" * 64
        )
        authorize_demo_risk_budget(
            canonical_connection,
            deployment_approval_id=approval.deployment_approval_id,
            actor_identity="isolated-human-owner",
            reason="formal fixture",
            policy_source_receipt_digest=first_source,
            evaluated_at=NOW,
        )
        with pytest.raises(
            CanonicalExecutionChainBlocked,
            match="CANONICAL_PHASE9_RISK_BUDGET_SOURCE_UNSET",
        ):
            authorize_demo_risk_budget(
                canonical_connection,
                deployment_approval_id=approval.deployment_approval_id,
                actor_identity="isolated-human-owner",
                reason="formal fixture",
                policy_source_receipt_digest="d" * 64,
                evaluated_at=NOW,
            )
        assert canonical_connection.execute(
            select(EXECUTION_RISK_BUDGET_AUTHORIZATIONS_TABLE.c.max_notional)
        ).scalar_one() == Decimal("10")


def test_budget_requires_existing_canonical_policy_source(canonical_connection):
    with canonical_connection.begin():
        approval, _deployment, _runtime, _intent, _launcher = _production_chain(
            canonical_connection
        )
        with pytest.raises(
            CanonicalExecutionChainBlocked,
            match="CANONICAL_PHASE9_RISK_BUDGET_SOURCE_UNSET",
        ):
            authorize_demo_risk_budget(
                canonical_connection,
                deployment_approval_id=approval.deployment_approval_id,
                actor_identity="isolated-human-owner",
                reason="must not invent production budget",
                policy_source_receipt_digest="f" * 64,
                evaluated_at=NOW,
            )


def test_redacted_attestation_requires_exact_safe_permissions(canonical_connection):
    with canonical_connection.begin():
        _approval, deployment, _runtime, _intent, _launcher = _production_chain(
            canonical_connection
        )
        result = record_redacted_demo_attestation(
            canonical_connection,
            deployment_id=deployment.deployment_id,
            instrument="BTC-USDT-SWAP",
            account_fingerprint_digest="d" * 64,
            credential_generation_digest="e" * 64,
            permissions={"read": True, "trade": True, "withdraw": False},
            observed_at=NOW,
            expires_at=NOW + timedelta(seconds=60),
            evaluated_at=NOW,
        )
        assert result.repeat_noop is False
        assert (
            record_redacted_demo_attestation(
                canonical_connection,
                deployment_id=deployment.deployment_id,
                instrument="BTC-USDT-SWAP",
                account_fingerprint_digest="d" * 64,
                credential_generation_digest="e" * 64,
                permissions={"read": True, "trade": True, "withdraw": False},
                observed_at=NOW,
                expires_at=NOW + timedelta(seconds=60),
                evaluated_at=NOW,
            ).repeat_noop
            is True
        )
        with pytest.raises(
            CanonicalExecutionChainBlocked, match="BLOCKED_ATTESTATION_PERMISSIONS"
        ):
            record_redacted_demo_attestation(
                canonical_connection,
                deployment_id=deployment.deployment_id,
                instrument="BTC-USDT-SWAP",
                account_fingerprint_digest="d" * 64,
                credential_generation_digest="e" * 64,
                permissions={"read": True, "trade": True, "withdraw": True},
                observed_at=NOW,
                expires_at=NOW + timedelta(seconds=60),
                evaluated_at=NOW,
            )
        with pytest.raises(
            CanonicalExecutionChainBlocked, match="BLOCKED_ATTESTATION_FRESHNESS"
        ):
            record_redacted_demo_attestation(
                canonical_connection,
                deployment_id=deployment.deployment_id,
                instrument="BTC-USDT-SWAP",
                account_fingerprint_digest="d" * 64,
                credential_generation_digest="e" * 64,
                permissions={"read": True, "trade": True, "withdraw": False},
                observed_at=NOW - timedelta(seconds=16),
                expires_at=NOW + timedelta(seconds=44),
                evaluated_at=NOW,
            )


def test_production_runtime_rejects_unsafe_supervisor_receipt(canonical_connection):
    with canonical_connection.begin():
        _plan, decision = _qualified(canonical_connection)
        approval = approve_demo_deployment(
            canonical_connection,
            qualification_decision_id=decision.qualification_decision_id,
            actor_identity="isolated-human-owner",
            reason="exact Phase 9 isolated contract",
        )
        deployment = create_demo_deployment(
            canonical_connection, deployment_approval_id=approval.deployment_approval_id
        )
        spec = _runtime_spec(canonical_connection, approval, deployment)
        receipt = build_runtime_observation_receipt(
            runtime_instance_id=uuid4(),
            launch_spec=spec,
            status="HEALTHY",
            observed_at=NOW,
            evidence_class="TEST_SIMULATED",
        )
        with pytest.raises(Exception, match="BLOCKED_RUNTIME_LAUNCH_RECEIPT_DRIFT"):
            confirm_production_demo_runtime_observation(
                canonical_connection,
                deployment_id=deployment.deployment_id,
                runtime_identity=spec.runtime_identity,
                image_digest=spec.image_digest,
                credential_reference=spec.credential_reference,
                receipt=receipt,
                evaluated_at=NOW,
            )
