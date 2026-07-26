import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import app.core.config as config
from app.core.config import REPO_ROOT, get_settings, load_app_yaml
from app.main import app
from app.core.execution_target import (
    ExecutionTargetConfigurationError,
    parse_execution_target_manifest,
)


def valid_manifest() -> dict:
    return {
        "schema_version": "1",
        "implicit_fallback": False,
        "targets": [
            {
                "target_id": "OKX_DEMO",
                "status": "ACTIVE",
                "exchange": "okx",
                "product_type": "SWAP",
                "margin_mode": "isolated",
                "account_mode": "demo",
                "simulated_trading": True,
                "credential_source": "macos_keychain",
                "write_policy": "SOLE_EXCHANGE_ORDER_TARGET",
                "order_submission_enabled": False,
                "allow_real_funds": False,
            }
        ],
        "non_exchange_scopes": [
            {
                "scope_id": "LOCAL_DRY_RUN",
                "scope_type": "local_simulation",
                "exchange_order_execution": False,
                "write_policy": "NO_EXCHANGE_WRITES",
            }
        ],
    }


def test_default_app_config_resolves_only_okx_demo() -> None:
    settings = get_settings()

    assert settings.execution_target_manifest.active_target_id == "OKX_DEMO"
    assert len(settings.execution_target_manifest.targets) == 1
    assert settings.execution_target_manifest.implicit_fallback is False
    assert settings.execution_target_manifest.active_target.allow_real_funds is False
    assert settings.execution_target_manifest.active_target.order_submission_enabled is False


def test_app_yaml_contains_the_same_execution_target_manifest() -> None:
    raw = load_app_yaml(REPO_ROOT / "config" / "app.yaml")

    parsed = parse_execution_target_manifest(raw["execution"])

    assert parsed.active_target_id == "OKX_DEMO"
    assert parsed.non_exchange_scopes[0].scope_id == "LOCAL_DRY_RUN"
    assert parsed.non_exchange_scopes[0].exchange_order_execution is False


@pytest.mark.parametrize("raw", [None, {}, []])
def test_missing_target_configuration_fails_closed(raw) -> None:
    with pytest.raises(
        ExecutionTargetConfigurationError,
        match="missing; implicit fallback is forbidden",
    ):
        parse_execution_target_manifest(raw)


def test_settings_loader_refuses_to_start_without_execution_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_settings.cache_clear()
    monkeypatch.setattr(config, "load_env_file", lambda _path: None)
    monkeypatch.setattr(config, "load_app_yaml", lambda _path: {"app": {"name": "fixture"}})
    try:
        with pytest.raises(
            ExecutionTargetConfigurationError,
            match="implicit fallback is forbidden",
        ):
            get_settings()
    finally:
        get_settings.cache_clear()


def test_direct_settings_construction_cannot_implicitly_fallback() -> None:
    with pytest.raises(ValidationError, match="execution_target_manifest"):
        config.Settings()


def test_duplicate_target_ids_fail_closed() -> None:
    raw = valid_manifest()
    raw["targets"].append(dict(raw["targets"][0]))

    with pytest.raises(ExecutionTargetConfigurationError, match="duplicate"):
        parse_execution_target_manifest(raw)


@pytest.mark.parametrize("target_id", ["OKX_LIVE", "UNKNOWN", "LOCAL_DRY_RUN"])
def test_unknown_or_non_exchange_target_fails_closed(target_id: str) -> None:
    raw = valid_manifest()
    raw["targets"][0]["target_id"] = target_id

    with pytest.raises(ExecutionTargetConfigurationError, match="only configured"):
        parse_execution_target_manifest(raw)


def test_multiple_active_targets_fail_closed() -> None:
    raw = valid_manifest()
    second = dict(raw["targets"][0])
    second["target_id"] = "UNKNOWN"
    raw["targets"].append(second)

    with pytest.raises(ExecutionTargetConfigurationError):
        parse_execution_target_manifest(raw)


def test_real_funds_disguised_as_demo_fails_closed() -> None:
    raw = valid_manifest()
    raw["targets"][0]["allow_real_funds"] = True

    with pytest.raises(
        ExecutionTargetConfigurationError,
        match="allow_real_funds=True",
    ):
        parse_execution_target_manifest(raw)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("account_mode", "live"),
        ("simulated_trading", False),
        ("exchange", "binance"),
        ("product_type", "SPOT"),
        ("margin_mode", "cross"),
        ("credential_source", "env_file"),
        ("write_policy", "MULTIPLE_WRITERS"),
        ("order_submission_enabled", True),
    ],
)
def test_okx_demo_contract_cannot_be_relabelled_or_weakened(field: str, value) -> None:
    raw = valid_manifest()
    raw["targets"][0][field] = value

    with pytest.raises(ExecutionTargetConfigurationError, match=field):
        parse_execution_target_manifest(raw)


def test_implicit_fallback_cannot_be_enabled_or_named() -> None:
    enabled = valid_manifest()
    enabled["implicit_fallback"] = True
    named = valid_manifest()
    named["fallback_target_id"] = "OKX_DEMO"

    with pytest.raises(ExecutionTargetConfigurationError):
        parse_execution_target_manifest(enabled)
    with pytest.raises(ExecutionTargetConfigurationError):
        parse_execution_target_manifest(named)


def test_local_dry_run_is_never_an_exchange_order_scope() -> None:
    raw = valid_manifest()
    raw["non_exchange_scopes"][0]["exchange_order_execution"] = True

    with pytest.raises(
        ExecutionTargetConfigurationError,
        match="exchange_order_execution",
    ):
        parse_execution_target_manifest(raw)


def test_execution_target_endpoint_and_manifest_never_render_credentials() -> None:
    response = TestClient(app).get("/runtime/execution-target")

    assert response.status_code == 200
    payload = response.json()
    rendered = json.dumps(payload, sort_keys=True)
    assert payload["targets"][0]["target_id"] == "OKX_DEMO"
    assert payload["targets"][0]["credential_source"] == "macos_keychain"
    assert payload["non_exchange_scopes"][0]["scope_id"] == "LOCAL_DRY_RUN"
    assert "api_key" not in rendered.lower()
    assert "api_secret" not in rendered.lower()
    assert "passphrase" not in rendered.lower()


def test_operator_runtime_status_uses_the_same_target_id() -> None:
    response = TestClient(app).get("/runtime/operator-status")

    assert response.status_code == 200
    assert response.json()["runtime_contract"]["execution_target_id"] == "OKX_DEMO"
