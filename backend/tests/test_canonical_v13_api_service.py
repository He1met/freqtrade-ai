from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SERVICE_PATH = REPOSITORY_ROOT / "scripts/canonical_v13_api_service.py"


def _load_service(name: str):
    spec = importlib.util.spec_from_file_location(name, SERVICE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_launch_agent_payload_is_loopback_only_and_contains_no_database_secret() -> None:
    service = _load_service("canonical_v13_api_service_plist")
    payload = service._plist_payload(8011)
    serialized = json.dumps(payload)
    assert payload["Label"] == service.LABEL
    assert payload["ProgramArguments"][-3:] == ["serve", "--port", "8011"]
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is True
    assert "DATABASE_URL" not in serialized
    assert "PASSWORD" not in serialized
    assert service.READER_KEYCHAIN_SERVICE not in serialized
    assert service.CONTROL_KEYCHAIN_SERVICE not in serialized


def test_database_urls_are_built_only_from_fixed_principal_and_keychain_reference(
    monkeypatch,
) -> None:
    service = _load_service("canonical_v13_api_service_url")
    monkeypatch.setattr(service, "_read_keychain", lambda _service: "x" * 64)
    url = service._database_url(
        service.READER_PRINCIPAL, service.READER_KEYCHAIN_SERVICE
    )
    assert url.startswith("postgresql+psycopg://freqtrade_ai_v13_api_login:")
    assert url.endswith("@127.0.0.1:5432/freqtrade_ai_v13")


def test_scram_verifier_is_deterministic_and_never_contains_input_material() -> None:
    service = _load_service("canonical_v13_api_service_scram")
    material = "x" * 64
    verifier = service._scram_verifier(material, salt=b"0123456789abcdef")
    assert verifier == service._scram_verifier(
        material, salt=b"0123456789abcdef"
    )
    assert material not in verifier
    assert re.fullmatch(
        r"SCRAM-SHA-256\$4096:[A-Za-z0-9+/=]+\$"
        r"[A-Za-z0-9+/=]+:[A-Za-z0-9+/=]+",
        verifier,
    )


def test_provision_fails_closed_before_database_write_on_existing_keychain(
    monkeypatch,
) -> None:
    service = _load_service("canonical_v13_api_service_existing")
    monkeypatch.setattr(service, "_read_keychain", lambda _service: "x" * 64)
    monkeypatch.setattr(
        service,
        "_admin_connection",
        lambda: pytest.fail("database must not be touched after Keychain collision"),
    )
    with pytest.raises(
        service.CanonicalServiceBlocked,
        match="BLOCKED_KEYCHAIN_ITEM_ALREADY_EXISTS",
    ):
        service.provision_principals()


def test_service_manager_has_no_delete_or_uninstall_command() -> None:
    source = SERVICE_PATH.read_text(encoding="utf-8")
    assert 'choices=("provision", "serve", "install", "status", "restart")' in source
    assert '"uninstall"' not in source


def test_release_checkout_requires_clean_exact_origin_main(monkeypatch) -> None:
    service = _load_service("canonical_v13_api_service_checkout")
    monkeypatch.setattr(service, "REPO_ROOT", Path("/Users/local/release"))
    responses = iter(
        (
            type("Result", (), {"returncode": 0, "stdout": ""})(),
            type("Result", (), {"returncode": 0, "stdout": "abc\n"})(),
            type("Result", (), {"returncode": 0, "stdout": "abc\n"})(),
        )
    )
    monkeypatch.setattr(service, "_run", lambda _command: next(responses))
    monkeypatch.setattr(service, "BACKEND_PYTHON", Path(__file__))
    service._require_release_checkout()


def test_release_checkout_rejects_dirty_or_non_main_head(monkeypatch) -> None:
    service = _load_service("canonical_v13_api_service_checkout_blocked")
    monkeypatch.setattr(service, "REPO_ROOT", Path("/Users/local/release"))
    responses = iter(
        (
            type("Result", (), {"returncode": 0, "stdout": " M user-file\n"})(),
            type("Result", (), {"returncode": 0, "stdout": "abc\n"})(),
            type("Result", (), {"returncode": 0, "stdout": "def\n"})(),
        )
    )
    monkeypatch.setattr(service, "_run", lambda _command: next(responses))
    with pytest.raises(
        service.CanonicalServiceBlocked,
        match="BLOCKED_CANONICAL_RELEASE_CHECKOUT_REQUIRED",
    ):
        service._require_release_checkout()
