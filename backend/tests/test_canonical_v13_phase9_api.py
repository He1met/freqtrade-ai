from __future__ import annotations

from app.canonical_v13.api import API_PREFIX
from app.canonical_v13.manifest import CANONICAL_BUSINESS_SCHEMA
from app.canonical_v13.models import (
    DEPLOYMENTS_TABLE,
    EXECUTION_CANARY_PROBE_RECEIPTS_TABLE,
    EXECUTION_CANARY_RISK_POLICIES_TABLE,
    EXECUTION_RISK_BUDGET_AUTHORIZATIONS_TABLE,
    EXECUTION_RISK_RESERVATIONS_TABLE,
    ORDERS_TABLE,
    RISK_DECISIONS_TABLE,
    RUNTIME_INSTANCES_TABLE,
    SIGNALS_TABLE,
    TRADE_INTENTS_TABLE,
)
from sqlalchemy import func, select
from tests.test_canonical_v13_api import _client
from tests.test_canonical_v13_phase9_execution_authority import _production_chain
from tests.test_canonical_v13_phase9_readiness import (
    _handoff,
    _qualified,
    _seed_stage_a,
)


def test_phase9_api_requires_and_projects_exact_handoff() -> None:
    engine, client = _client()
    try:
        with engine.begin() as connection:
            qualification = _qualified(connection)
            handoff = _handoff(connection, qualification)
        response = client.get(
            f"{API_PREFIX}/phase9/readiness",
            params={
                "qualification_decision_id": str(handoff.qualification_decision_id),
                "strategy_version_id": str(handoff.strategy_version_id),
                "configuration_bundle_id": str(handoff.configuration_bundle_id),
                "market_snapshot_id": str(handoff.market_snapshot_id),
                "stage": "QUALIFICATION_HANDOFF",
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "READY"
        assert payload["reason_codes"] == []
        assert payload["handoff"]["qualification_decision_id"] == str(
            handoff.qualification_decision_id
        )
        assert payload["execution_domain_counts"]["orders"] == 0

        drifted = client.get(
            f"{API_PREFIX}/phase9/readiness",
            params={
                "qualification_decision_id": str(handoff.qualification_decision_id),
                "strategy_version_id": "00000000-0000-0000-0000-000000000001",
                "configuration_bundle_id": str(handoff.configuration_bundle_id),
                "market_snapshot_id": str(handoff.market_snapshot_id),
            },
        )
        assert drifted.status_code == 409
        assert drifted.json()["error"]["code"] == (
            "EXACT_QUALIFICATION_HANDOFF_ID_MISMATCH"
        )
    finally:
        client.close()
        engine.dispose()


def test_phase9_control_api_preserves_qualified_approval_and_demo_deployment() -> None:
    engine, client = _client()
    try:
        with engine.begin() as connection:
            qualification = _qualified(connection)
        approval = client.post(
            f"{API_PREFIX}/phase9/approvals",
            json={
                "qualification_decision_id": str(
                    qualification.qualification_decision_id
                ),
                "actor_identity": "phase9-human-approver",
                "reason": "explicit reviewed Demo-only deployment",
            },
        )
        assert approval.status_code == 201, approval.text
        approval_payload = approval.json()
        assert approval_payload["status"] == "APPROVED"

        deployment = client.post(
            f"{API_PREFIX}/phase9/deployments",
            json={"deployment_approval_id": approval_payload["deployment_approval_id"]},
        )
        assert deployment.status_code == 201, deployment.text
        deployment_payload = deployment.json()
        assert deployment_payload["status"] == "PENDING"
        assert len(deployment_payload["capability_digest"]) == 64

        replay = client.post(
            f"{API_PREFIX}/phase9/deployments",
            json={"deployment_approval_id": approval_payload["deployment_approval_id"]},
        )
        assert replay.status_code == 201
        assert replay.json() == deployment_payload

        missing_probe_receipt = client.post(
            f"{API_PREFIX}/phase9/canary-risk-policies",
            json={
                "qualification_decision_id": str(
                    qualification.qualification_decision_id
                ),
                "deployment_approval_id": approval_payload["deployment_approval_id"],
                "probe_receipt_id": ("00000000-0000-0000-0000-000000000001"),
                "actor_identity": "phase9-human-approver",
                "idempotency_key": "phase9-canary-api-missing-probe-receipt",
                "reason": "must fail closed without current exchange evidence",
            },
        )
        assert missing_probe_receipt.status_code == 409
        assert missing_probe_receipt.json()["error"]["code"] == (
            "BLOCKED_CANARY_POLICY_LINEAGE"
        )

        forged_raw_evidence = client.post(
            f"{API_PREFIX}/phase9/canary-risk-policies",
            json={
                "qualification_decision_id": str(
                    qualification.qualification_decision_id
                ),
                "deployment_approval_id": approval_payload["deployment_approval_id"],
                "probe_receipt_id": "00000000-0000-0000-0000-000000000001",
                "actor_identity": "phase9-human-approver",
                "idempotency_key": "phase9-canary-api-forged-raw-evidence",
                "reason": "raw evidence must never cross the API boundary",
                "redacted_evidence": {
                    "mark_price": "1",
                    "exchange_max_leverage": "999",
                },
            },
        )
        assert forged_raw_evidence.status_code == 422

        missing_budget_source = client.post(
            f"{API_PREFIX}/phase9/risk-budgets",
            json={
                "deployment_approval_id": approval_payload["deployment_approval_id"],
                "actor_identity": "phase9-human-approver",
                "reason": "must not invent a production execution budget",
                "policy_source_receipt_digest": "f" * 64,
            },
        )
        assert missing_budget_source.status_code == 409
        assert missing_budget_source.json()["error"]["code"] == (
            "CANONICAL_PHASE9_RISK_BUDGET_SOURCE_UNSET"
        )

        missing_shadow_intent = client.post(
            f"{API_PREFIX}/phase9/shadow-risk-decisions",
            json={"trade_intent_id": "00000000-0000-0000-0000-000000000001"},
        )
        assert missing_shadow_intent.status_code == 409
        assert missing_shadow_intent.json()["error"]["code"] == (
            "BLOCKED_SHADOW_RISK_LINEAGE"
        )

        forged_shadow_authority = client.post(
            f"{API_PREFIX}/phase9/shadow-risk-decisions",
            json={
                "trade_intent_id": "00000000-0000-0000-0000-000000000001",
                "execution_authorized": True,
                "risk_budget_authorization_id": (
                    "00000000-0000-0000-0000-000000000002"
                ),
            },
        )
        assert forged_shadow_authority.status_code == 422
    finally:
        client.close()
        engine.dispose()


def test_phase9_api_disables_stopped_historical_deployment_exactly_once() -> None:
    engine, client = _client()
    try:
        with engine.begin() as connection:
            historical = _qualified(connection)
            successor = _qualified(connection)
            deployment_id, runtime_id = _seed_stage_a(
                connection, _handoff(connection, historical)
            )
            connection.execute(
                RUNTIME_INSTANCES_TABLE.update()
                .where(RUNTIME_INSTANCES_TABLE.c.id == runtime_id)
                .values(status="STOPPED")
            )
        command = {
            "superseded_by_qualification_decision_id": str(
                successor.qualification_decision_id
            ),
            "actor_identity": "operator:phase9-api-test",
            "reason": "supersede exact stopped Demo lineage",
        }
        first = client.post(
            f"{API_PREFIX}/phase9/deployments/{deployment_id}/disable",
            json=command,
        )
        assert first.status_code == 200, first.text
        assert first.json()["status"] == "DISABLED"
        assert first.json()["repeat_noop"] is False
        repeated = client.post(
            f"{API_PREFIX}/phase9/deployments/{deployment_id}/disable",
            json=command,
        )
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()["receipt_digest"] == first.json()["receipt_digest"]
        assert repeated.json()["disabled_at"] == first.json()["disabled_at"]
        assert repeated.json()["repeat_noop"] is True
        drifted = client.post(
            f"{API_PREFIX}/phase9/deployments/{deployment_id}/disable",
            json={**command, "reason": "different reason must fail closed"},
        )
        assert drifted.status_code == 409
        assert drifted.json()["error"]["code"] == (
            "BLOCKED_DEPLOYMENT_DISABLE_REPLAY_DRIFT"
        )
        with engine.begin() as connection:
            effective = connection.execution_options(
                schema_translate_map={CANONICAL_BUSINESS_SCHEMA: None}
            )
            row = (
                effective.execute(
                    select(DEPLOYMENTS_TABLE).where(
                        DEPLOYMENTS_TABLE.c.id == deployment_id
                    )
                )
                .mappings()
                .one()
            )
        assert row["disabled_by"] == command["actor_identity"]
        assert row["disable_reason"] == command["reason"]
        assert row["disable_receipt_digest"] == first.json()["receipt_digest"]
    finally:
        client.close()
        engine.dispose()


def test_shadow_api_persists_one_non_executable_dual_check_receipt() -> None:
    engine, client = _client()
    try:
        with engine.begin() as connection:
            _approval, _deployment, _runtime, intent_id, _launcher = _production_chain(
                connection, intent_mode="SIGNAL_RISK_SHADOW"
            )
        first = client.post(
            f"{API_PREFIX}/phase9/shadow-risk-decisions",
            json={"trade_intent_id": str(intent_id)},
        )
        assert first.status_code == 201, first.text
        repeated = client.post(
            f"{API_PREFIX}/phase9/shadow-risk-decisions",
            json={"trade_intent_id": str(intent_id)},
        )
        assert repeated.status_code == 201, repeated.text
        assert repeated.json()["repeat_noop"] is True
        assert repeated.json()["risk_decision_id"] == first.json()["risk_decision_id"]
        assert repeated.json()["decision_digest"] == first.json()["decision_digest"]
        with engine.begin() as connection:
            effective = connection.execution_options(
                schema_translate_map={CANONICAL_BUSINESS_SCHEMA: None}
            )
            decision = effective.execute(select(RISK_DECISIONS_TABLE)).mappings().one()
            counts = {
                table.name: effective.execute(
                    select(func.count()).select_from(table)
                ).scalar_one()
                for table in (
                    SIGNALS_TABLE,
                    TRADE_INTENTS_TABLE,
                    RISK_DECISIONS_TABLE,
                    EXECUTION_CANARY_PROBE_RECEIPTS_TABLE,
                    EXECUTION_CANARY_RISK_POLICIES_TABLE,
                    EXECUTION_RISK_BUDGET_AUTHORIZATIONS_TABLE,
                    EXECUTION_RISK_RESERVATIONS_TABLE,
                    ORDERS_TABLE,
                )
            }
        assert decision["decision_json"]["checks"][0]["outcome"] == "ACCEPTED"
        assert decision["decision_json"]["checks"][1]["outcome"] == "REJECTED"
        assert decision["decision_json"]["order_submission_enabled"] is False
        assert decision["decision_json"]["execution_authorized"] is False
        assert counts == {
            "signals": 1,
            "trade_intents": 1,
            "risk_decisions": 1,
            "execution_canary_probe_receipts": 0,
            "execution_canary_risk_policies": 0,
            "execution_risk_budget_authorizations": 0,
            "execution_risk_reservations": 0,
            "orders": 0,
        }
        assert counts["risk_decisions"] == 1
    finally:
        client.close()
        engine.dispose()


def test_intent_api_separates_execution_from_existing_shadow_on_same_signal() -> None:
    engine, client = _client()
    try:
        with engine.begin() as connection:
            _approval, _deployment, _runtime, shadow_intent_id, _launcher = (
                _production_chain(
                    connection, intent_mode="SIGNAL_RISK_SHADOW"
                )
            )
            effective = connection.execution_options(
                schema_translate_map={CANONICAL_BUSINESS_SCHEMA: None}
            )
            shadow_intent = (
                effective.execute(
                    select(TRADE_INTENTS_TABLE).where(
                        TRADE_INTENTS_TABLE.c.id == shadow_intent_id
                    )
                )
                .mappings()
                .one()
            )
        payload = dict(shadow_intent["intent_json"])
        payload.pop("intent_mode")
        command = {
            "signal_id": str(shadow_intent["signal_id"]),
            "intent_mode": "EXECUTION",
            "intent_json": payload,
        }
        first = client.post(f"{API_PREFIX}/phase9/intents", json=command)
        repeated = client.post(f"{API_PREFIX}/phase9/intents", json=command)
        assert first.status_code == 201, first.text
        assert repeated.status_code == 201, repeated.text
        assert repeated.json() == first.json()
        assert first.json()["trade_intent_id"] != str(shadow_intent_id)
        with engine.begin() as connection:
            effective = connection.execution_options(
                schema_translate_map={CANONICAL_BUSINESS_SCHEMA: None}
            )
            modes = effective.execute(
                select(TRADE_INTENTS_TABLE.c.intent_mode).order_by(
                    TRADE_INTENTS_TABLE.c.intent_mode
                )
            ).scalars().all()
        assert modes == ["EXECUTION", "SIGNAL_RISK_SHADOW"]
    finally:
        client.close()
        engine.dispose()
