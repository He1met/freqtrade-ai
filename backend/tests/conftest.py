import pytest

from app.services.operator_authorization import (
    OPERATOR_TOKEN_ENV,
    operator_request_coordinator,
)


@pytest.fixture(autouse=True)
def reset_operator_request_boundary(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(OPERATOR_TOKEN_ENV, "synthetic-test-operator-token")
    monkeypatch.setenv(
        "FREQTRADE_AI_OKX_DEMO_ATTESTATION_PROOF_KEY",
        "74" * 32,
    )
    operator_request_coordinator.reset_for_tests()
    yield
    operator_request_coordinator.reset_for_tests()
