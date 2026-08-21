from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.canonical_v13.deployment_approval import approve_demo_deployment
from app.canonical_v13.deployment_control import (
    confirm_production_demo_runtime_observation,
    create_demo_deployment,
)
from app.canonical_v13.execution_common import CanonicalExecutionChainBlocked
from app.canonical_v13.models import (
    AUDIT_EVENTS_TABLE,
    DEPLOYMENT_APPROVALS_TABLE,
    EXECUTION_RISK_BUDGET_AUTHORIZATIONS_TABLE,
    EXECUTION_RISK_RESERVATIONS_TABLE,
    QUALIFICATION_DECISIONS_TABLE,
    RISK_DECISIONS_TABLE,
)
from app.canonical_v13.phase9_execution_authority import (
    authorize_demo_risk_budget,
    decide_central_demo_risk,
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
    canonical_connection,  # noqa: F401,F811 - registers the shared fixture
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


def _production_chain(connection):
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
        observed_at=NOW,
        evidence_class="PRODUCTION_DEMO_RUNTIME",
    )
    runtime_id = confirm_production_demo_runtime_observation(
        connection,
        deployment_id=deployment.deployment_id,
        runtime_identity=spec.runtime_identity,
        image_digest=spec.image_digest,
        credential_reference=spec.credential_reference,
        receipt=receipt,
        evaluated_at=NOW,
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
        "side": "buy",
    }
    signal_id = record_production_demo_signal(
        connection,
        deployment_id=deployment.deployment_id,
        runtime_instance_id=runtime_id,
        research_target_id=plan["research_target_id"],
        signal_json=signal_payload,
        evaluated_at=NOW + timedelta(seconds=1),
    )
    from app.canonical_v13.models import SIGNALS_TABLE

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
            "exchange_body": {
                "instId": "BTC-USDT-SWAP",
                "tdMode": "isolated",
                "clOrdId": "v13canary00000000000000000001",
                "side": "buy",
                "posSide": "long",
                "ordType": "post_only",
                "sz": "1",
                "px": "10000",
            },
        },
    )
    return approval, deployment, runtime_id, intent_id, None


def _risk_policy_source(
    connection,
    approval,
    *,
    max_notional="10",
    max_order_count=1,
    policy_digest="b" * 64,
    expires_at=None,
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
    evidence = {
        "contract": "canonical-v13-phase9-risk-policy-source-v1",
        "qualification_decision_id": str(decision["id"]),
        "qualification_decision_digest": decision["decision_digest"],
        "configuration_bundle_id": str(decision["configuration_bundle_id"]),
        "configuration_bundle_digest": decision["configuration_bundle_digest"],
        "execution_target": "OKX_DEMO",
        "instrument": "BTC-USDT-SWAP",
        "max_notional": max_notional,
        "max_order_count": max_order_count,
        "policy_digest": policy_digest,
        "expires_at": (expires_at or NOW + timedelta(hours=1)).isoformat(),
        "allow_real_funds": False,
        "status": "ACCEPTED",
    }
    receipt_digest = canonical_execution_digest(evidence)
    connection.execute(
        AUDIT_EVENTS_TABLE.insert().values(
            id=uuid4(),
            event_type="PHASE9_RISK_POLICY_ACCEPTED",
            aggregate_type="canonical_phase9_risk_policy",
            aggregate_id=str(decision["id"]),
            actor_identity="isolated-policy-owner",
            request_digest=policy_digest,
            receipt_digest=receipt_digest,
            evidence_json=evidence,
            created_at=NOW,
        )
    )
    return receipt_digest


def test_exact_budget_and_central_risk_are_replay_safe(canonical_connection):
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
        decision = decide_central_demo_risk(
            canonical_connection,
            trade_intent_id=intent_id,
            risk_budget_authorization_id=budget.authorization_id,
            evaluated_at=NOW + timedelta(seconds=2),
        )
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
        drifted_source = _risk_policy_source(
            canonical_connection,
            approval,
            max_notional="11",
            policy_digest="d" * 64,
        )
        with pytest.raises(
            CanonicalExecutionChainBlocked, match="BLOCKED_RISK_BUDGET_REPLAY_DRIFT"
        ):
            authorize_demo_risk_budget(
                canonical_connection,
                deployment_approval_id=approval.deployment_approval_id,
                actor_identity="isolated-human-owner",
                reason="formal fixture",
                policy_source_receipt_digest=drifted_source,
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
