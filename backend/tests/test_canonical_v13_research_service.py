from __future__ import annotations

from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace


SERVICE_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts/canonical_v13_research_service.py"
)
SPEC = importlib.util.spec_from_file_location(
    "canonical_v13_research_service_test", SERVICE_PATH
)
assert SPEC is not None and SPEC.loader is not None
service = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(service)


def _write_private(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


def _execution(workspace: Path) -> dict[str, object]:
    return {
        "api_base_url": "http://127.0.0.1:8011",
        "oci_runtime": "/opt/homebrew/Cellar/podman/6.1.0/bin/podman-remote",
        "image_reference": "canonical-v13-research@sha256:" + "a" * 64,
        "market_artifact_root": "/private/market",
        "workspace_root": str(workspace),
        "cpu_limit": "1.0",
        "memory_mb": 1024,
        "timeout_seconds": 900,
        "output_bytes": 1048576,
        "pids_limit": 64,
        "tmpfs_mb": 128,
    }


def _fake_cli(observed: dict[str, object]):
    @contextmanager
    def gate_lock(environment):
        observed["gate_lock"] = dict(environment)
        yield

    def gate_execute(environment, payload):
        observed["environment"] = dict(environment)
        observed["payload"] = payload
        return {"status": "PASSED"}

    def worker_execute(environment, payload):
        observed["environment"] = dict(environment)
        observed["payload"] = payload
        return {"status": "ACCEPTED"}

    return SimpleNamespace(
        API_BASE_ENV="API",
        OCI_RUNTIME_ENV="OCI",
        IMAGE_ENV="IMAGE",
        MARKET_ROOT_ENV="MARKET",
        WORKSPACE_ROOT_ENV="WORKSPACE",
        CPU_LIMIT_ENV="CPU",
        MEMORY_LIMIT_ENV="MEMORY",
        TIMEOUT_LIMIT_ENV="TIMEOUT",
        OUTPUT_LIMIT_ENV="OUTPUT",
        PIDS_LIMIT_ENV="PIDS",
        TMPFS_LIMIT_ENV="TMPFS",
        LOOKAHEAD_ACTIVATION_ENV="LOOKAHEAD_ACTIVATION",
        ACTIVATION_ENV="RESEARCH_ACTIVATION",
        READER_DATABASE_URL_ENV="READER_DSN",
        VALIDATION_DATABASE_URL_ENV="VALIDATION_DSN",
        SCORING_DATABASE_URL_ENV="SCORING_DSN",
        QUALIFICATION_DATABASE_URL_ENV="QUALIFICATION_DSN",
        _gate_writer_lock=gate_lock,
        _gate_execute=gate_execute,
        _worker_execute=worker_execute,
    )


def test_gate_injects_only_reader_and_never_exports_database_urls(
    monkeypatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    research = tmp_path / "gate.json"
    supervisor = tmp_path / "supervisor.json"
    _write_private(research, {"exact": "gate"})
    _write_private(
        supervisor,
        {
            "command": "gate",
            "research_command_file": str(research),
            "execution": _execution(workspace),
        },
    )
    observed: dict[str, object] = {}
    requested: list[tuple[str, str]] = []
    before = dict(os.environ)
    monkeypatch.setattr(service, "_load_research_cli", lambda: _fake_cli(observed))
    monkeypatch.setattr(service.api_service, "require_release_checkout", lambda: None)

    def database_url(principal, keychain_service):
        requested.append((principal, keychain_service))
        return "postgresql+psycopg://reader:secret@127.0.0.1/freqtrade_ai_v13"

    monkeypatch.setattr(service.api_service, "_database_url", database_url)
    assert service.execute(supervisor) == {"status": "PASSED"}
    assert requested == [
        (
            service.api_service.READER_PRINCIPAL,
            service.api_service.READER_KEYCHAIN_SERVICE,
        )
    ]
    assert "READER_DSN" in observed["environment"]
    assert "VALIDATION_DSN" not in observed["environment"]
    assert dict(os.environ) == before


def test_worker_injects_exact_four_separated_research_identities(
    monkeypatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    research = tmp_path / "worker.json"
    supervisor = tmp_path / "supervisor.json"
    _write_private(research, {"exact": "worker"})
    _write_private(
        supervisor,
        {
            "command": "worker-execute",
            "research_command_file": str(research),
            "execution": _execution(workspace),
        },
    )
    observed: dict[str, object] = {}
    requested: list[tuple[str, str]] = []
    monkeypatch.setattr(service, "_load_research_cli", lambda: _fake_cli(observed))
    monkeypatch.setattr(service.api_service, "require_release_checkout", lambda: None)

    def database_url(principal, keychain_service):
        requested.append((principal, keychain_service))
        return f"postgresql+psycopg://{principal}:secret@127.0.0.1/freqtrade_ai_v13"

    monkeypatch.setattr(service.api_service, "_database_url", database_url)
    assert service.execute(supervisor) == {"status": "ACCEPTED"}
    assert requested == [
        (
            service.api_service.READER_PRINCIPAL,
            service.api_service.READER_KEYCHAIN_SERVICE,
        ),
        *[
            (principal, keychain)
            for principal, _role, keychain in (
                service.api_service.RESEARCH_PRINCIPAL_SPECS[:3]
            )
        ],
    ]
    assert {
        "READER_DSN",
        "VALIDATION_DSN",
        "SCORING_DSN",
        "QUALIFICATION_DSN",
    }.issubset(observed["environment"])
    assert "PRODUCTION_RESEARCH_NO_TRADE_V1" in observed["environment"].values()


def test_supervisor_rejects_non_private_command_file(tmp_path: Path) -> None:
    command = tmp_path / "supervisor.json"
    command.write_text("{}", encoding="utf-8")
    command.chmod(0o644)
    try:
        service.execute(command)
    except service.CanonicalResearchServiceBlocked as exc:
        assert str(exc) == "BLOCKED_SUPERVISOR_COMMAND_FILE_MODE"
    else:
        raise AssertionError("public supervisor command file was accepted")


def test_main_never_renders_unexpected_exception_detail(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    command = tmp_path / "supervisor.json"
    _write_private(command, {})
    monkeypatch.setattr(
        service,
        "execute",
        lambda _path: (_ for _ in ()).throw(RuntimeError("sensitive detail")),
    )
    assert service.main(["execute", "--command-file", str(command)]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "status": "BLOCKED",
        "reason_code": "BLOCKED_RESEARCH_SUPERVISOR_FAILURE",
    }
