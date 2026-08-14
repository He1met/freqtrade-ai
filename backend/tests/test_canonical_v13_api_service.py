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
    assert all(spec[2] not in serialized for spec in service.RESEARCH_PRINCIPAL_SPECS)


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


def test_keychain_presence_probe_never_reads_secret_value(monkeypatch) -> None:
    service = _load_service("canonical_v13_api_service_keychain_presence")
    observed: list[str] = []

    def run(command, **_kwargs):
        observed.extend(command)
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(service.subprocess, "run", run)
    assert service._keychain_item_exists(service.READER_KEYCHAIN_SERVICE) is True
    assert "-w" not in observed


def test_production_environment_uses_six_fixed_keychain_backed_principals(
    monkeypatch,
) -> None:
    service = _load_service("canonical_v13_api_service_six_identities")
    monkeypatch.setattr(service, "_read_keychain", lambda _service: "x" * 64)
    environment = service._production_database_environment()
    assert len(environment) == 6
    assert {
        value.split("://", 1)[1].split(":", 1)[0]
        for value in environment.values()
    } == {
        service.READER_PRINCIPAL,
        service.CONTROL_PRINCIPAL,
        *(spec[0] for spec in service.RESEARCH_PRINCIPAL_SPECS),
    }


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
    with pytest.raises(
        service.CanonicalServiceBlocked,
        match="BLOCKED_KEYCHAIN_ITEM_ALREADY_EXISTS",
    ):
        service.provision_research_principals()


def test_research_provision_requires_empty_current_authority_before_writes(
    monkeypatch,
) -> None:
    service = _load_service("canonical_v13_api_service_research_preflight")
    monkeypatch.setattr(service, "_read_keychain", lambda _service: None)

    def blocked_preflight() -> None:
        raise service.CanonicalServiceBlocked(
            "BLOCKED_RESEARCH_AUTHORITY_PREFLIGHT"
        )

    monkeypatch.setattr(
        service,
        "_require_research_authority_preprovisioned",
        blocked_preflight,
    )
    monkeypatch.setattr(
        service,
        "_admin_connection",
        lambda: pytest.fail("principal writes must not begin after failed preflight"),
    )
    with pytest.raises(
        service.CanonicalServiceBlocked,
        match="BLOCKED_RESEARCH_AUTHORITY_PREFLIGHT",
    ):
        service.provision_research_principals()


def test_research_database_connect_repair_fails_closed_on_non_exact_state(
    monkeypatch,
) -> None:
    service = _load_service("canonical_v13_api_service_connect_repair")
    monkeypatch.setattr(service, "_keychain_item_exists", lambda _service: True)
    verification = type("Verification", (), {"problems": ()})()
    monkeypatch.setattr(
        service,
        "_verify_research_provisioned_state",
        lambda: verification,
    )
    monkeypatch.setattr(
        service,
        "_admin_connection",
        lambda: pytest.fail("repair writes require the exact all-missing state"),
    )
    with pytest.raises(
        service.CanonicalServiceBlocked,
        match="BLOCKED_RESEARCH_CONNECT_REPAIR_PREFLIGHT",
    ):
        service.repair_research_database_connect()


def test_research_connect_grants_target_only_four_capability_roles() -> None:
    service = _load_service("canonical_v13_api_service_connect_grants")

    class Connection:
        def __init__(self) -> None:
            self.statements: list[object] = []

        def execute(self, statement: object) -> None:
            self.statements.append(statement)

    connection = Connection()
    service._grant_research_database_connect(connection)
    assert len(connection.statements) == 4
    rendered = "\n".join(str(value) for value in connection.statements)
    assert "GRANT" in rendered
    assert service.DATABASE_NAME in rendered
    assert all(spec[1] in rendered for spec in service.RESEARCH_PRINCIPAL_SPECS)
    assert all(spec[0] not in rendered for spec in service.RESEARCH_PRINCIPAL_SPECS)


def test_research_connect_repair_accepts_only_all_missing_then_postverifies(
    monkeypatch,
) -> None:
    service = _load_service("canonical_v13_api_service_connect_repair_success")
    monkeypatch.setattr(service, "_keychain_item_exists", lambda _service: True)
    verifications = iter(
        (
            type(
                "Verification",
                (),
                {
                    "accepted": False,
                    "problems": ("missing service database CONNECT count=4",),
                },
            )(),
            type("Verification", (), {"accepted": True, "problems": ()})(),
        )
    )
    monkeypatch.setattr(
        service,
        "_verify_research_provisioned_state",
        lambda: next(verifications),
    )

    class Transaction:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

    class Connection:
        def __init__(self) -> None:
            self.grants = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def transaction(self) -> Transaction:
            return Transaction()

        def execute(self, statement, _parameters=None):
            if isinstance(statement, str):
                return [(spec[1], False) for spec in service.RESEARCH_PRINCIPAL_SPECS]
            self.grants += 1
            return None

    connection = Connection()
    monkeypatch.setattr(service, "_admin_connection", lambda: connection)
    result = service.repair_research_database_connect()
    assert result == {
        "status": "REPAIRED",
        "database": service.DATABASE_NAME,
        "capabilities": [spec[1] for spec in service.RESEARCH_PRINCIPAL_SPECS],
        "database_connect_grants": 4,
        "keychain_items_modified": 0,
    }
    assert connection.grants == 4


def test_service_manager_has_no_delete_or_uninstall_command() -> None:
    source = SERVICE_PATH.read_text(encoding="utf-8")
    assert '"provision-research",' in source
    assert '"repair-research-connect",' in source
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
