from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SERVICE_PATH = REPOSITORY_ROOT / "scripts/canonical_v13_api_service.py"


def _load_service(name: str):
    spec = importlib.util.spec_from_file_location(name, SERVICE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_launch_agent_payload_is_loopback_only_and_contains_no_database_secret() -> (
    None
):
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
    assert all(spec[2] not in serialized for spec in service.PHASE9_PRINCIPAL_SPECS)
    assert service.RUNTIME_READER_PRINCIPAL_SPEC[2] not in serialized
    assert service.RUNTIME_SIGNAL_SIGNER_KEYCHAIN_SERVICE not in serialized


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
    assert verifier == service._scram_verifier(material, salt=b"0123456789abcdef")
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

    monkeypatch.setattr(
        service,
        "_security_command",
        lambda: Path("/usr/bin/security"),
    )
    monkeypatch.setattr(service, "_keychain_account", lambda: "ci-operator")
    monkeypatch.setattr(service.subprocess, "run", run)
    assert service._keychain_item_exists(service.READER_KEYCHAIN_SERVICE) is True
    assert "-w" not in observed


def test_keychain_add_argv_contains_security_binary_exactly_once(monkeypatch) -> None:
    service = _load_service("canonical_v13_api_service_keychain_add_argv")
    material = "m" * 64
    reads = iter((None, material))
    observed = []
    monkeypatch.setattr(service, "_read_keychain", lambda _service: next(reads))
    monkeypatch.setattr(service, "_security_command", lambda: Path("/usr/bin/security"))
    monkeypatch.setattr(service, "_keychain_account", lambda: "ci-operator")

    def run(command, **kwargs):
        observed.append((tuple(command), kwargs))
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(service.subprocess, "run", run)
    service._add_keychain("freqtrade-ai/v13/test-service", material)
    command, kwargs = observed[0]
    assert command.count("/usr/bin/security") == 1
    assert command[1] == "add-generic-password"
    assert kwargs["input"] == material + "\n" + material + "\n"


def test_keychain_replace_keeps_material_out_of_argv(monkeypatch) -> None:
    service = _load_service("canonical_v13_api_service_keychain_replace")
    material = "r" * 64
    observed = []
    monkeypatch.setattr(service, "_keychain_item_exists", lambda _service: True)
    monkeypatch.setattr(service, "_read_keychain", lambda _service: material)
    monkeypatch.setattr(service, "_security_command", lambda: Path("/usr/bin/security"))
    monkeypatch.setattr(service, "_keychain_account", lambda: "ci-operator")

    def run(command, **kwargs):
        observed.append((tuple(command), kwargs))
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(service.subprocess, "run", run)
    service._replace_keychain(service.READER_KEYCHAIN_SERVICE, material)
    command, kwargs = observed[0]
    assert command == (
        "/usr/bin/security",
        "add-generic-password",
        "-U",
        "-a",
        "ci-operator",
        "-s",
        service.READER_KEYCHAIN_SERVICE,
        "-w",
    )
    assert material not in command
    assert kwargs["input"] == material + "\n" + material + "\n"


def test_strict_keychain_delete_uses_fixed_identity_and_verifies_absence(
    monkeypatch,
) -> None:
    service = _load_service("canonical_v13_api_service_keychain_strict_delete")
    item = service.RUNTIME_SIGNAL_SIGNER_KEYCHAIN_SERVICE
    presence = iter((True, False))
    observed = []
    monkeypatch.setattr(service, "_keychain_item_exists", lambda _item: next(presence))
    monkeypatch.setattr(service, "_security_command", lambda: Path("/usr/bin/security"))
    monkeypatch.setattr(service, "_keychain_account", lambda: "ci-operator")

    def run(command, **kwargs):
        observed.append((tuple(command), kwargs))
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(service.subprocess, "run", run)
    assert service._delete_keychain_strict(item) is True
    command, kwargs = observed[0]
    assert command == (
        "/usr/bin/security",
        "delete-generic-password",
        "-a",
        "ci-operator",
        "-s",
        item,
    )
    assert "-w" not in command
    assert kwargs["stdin"] is service.subprocess.DEVNULL


def test_production_environment_uses_fourteen_fixed_keychain_backed_principals(
    monkeypatch,
) -> None:
    service = _load_service("canonical_v13_api_service_fourteen_identities")
    monkeypatch.setattr(service, "_read_keychain", lambda _service: "x" * 64)
    environment = service._production_database_environment()
    assert len(environment) == 14
    assert {
        value.split("://", 1)[1].split(":", 1)[0] for value in environment.values()
    } == {
        service.READER_PRINCIPAL,
        service.CONTROL_PRINCIPAL,
        *(spec[0] for spec in service.RESEARCH_PRINCIPAL_SPECS),
        *(spec[0] for spec in service.PHASE9_PRINCIPAL_SPECS),
    }


def test_api_runtime_reads_only_three_api_routed_phase9_keychain_items(
    monkeypatch,
) -> None:
    service = _load_service("canonical_v13_api_service_runtime_identities")
    observed = []
    monkeypatch.setattr(
        service,
        "_read_keychain",
        lambda key: observed.append(key) or "x" * 64,
    )
    from app.canonical_v13.phase9_persistence import API_PHASE9_CAPABILITIES

    environment = service._production_database_environment(
        phase9_capabilities=API_PHASE9_CAPABILITIES
    )
    assert len(environment) == 9
    assert not {
        "freqtrade-ai/v13/phase9-order-password",
        "freqtrade-ai/v13/phase9-signal-password",
        "freqtrade-ai/v13/phase9-fill-password",
        "freqtrade-ai/v13/phase9-ledger-password",
        "freqtrade-ai/v13/phase9-reconciliation-password",
        service.RUNTIME_READER_PRINCIPAL_SPEC[2],
        service.RUNTIME_SIGNAL_SIGNER_KEYCHAIN_SERVICE,
    }.intersection(observed)


def test_runtime_reader_is_fifteenth_distinct_non_api_service_identity() -> None:
    from app.canonical_v13.bootstrap import (
        LOCAL_PHASE9_SERVICE_PRINCIPALS,
        LOCAL_RESEARCH_SERVICE_PRINCIPALS,
        LOCAL_RUNTIME_SERVICE_PRINCIPALS,
        LOCAL_SERVICE_PRINCIPALS,
    )

    identities = {
        **LOCAL_SERVICE_PRINCIPALS,
        **LOCAL_RESEARCH_SERVICE_PRINCIPALS,
        **LOCAL_PHASE9_SERVICE_PRINCIPALS,
        **LOCAL_RUNTIME_SERVICE_PRINCIPALS,
    }
    assert len(LOCAL_PHASE9_SERVICE_PRINCIPALS) == 8
    assert len(identities) == 15
    assert LOCAL_RUNTIME_SERVICE_PRINCIPALS == {
        "freqtrade_ai_v13_runtime_login": "canonical_runtime_reader"
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
    with pytest.raises(
        service.CanonicalServiceBlocked,
        match="BLOCKED_KEYCHAIN_ITEM_ALREADY_EXISTS",
    ):
        service.provision_phase9_principals()
    with pytest.raises(
        service.CanonicalServiceBlocked,
        match="BLOCKED_KEYCHAIN_ITEM_ALREADY_EXISTS",
    ):
        service.provision_runtime_reader()


def test_research_provision_requires_empty_current_authority_before_writes(
    monkeypatch,
) -> None:
    service = _load_service("canonical_v13_api_service_research_preflight")
    monkeypatch.setattr(service, "_read_keychain", lambda _service: None)

    def blocked_preflight() -> None:
        raise service.CanonicalServiceBlocked("BLOCKED_RESEARCH_AUTHORITY_PREFLIGHT")

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
    assert '"cleanup-phase9-provisioning",' in source
    assert '"uninstall"' not in source


class _RotationResult:
    def __init__(self, rows):
        self.rows = list(rows)

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.rows[0]


class _RotationTransaction:
    def __enter__(self):
        return self

    def __exit__(self, _exc_type, *_args):
        return None


class _RotationConnection:
    def __init__(self, service, *, fail_commit: bool = False, replay=()) -> None:
        self.service = service
        self.fail_commit = fail_commit
        self.replay = tuple(replay)
        self.insert_parameters = None
        self.alter_count = 0
        self.advisory_parameters = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def transaction(self):
        return _RotationTransaction()

    def commit(self):
        if self.fail_commit:
            raise RuntimeError("injected commit failure")

    def execute(self, statement, parameters=None):
        rendered = str(statement)
        if "pg_advisory_xact_lock" in rendered:
            self.advisory_parameters = parameters
            return _RotationResult([(None,)])
        if "SELECT id, request_digest, receipt_digest" in rendered:
            return _RotationResult(self.replay)
        if "pg_catalog.pg_authid" in rendered:
            return _RotationResult(
                [
                    (
                        True,
                        False,
                        False,
                        False,
                        True,
                        False,
                        False,
                        8,
                        self.service._scram_verifier(
                            "o" * 64, salt=b"0123456789abcdef"
                        ),
                    )
                ]
            )
        if "pg_catalog.pg_auth_members" in rendered:
            return _RotationResult([(self.service.READER_CAPABILITY, False)])
        if "SELECT count(*)" in rendered:
            return _RotationResult([(0,)])
        if "INSERT INTO strategy_platform_v13.audit_events" in rendered:
            self.insert_parameters = parameters
            return _RotationResult([])
        if not isinstance(statement, str):
            self.alter_count += 1
        return _RotationResult([])


def _configure_rotation(service, monkeypatch, connection):
    state = {"material": "o" * 64}
    replacements = []
    verified = []
    rejected = []
    monkeypatch.setattr(service, "_require_reader_rotation_safe", lambda: "a" * 40)
    monkeypatch.setattr(service, "_admin_connection", lambda: connection)
    monkeypatch.setattr(
        service, "_read_keychain", lambda _service: state["material"]
    )
    monkeypatch.setattr(service.secrets, "token_urlsafe", lambda _size: "n" * 64)

    def replace(_service, material):
        replacements.append(material)
        state["material"] = material

    monkeypatch.setattr(service, "_replace_keychain", replace)
    monkeypatch.setattr(
        service, "_verify_reader_material", lambda material: verified.append(material)
    )
    monkeypatch.setattr(
        service,
        "_verify_reader_material_rejected",
        lambda material: rejected.append(material),
    )
    monkeypatch.setattr(
        service,
        "restart",
        lambda port: {
            "status": "RESTARTED",
            "health": "HEALTHY",
            "ready": "READY",
            "port": port,
        },
    )
    return state, replacements, verified, rejected


def test_api_reader_rotation_is_redacted_read_only_and_restarts_once(
    monkeypatch,
) -> None:
    service = _load_service("canonical_v13_api_service_reader_rotation")
    connection = _RotationConnection(service)
    state, replacements, verified, rejected = _configure_rotation(
        service, monkeypatch, connection
    )
    payload = service.rotate_api_reader(
        actor_identity="operator:test",
        idempotency_key="incident:test:v1",
        port=8011,
    )
    serialized = json.dumps(payload, sort_keys=True)
    assert payload["status"] == "ROTATED"
    assert payload["credential_generation"] == 1
    assert payload["old_credential_rejected"] is True
    assert payload["new_credential_read_only"] is True
    assert payload["api_restart_count"] == 1
    assert payload["trading_credentials_modified"] is False
    assert replacements == ["n" * 64]
    assert verified == ["o" * 64, "n" * 64]
    assert rejected == ["o" * 64]
    assert state["material"] == "n" * 64
    assert connection.alter_count == 1
    assert connection.advisory_parameters == (
        f"{service.READER_ROTATION_AGGREGATE_TYPE}:{service.READER_PRINCIPAL}",
    )
    assert connection.insert_parameters is not None
    audit_payload = connection.insert_parameters[7]
    assert "o" * 64 not in serialized + audit_payload
    assert "n" * 64 not in serialized + audit_payload
    assert service.CONTROL_KEYCHAIN_SERVICE not in serialized + audit_payload


def test_api_reader_rotation_restores_keychain_if_database_commit_fails(
    monkeypatch,
) -> None:
    service = _load_service("canonical_v13_api_service_reader_rotation_rollback")
    connection = _RotationConnection(service, fail_commit=True)
    state, replacements, _verified, _rejected = _configure_rotation(
        service, monkeypatch, connection
    )
    with pytest.raises(RuntimeError, match="injected commit failure"):
        service.rotate_api_reader(
            actor_identity="operator:test",
            idempotency_key="incident:test:rollback",
            port=8011,
        )
    assert replacements == ["n" * 64, "o" * 64]
    assert state["material"] == "o" * 64


def test_api_reader_rotation_exact_replay_is_noop_without_restart(monkeypatch) -> None:
    service = _load_service("canonical_v13_api_service_reader_rotation_replay")
    actor = "operator:test"
    key = "incident:test:replay"
    release_sha = "a" * 40
    event_id = "11111111-1111-4111-8111-111111111111"
    request_digest = service._canonical_digest(
        {
            "actor_identity": actor,
            "idempotency_key": key,
            "principal": service.READER_PRINCIPAL,
            "release_sha": release_sha,
            "scope": "API_READER_ONLY",
        }
    )
    evidence = {
        "credential_generation": 3,
        "release_sha": release_sha,
        "scope": "API_READER_ONLY",
        "trading_credentials_modified": False,
    }
    receipt_digest = service._canonical_digest(
        {
            "event_id": event_id,
            "event_type": service.READER_ROTATION_EVENT,
            "request_digest": request_digest,
            "evidence": evidence,
        }
    )
    connection = _RotationConnection(
        service,
        replay=((event_id, request_digest, receipt_digest, evidence),),
    )
    monkeypatch.setattr(service, "_require_reader_rotation_safe", lambda: release_sha)
    monkeypatch.setattr(service, "_admin_connection", lambda: connection)
    monkeypatch.setattr(
        service,
        "restart",
        lambda _port: pytest.fail("exact replay must not restart the API"),
    )
    payload = service.rotate_api_reader(
        actor_identity=actor,
        idempotency_key=key,
        port=8011,
    )
    assert payload == {
        "status": "NO_OP_ALREADY_ROTATED",
        "scope": "API_READER_ONLY",
        "credential_generation": 3,
        "receipt_digest": receipt_digest,
        "release_sha": release_sha,
        "secret_material_exposed": False,
    }
    assert connection.alter_count == 0


class _CleanupResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0]


class _CleanupTransaction:
    def __init__(self, connection) -> None:
        self.connection = connection

    def __enter__(self):
        self.connection.events.append("db-transaction-begin")
        return self

    def __exit__(self, exc_type, *_args) -> None:
        self.connection.events.append(
            "db-transaction-commit" if exc_type is None else "db-transaction-rollback"
        )
        if exc_type is None:
            self.connection.roles.clear()


class _CleanupConnection:
    def __init__(
        self, service, *, principals=None, active_sessions=0, fail_write_ordinal=None
    ) -> None:
        self.service = service
        self.roles = set(
            principals
            if principals is not None
            else (spec[0] for spec in service._phase9_cleanup_specs())
        )
        self.active_sessions = active_sessions
        self.fail_write_ordinal = fail_write_ordinal
        self.write_count = 0
        self.events: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def transaction(self):
        return _CleanupTransaction(self)

    def execute(self, statement, _parameters=None):
        if isinstance(statement, str) and "rolcanlogin" in statement:
            return _CleanupResult(
                [
                    (principal, True, False, False, False, True, False, False, 2)
                    for principal in sorted(self.roles)
                ]
            )
        if isinstance(statement, str) and "pg_auth_members" in statement:
            capabilities = {
                principal: capability
                for principal, capability, _service in (
                    self.service._phase9_cleanup_specs()
                )
            }
            return _CleanupResult(
                [
                    (principal, capabilities[principal])
                    for principal in sorted(self.roles)
                ]
            )
        if isinstance(statement, str) and "pg_stat_activity" in statement:
            return _CleanupResult([(self.active_sessions,)])
        self.write_count += 1
        if self.write_count == self.fail_write_ordinal:
            raise RuntimeError("injected database write failure")
        self.events.append("db-write")
        return _CleanupResult([])


def _configure_cleanup_preflight(service, monkeypatch) -> None:
    monkeypatch.setattr(service, "_require_phase9_cleanup_schema_ready", lambda: None)
    monkeypatch.setattr(
        service, "_require_phase9_cleanup_services_stopped", lambda: None
    )


def test_phase9_cleanup_drops_exact_roles_before_deleting_fixed_keychain_items(
    monkeypatch,
) -> None:
    service = _load_service("canonical_v13_api_service_phase9_cleanup")
    _configure_cleanup_preflight(service, monkeypatch)
    connection = _CleanupConnection(service)
    present = set(service._phase9_cleanup_keychain_services())
    events = connection.events
    monkeypatch.setattr(service, "_admin_connection", lambda: connection)
    monkeypatch.setattr(
        service, "_keychain_item_exists", lambda item: item in present
    )

    def delete(item):
        assert connection.roles == set()
        events.append(f"keychain-delete:{item}")
        present.remove(item)
        return True

    monkeypatch.setattr(service, "_delete_keychain_strict", delete)
    result = service.cleanup_phase9_provisioning()
    assert result == {
        "status": "CLEANED_UP",
        "database": service.DATABASE_NAME,
        "principals_removed": 9,
        "keychain_items_removed": 10,
        "repeat_noop": False,
    }
    assert events.count("db-write") == 18
    assert events.index("db-transaction-commit") < next(
        index
        for index, value in enumerate(events)
        if value.startswith("keychain-delete:")
    )
    assert set(service._phase9_cleanup_specs()).isdisjoint(
        set(service.RESEARCH_PRINCIPAL_SPECS)
    )


def test_phase9_cleanup_replays_absent_database_and_residual_keychain(
    monkeypatch,
) -> None:
    service = _load_service("canonical_v13_api_service_phase9_cleanup_replay")
    _configure_cleanup_preflight(service, monkeypatch)
    connection = _CleanupConnection(service, principals=())
    residual = {service.RUNTIME_SIGNAL_SIGNER_KEYCHAIN_SERVICE}
    monkeypatch.setattr(service, "_admin_connection", lambda: connection)
    monkeypatch.setattr(
        service, "_keychain_item_exists", lambda item: item in residual
    )
    monkeypatch.setattr(
        service,
        "_delete_keychain_strict",
        lambda item: (residual.remove(item) or True) if item in residual else False,
    )
    recovered = service.cleanup_phase9_provisioning()
    assert recovered["principals_removed"] == 0
    assert recovered["keychain_items_removed"] == 1
    assert recovered["repeat_noop"] is False
    repeated = service.cleanup_phase9_provisioning()
    assert repeated["principals_removed"] == 0
    assert repeated["keychain_items_removed"] == 0
    assert repeated["repeat_noop"] is True
    assert connection.events == []


@pytest.mark.parametrize("drift", ["role-partial", "keychain-partial", "active"])
def test_phase9_cleanup_fails_closed_on_provisioning_drift(
    monkeypatch, drift
) -> None:
    service = _load_service(f"canonical_v13_api_service_phase9_cleanup_{drift}")
    _configure_cleanup_preflight(service, monkeypatch)
    specs = service._phase9_cleanup_specs()
    connection = _CleanupConnection(
        service,
        principals=(
            (spec[0] for spec in specs[:-1]) if drift == "role-partial" else None
        ),
        active_sessions=1 if drift == "active" else 0,
    )
    present = set(service._phase9_cleanup_keychain_services())
    if drift == "keychain-partial":
        present.remove(service.RUNTIME_SIGNAL_SIGNER_KEYCHAIN_SERVICE)
    monkeypatch.setattr(service, "_admin_connection", lambda: connection)
    monkeypatch.setattr(
        service, "_keychain_item_exists", lambda item: item in present
    )
    monkeypatch.setattr(
        service,
        "_delete_keychain_strict",
        lambda _item: pytest.fail("Keychain must not change after drift"),
    )
    reason = {
        "role-partial": "BLOCKED_PHASE9_CLEANUP_ROLE_PARTIAL",
        "keychain-partial": "BLOCKED_PHASE9_CLEANUP_KEYCHAIN_PARTIAL",
        "active": "BLOCKED_PHASE9_CLEANUP_ACTIVE_SESSION",
    }[drift]
    with pytest.raises(service.CanonicalServiceBlocked, match=reason):
        service.cleanup_phase9_provisioning()
    assert "db-transaction-begin" not in connection.events


def test_phase9_cleanup_rejects_attribute_or_membership_drift(monkeypatch) -> None:
    service = _load_service("canonical_v13_api_service_phase9_cleanup_role_drift")
    connection = _CleanupConnection(service)
    observed = service._phase9_cleanup_role_state(connection)
    principal = next(iter(observed))
    observed[principal] = (True, True, False, False, True, False, False, 2)
    with pytest.raises(
        service.CanonicalServiceBlocked,
        match="BLOCKED_PHASE9_CLEANUP_ROLE_ATTRIBUTES",
    ):
        service._require_exact_phase9_cleanup_roles(connection, observed)

    observed[principal] = (True, False, False, False, True, False, False, 2)
    original_execute = connection.execute

    def membership_drift(statement, parameters=None):
        if isinstance(statement, str) and "pg_auth_members" in statement:
            return _CleanupResult([])
        return original_execute(statement, parameters)

    monkeypatch.setattr(connection, "execute", membership_drift)
    with pytest.raises(
        service.CanonicalServiceBlocked,
        match="BLOCKED_PHASE9_CLEANUP_ROLE_MEMBERSHIP",
    ):
        service._require_exact_phase9_cleanup_roles(connection, observed)


def test_phase9_cleanup_recovers_after_keychain_partial_failure(monkeypatch) -> None:
    service = _load_service("canonical_v13_api_service_phase9_cleanup_crash_replay")
    _configure_cleanup_preflight(service, monkeypatch)
    connection = _CleanupConnection(service)
    present = set(service._phase9_cleanup_keychain_services())
    monkeypatch.setattr(service, "_admin_connection", lambda: connection)
    monkeypatch.setattr(
        service, "_keychain_item_exists", lambda item: item in present
    )
    calls = 0

    def flaky_delete(item):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise service.CanonicalServiceBlocked(
                "BLOCKED_PHASE9_CLEANUP_KEYCHAIN_DELETE"
            )
        if item not in present:
            return False
        present.remove(item)
        return True

    monkeypatch.setattr(service, "_delete_keychain_strict", flaky_delete)
    with pytest.raises(
        service.CanonicalServiceBlocked,
        match="BLOCKED_PHASE9_CLEANUP_KEYCHAIN_DELETE",
    ):
        service.cleanup_phase9_provisioning()
    assert connection.roles == set()
    result = service.cleanup_phase9_provisioning()
    assert result["principals_removed"] == 0
    assert result["keychain_items_removed"] == 9
    assert present == set()


def test_phase9_cleanup_database_failure_rolls_back_before_keychain(
    monkeypatch,
) -> None:
    service = _load_service("canonical_v13_api_service_phase9_cleanup_db_failure")
    _configure_cleanup_preflight(service, monkeypatch)
    connection = _CleanupConnection(service, fail_write_ordinal=5)
    present = set(service._phase9_cleanup_keychain_services())
    monkeypatch.setattr(service, "_admin_connection", lambda: connection)
    monkeypatch.setattr(
        service, "_keychain_item_exists", lambda item: item in present
    )
    monkeypatch.setattr(
        service,
        "_delete_keychain_strict",
        lambda _item: pytest.fail("Keychain must follow committed DB cleanup"),
    )
    with pytest.raises(
        service.CanonicalServiceBlocked,
        match="BLOCKED_PHASE9_CLEANUP_DATABASE_WRITE",
    ):
        service.cleanup_phase9_provisioning()
    assert connection.roles == {spec[0] for spec in service._phase9_cleanup_specs()}
    assert connection.events[-1] == "db-transaction-rollback"
    assert present == set(service._phase9_cleanup_keychain_services())


def test_phase9_cleanup_requires_all_fixed_services_unloaded(monkeypatch) -> None:
    service = _load_service("canonical_v13_api_service_phase9_cleanup_services")
    calls = []

    def run(command):
        calls.append(tuple(command))
        return type("Result", (), {"returncode": 0 if len(calls) == 2 else 113})()

    monkeypatch.setattr(service, "_run", run)
    with pytest.raises(
        service.CanonicalServiceBlocked,
        match="BLOCKED_PHASE9_CLEANUP_SERVICE_RUNNING",
    ):
        service._require_phase9_cleanup_services_stopped()
    assert service.PHASE9_CLEANUP_LAUNCH_AGENT_LABELS[0] == service.LABEL
    assert len(calls) == 2


def test_phase9_cleanup_cli_dispatches_only_narrow_cleanup(monkeypatch, capsys) -> None:
    service = _load_service("canonical_v13_api_service_phase9_cleanup_cli")
    monkeypatch.setattr(
        service,
        "cleanup_phase9_provisioning",
        lambda: {
            "status": "CLEANED_UP",
            "principals_removed": 0,
            "keychain_items_removed": 0,
            "repeat_noop": True,
        },
    )
    assert service.main(["cleanup-phase9-provisioning"]) == 0
    assert json.loads(capsys.readouterr().out)["repeat_noop"] is True


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
