from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from app.core import config
from app.main import app
from app.services.runtime_research_bundle_binding import RuntimeResearchBundleBinding


def _binding() -> RuntimeResearchBundleBinding:
    return RuntimeResearchBundleBinding(
        bundle_id=77,
        bundle_digest="a" * 64,
        generation_profile_version_id=101,
        diversity_profile_version_id=102,
        quality_gate_profile_version_id=103,
        scoring_profile_version_id=104,
        research_profile_version_id=105,
        candidates_per_target=4,
        target_count=6,
        candidate_count=24,
    )


def test_v13_runtime_configuration_api_is_opt_in(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.api.runtime_v13.get_settings",
        lambda: SimpleNamespace(
            v13_no_trade_mode=False,
            v13_configuration_bundle_snapshot_id=None,
        ),
    )
    response = TestClient(app).get("/api/v1/runtime/configuration-readiness")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "V13_NO_TRADE_MODE_DISABLED"


def test_v13_runtime_configuration_api_requires_explicit_bundle(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.api.runtime_v13.get_settings",
        lambda: SimpleNamespace(
            v13_no_trade_mode=True,
            v13_configuration_bundle_snapshot_id=None,
        ),
    )
    response = TestClient(app).get("/api/v1/runtime/configuration-readiness")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == (
        "V13_CONFIGURATION_BUNDLE_ID_REQUIRED"
    )


def test_v13_runtime_configuration_api_returns_sanitized_binding(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.api.runtime_v13.get_settings",
        lambda: SimpleNamespace(
            v13_no_trade_mode=True,
            v13_configuration_bundle_snapshot_id=77,
        ),
    )
    monkeypatch.setattr(
        "app.api.runtime_v13.read_runtime_research_bundle_binding",
        lambda _engine, bundle_id: _binding() if bundle_id == 77 else None,
    )
    response = TestClient(app).get("/api/v1/runtime/configuration-readiness")

    assert response.status_code == 200
    payload = response.json()
    assert payload["bundle_id"] == 77
    assert payload["candidate_count"] == 24
    assert payload["credential_attestation"] == "UNKNOWN_OUT_OF_SCOPE"
    assert payload["order_submission"] == "DISABLED"
    assert payload["allow_real_funds"] is False
    assert not any("secret" in key.lower() for key in payload)


def test_readyz_uses_bundle_gate_only_in_explicit_v13_mode(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.api.health.get_settings",
        lambda: SimpleNamespace(
            v13_no_trade_mode=True,
            v13_configuration_bundle_snapshot_id=77,
        ),
    )
    monkeypatch.setattr(
        "app.api.health.read_runtime_research_bundle_binding",
        lambda _engine, bundle_id: _binding() if bundle_id == 77 else None,
    )

    response = TestClient(app).get("/readyz")

    assert response.status_code == 200
    assert response.json()["runtime_mode"] == "V13_NO_TRADE"
    assert response.json()["order_submission"] == "DISABLED"


def test_settings_require_explicit_strict_v13_environment(monkeypatch) -> None:
    monkeypatch.setenv(config.TEST_DISABLE_ENV_FILE_ENV, "1")
    monkeypatch.setenv("FREQTRADE_AI_V13_NO_TRADE_MODE", "1")
    monkeypatch.setenv("FREQTRADE_AI_CONFIGURATION_BUNDLE_SNAPSHOT_ID", "77")
    config.get_settings.cache_clear()
    try:
        settings = config.get_settings()
        assert settings.v13_no_trade_mode is True
        assert settings.v13_configuration_bundle_snapshot_id == 77
    finally:
        config.get_settings.cache_clear()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("FREQTRADE_AI_V13_NO_TRADE_MODE", "true"),
        ("FREQTRADE_AI_CONFIGURATION_BUNDLE_SNAPSHOT_ID", "0"),
        ("FREQTRADE_AI_CONFIGURATION_BUNDLE_SNAPSHOT_ID", "60/6"),
    ],
)
def test_settings_reject_invalid_v13_environment(monkeypatch, name, value) -> None:
    monkeypatch.setenv(config.TEST_DISABLE_ENV_FILE_ENV, "1")
    monkeypatch.setenv(name, value)
    if name != "FREQTRADE_AI_V13_NO_TRADE_MODE":
        monkeypatch.setenv("FREQTRADE_AI_V13_NO_TRADE_MODE", "1")
    config.get_settings.cache_clear()
    try:
        with pytest.raises(ValueError, match=name):
            config.get_settings()
    finally:
        config.get_settings.cache_clear()
