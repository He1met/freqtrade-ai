from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_db
from app.main import app
from app.services.okx_demo_submission_grant import OkxDemoSubmissionGrantService
from app.services.operator_authorization import operator_request_coordinator


NOW = datetime(2026, 8, 1, tzinfo=timezone.utc)
REQUEST = {
    "approval_id": 41,
    "canonical_hash": "a" * 64,
    "policy_digest": "b" * 64,
    "approved_payload_hash": "c" * 64,
    "client_order_id": "ControlledCanaryOrder000000001",
}
LEGACY_ENDPOINT_BLOCKED = {
    "detail": {
        "operation_status": "BLOCKED",
        "message": "Legacy one-shot arming is disabled; use consent-finalize.",
    }
}


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("FREQTRADE_AI_OPERATOR_TOKEN", "operator-test-token")
    calls = []

    def arm(_self, **payload):
        calls.append(payload)
        return SimpleNamespace(
            grant_id="d" * 32,
            approval_id=41,
            reconciliation_run_id=73,
            request_digest="e" * 64,
            provenance="CONTROLLED_CANARY_NON_PRODUCTION",
            expires_at=NOW + timedelta(seconds=10),
        )

    monkeypatch.setattr(OkxDemoSubmissionGrantService, "arm", arm)

    def override_db():
        yield object()

    app.dependency_overrides[get_db] = override_db
    operator_request_coordinator.reset_for_tests()
    try:
        yield TestClient(app), calls
    finally:
        operator_request_coordinator.reset_for_tests()
        app.dependency_overrides.clear()


def test_legacy_one_shot_grant_is_disabled_before_token_validation(client) -> None:
    api, calls = client
    response = api.post(
        "/api/okx-demo/submission-grants/one-shot",
        headers={
            "Idempotency-Key": "grant-api-no-token",
            "X-Provider-Authorization": "once",
        },
        json=REQUEST,
    )
    assert response.status_code == 409
    assert response.json() == LEGACY_ENDPOINT_BLOCKED
    assert calls == []


def test_legacy_one_shot_grant_is_disabled_without_consent_header(client) -> None:
    api, calls = client
    response = api.post(
        "/api/okx-demo/submission-grants/one-shot",
        headers={
            "X-Operator-Token": "operator-test-token",
            "Idempotency-Key": "grant-api-no-consent",
        },
        json=REQUEST,
    )
    assert response.status_code == 409
    assert response.json() == LEGACY_ENDPOINT_BLOCKED
    assert calls == []


def test_legacy_one_shot_grant_remains_disabled_with_all_headers(client) -> None:
    api, calls = client
    response = api.post(
        "/api/okx-demo/submission-grants/one-shot",
        headers={
            "X-Operator-Token": "operator-test-token",
            "Idempotency-Key": "grant-api-authorized",
            "X-Provider-Authorization": "once",
        },
        json=REQUEST,
    )
    assert response.status_code == 409
    assert response.json() == LEGACY_ENDPOINT_BLOCKED
    assert calls == []
