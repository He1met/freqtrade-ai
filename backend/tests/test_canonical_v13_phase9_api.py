from __future__ import annotations

from app.canonical_v13.api import API_PREFIX
from tests.test_canonical_v13_api import _client
from tests.test_canonical_v13_phase9_readiness import _handoff, _qualified


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

        missing_live_evidence = client.post(
            f"{API_PREFIX}/phase9/canary-risk-policies",
            json={
                "qualification_decision_id": str(
                    qualification.qualification_decision_id
                ),
                "deployment_approval_id": approval_payload["deployment_approval_id"],
                "execution_attestation_id": ("00000000-0000-0000-0000-000000000001"),
                "actor_identity": "phase9-human-approver",
                "idempotency_key": "phase9-canary-api-missing-live-evidence",
                "reason": "must fail closed without current exchange evidence",
                "redacted_evidence": {},
            },
        )
        assert missing_live_evidence.status_code == 409
        assert missing_live_evidence.json()["error"]["code"] == (
            "BLOCKED_CANARY_POLICY_EVIDENCE_FIELDS"
        )

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
    finally:
        client.close()
        engine.dispose()
