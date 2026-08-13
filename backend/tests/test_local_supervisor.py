import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.adapters.okx_demo import runtime_service, server_factory
from app.adapters.okx_demo.write_semantics import OkxDemoWriteBlocked


REPO_ROOT = Path(__file__).resolve().parents[2]
SUPERVISOR_PATH = REPO_ROOT / "scripts" / "local_supervisor.py"
RUNTIME_PATH = REPO_ROOT / "scripts" / "local_runtime.py"
LAUNCH_AGENT_PATH = REPO_ROOT / "scripts" / "macos_launch_agent.py"


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_supervisor_verify_does_not_restart_healthy_runtime(monkeypatch):
    supervisor = load_module(SUPERVISOR_PATH, "local_supervisor_healthy")
    calls = []

    def fake_run(command):
        calls.append(command)
        return {"status": "VERIFIED", "return_code": 0}

    monkeypatch.setattr(supervisor, "run_runtime", fake_run)

    assert supervisor.verify_or_recover() is True
    assert calls == ["verify"]


def test_supervisor_recovers_through_existing_runtime_boundary(monkeypatch):
    supervisor = load_module(SUPERVISOR_PATH, "local_supervisor_recovery")
    calls = []
    responses = iter(
        (
            {"status": "BLOCKED", "reason": "service missing", "return_code": 2},
            {"status": "READY", "services": [], "return_code": 0},
            {"status": "RUNNING", "return_code": 0},
            {"status": "VERIFIED", "return_code": 0},
        )
    )

    def fake_run(command):
        calls.append(command)
        return next(responses)

    monkeypatch.setattr(supervisor, "run_runtime", fake_run)

    assert supervisor.verify_or_recover() is True
    assert calls == ["verify", "down", "up", "verify"]


def test_supervisor_respects_fail_closed_down(monkeypatch):
    supervisor = load_module(SUPERVISOR_PATH, "local_supervisor_blocked")
    calls = []
    responses = iter(
        (
            {"status": "BLOCKED", "return_code": 2},
            {
                "status": "READY",
                "services": [{"service": "backend", "status": "BLOCKED"}],
                "return_code": 0,
            },
        )
    )

    def fake_run(command):
        calls.append(command)
        return next(responses)

    monkeypatch.setattr(supervisor, "run_runtime", fake_run)

    assert supervisor.verify_or_recover() is False
    assert calls == ["verify", "down"]


def test_launch_agent_plist_has_one_keepalive_supervisor(monkeypatch, tmp_path):
    agent = load_module(LAUNCH_AGENT_PATH, "macos_launch_agent_plist")
    binary = tmp_path / "freqtrade"
    binary.write_text("", encoding="utf-8")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-secret-must-not-enter-plist")

    payload = agent.plist_payload(binary)

    assert payload["Label"] == "com.he1met.freqtrade-ai.runtime"
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is True
    assert payload["ProgramArguments"] == [
        str(agent.BACKEND_PYTHON),
        str(agent.SUPERVISOR_SCRIPT),
    ]
    assert payload["EnvironmentVariables"]["FREQTRADE_BINARY"] == str(binary)
    assert str(Path.home() / ".local" / "bin") in payload["EnvironmentVariables"]["PATH"]
    assert "DATABASE_URL" not in payload["EnvironmentVariables"]
    assert "DEEPSEEK_API_KEY" not in payload["EnvironmentVariables"]
    assert "deepseek-secret-must-not-enter-plist" not in json.dumps(payload)
    assert not any("KEY" in key for key in payload["EnvironmentVariables"])


def test_launch_agent_refuses_install_from_noncanonical_worktree(monkeypatch):
    agent = load_module(
        LAUNCH_AGENT_PATH,
        "macos_launch_agent_noncanonical",
    )
    monkeypatch.setattr(
        agent,
        "REPO_ROOT",
        Path("/Users/local/.codex/worktrees/freqtrade-ai-449"),
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "app.core.config",
        SimpleNamespace(
            load_app_yaml=lambda _path: {
                "paths": {
                    "canonical_repo_root": "/Users/local/Developer/Freqtrade Ai"
                }
            }
        ),
    )

    with pytest.raises(
        agent.LaunchAgentBlocked,
        match="canonical repository",
    ):
        agent.require_canonical_repo()


def test_launch_agent_stop_managed_runtime_uses_runtime_down_contract(monkeypatch):
    agent = load_module(LAUNCH_AGENT_PATH, "macos_launch_agent_stop_runtime")
    calls = []
    monkeypatch.setattr(
        agent,
        "run",
        lambda command, **kwargs: (
            calls.append(tuple(command))
            or SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "services": [
                            {"service": "worker", "status": "stopped"},
                            {"service": "frontend", "status": "not-managed"},
                            {"service": "backend", "status": "stopped"},
                        ]
                    }
                ),
                stderr="",
            )
        ),
    )

    agent.stop_managed_runtime()

    assert calls == [
        (
            str(agent.BACKEND_PYTHON),
            str(agent.RUNTIME_SCRIPT),
            "down",
            "--json",
        )
    ]


def test_launch_agent_stop_managed_runtime_fails_closed_on_blocked_service(monkeypatch):
    agent = load_module(LAUNCH_AGENT_PATH, "macos_launch_agent_stop_blocked")
    monkeypatch.setattr(
        agent,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "services": [
                        {
                            "service": "worker",
                            "status": "BLOCKED",
                            "reason": "pid does not belong to the managed runtime",
                        }
                    ]
                }
            ),
            stderr="",
        ),
    )

    with pytest.raises(
        agent.LaunchAgentBlocked,
        match="managed runtime could not be stopped safely",
    ):
        agent.stop_managed_runtime()


def test_launch_agent_install_is_idempotent(monkeypatch, tmp_path):
    agent = load_module(LAUNCH_AGENT_PATH, "macos_launch_agent_install")
    binary = tmp_path / "freqtrade"
    binary.write_text("", encoding="utf-8")
    binary.chmod(0o755)
    calls = []
    monkeypatch.setattr(agent, "require_canonical_repo", lambda: None)

    monkeypatch.setattr(agent.sys, "platform", "darwin")
    monkeypatch.setattr(agent.shutil, "which", lambda _name: "/bin/launchctl")
    monkeypatch.setattr(agent, "resolve_freqtrade_binary", lambda: binary)
    monkeypatch.setattr(agent, "write_plist", lambda payload: calls.append(("write", payload)))
    monkeypatch.setattr(agent, "bootout", lambda: calls.append(("bootout",)))
    monkeypatch.setattr(
        agent,
        "wait_until_unloaded",
        lambda: calls.append(("wait-unloaded",)) or True,
    )
    monkeypatch.setattr(
        agent,
        "stop_managed_runtime",
        lambda: calls.append(("runtime-down",)),
    )
    monkeypatch.setattr(agent, "bootstrap_with_retry", lambda: calls.append(("bootstrap",)))
    monkeypatch.setattr(
        agent,
        "wait_until_running",
        lambda: calls.append(("wait-running",)) or True,
    )
    monkeypatch.setattr(
        agent,
        "run",
        lambda command, **kwargs: (
            calls.append(tuple(command))
            or SimpleNamespace(returncode=0, stdout="", stderr="")
        ),
    )

    result = agent.install()

    assert result["status"] == "INSTALLED"
    assert calls[0][0] == "write"
    assert calls[1:] == [
        ("bootout",),
        ("wait-unloaded",),
        ("runtime-down",),
        ("launchctl", "enable", agent.launchd_target()),
        ("bootstrap",),
        ("wait-running",),
    ]


def test_launch_agent_restart_stops_runtime_before_bootstrap(monkeypatch, tmp_path):
    agent = load_module(LAUNCH_AGENT_PATH, "macos_launch_agent_restart")
    plist_path = tmp_path / "runtime.plist"
    plist_path.write_text("", encoding="utf-8")
    calls = []
    monkeypatch.setattr(agent, "require_canonical_repo", lambda: None)

    monkeypatch.setattr(agent, "PLIST_PATH", plist_path)
    monkeypatch.setattr(agent, "bootout", lambda: calls.append(("bootout",)))
    monkeypatch.setattr(
        agent,
        "wait_until_unloaded",
        lambda: calls.append(("wait-unloaded",)) or True,
    )
    monkeypatch.setattr(
        agent,
        "stop_managed_runtime",
        lambda: calls.append(("runtime-down",)),
    )
    monkeypatch.setattr(
        agent,
        "run",
        lambda command, **kwargs: (
            calls.append(tuple(command))
            or SimpleNamespace(returncode=0, stdout="", stderr="")
        ),
    )
    monkeypatch.setattr(
        agent,
        "bootstrap_with_retry",
        lambda: calls.append(("bootstrap",)),
    )
    monkeypatch.setattr(
        agent,
        "wait_until_running",
        lambda: calls.append(("wait-running",)) or True,
    )

    result = agent.restart()

    assert result == {
        "status": "RESTARTED",
        "label": agent.LABEL,
    }
    assert calls == [
        ("bootout",),
        ("wait-unloaded",),
        ("runtime-down",),
        ("launchctl", "enable", agent.launchd_target()),
        ("bootstrap",),
        ("wait-running",),
    ]


def test_launch_agent_waits_until_old_job_is_unloaded(monkeypatch):
    agent = load_module(LAUNCH_AGENT_PATH, "macos_launch_agent_wait_unloaded")
    responses = iter(
        (
            SimpleNamespace(returncode=0, stdout="state = SIGTERMed", stderr=""),
            SimpleNamespace(returncode=0, stdout="state = SIGTERMed", stderr=""),
            SimpleNamespace(returncode=3, stdout="", stderr="not found"),
        )
    )
    sleeps = []
    monkeypatch.setattr(agent, "run", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(agent.time, "sleep", lambda seconds: sleeps.append(seconds))

    assert agent.wait_until_unloaded() is True
    assert sleeps == [0.25, 0.25]


def test_launch_agent_bootstrap_retries_transient_unload_race(monkeypatch):
    agent = load_module(LAUNCH_AGENT_PATH, "macos_launch_agent_bootstrap_retry")
    responses = iter(
        (
            SimpleNamespace(returncode=5, stdout="", stderr="Input/output error"),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
        )
    )
    calls = []
    monkeypatch.setattr(
        agent,
        "run",
        lambda command, **kwargs: calls.append(tuple(command)) or next(responses),
    )
    monkeypatch.setattr(agent.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))

    agent.bootstrap_with_retry()

    bootstrap_command = (
        "launchctl",
        "bootstrap",
        agent.launchd_domain(),
        str(agent.PLIST_PATH),
    )
    assert calls == [
        bootstrap_command,
        ("sleep", agent.BOOTSTRAP_RETRY_SECONDS),
        bootstrap_command,
    ]


def test_launch_agent_resolves_binary_through_backend_contract(monkeypatch, tmp_path):
    agent = load_module(LAUNCH_AGENT_PATH, "macos_launch_agent_resolver")
    backend_python = tmp_path / "python"
    backend_python.write_text("", encoding="utf-8")
    backend_python.chmod(0o755)
    binary = tmp_path / "freqtrade"
    binary.write_text("", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setattr(agent, "BACKEND_PYTHON", backend_python)
    monkeypatch.setattr(
        agent.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "freqtrade": {
                        "status": "READY",
                        "resolved_path": str(binary),
                        "reason": None,
                    }
                }
            ),
            stderr="",
        ),
    )

    assert agent.resolve_freqtrade_binary() == binary.resolve()


def test_launch_agent_status_does_not_echo_inherited_environment(monkeypatch):
    agent = load_module(LAUNCH_AGENT_PATH, "macos_launch_agent_status")
    launchctl_output = """
gui/501/com.he1met.freqtrade-ai.runtime = {
    state = running
    inherited environment = {
        HTTP_PROXY => http://user:should-not-appear@example.invalid:8080
    }
    pid = 12345
    last exit code = 0
}
"""
    monkeypatch.setattr(
        agent,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=launchctl_output,
            stderr="",
        ),
    )

    payload = agent.status()

    assert payload["state"] == "running"
    assert payload["pid"] == 12345
    assert payload["last_exit_code"] == "0"
    assert "should-not-appear" not in json.dumps(payload)


def test_runtime_command_parses_json_without_exposing_environment(monkeypatch):
    supervisor = load_module(SUPERVISOR_PATH, "local_supervisor_json")
    assert supervisor.COMMAND_TIMEOUT_SECONDS == 900
    moments = iter((10.0, 10.125))
    monkeypatch.setattr(
        supervisor.time,
        "monotonic",
        lambda: next(moments),
    )
    monkeypatch.setattr(
        supervisor.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"status": "VERIFIED"}),
            stderr="",
        ),
    )

    assert supervisor.run_runtime("verify") == {
        "status": "VERIFIED",
        "return_code": 0,
        "command_elapsed_ms": 125,
    }


def test_supervisor_command_budget_covers_runtime_startup_and_cleanup():
    supervisor = load_module(
        SUPERVISOR_PATH,
        "local_supervisor_command_budget",
    )
    runtime = load_module(
        REPO_ROOT / "scripts" / "local_runtime.py",
        "local_runtime_command_budget",
    )

    assert (
        supervisor.COMMAND_TIMEOUT_SECONDS
        > runtime.STARTUP_COMMAND_BUDGET_SECONDS
    )


def test_supervisor_propagates_allowlisted_okx_runtime_failure_diagnostic(
    monkeypatch,
    capsys,
):
    supervisor = load_module(
        SUPERVISOR_PATH,
        "local_supervisor_okx_failure_diagnostic",
    )
    supervisor.LAST_CREDENTIAL_GENERATION = "generation-old"
    responses = {
        "supervisor-capability": {
            "status": "READY",
            "_generation": "generation-new",
            "return_code": 0,
        },
        "down": {"services": [], "return_code": 0},
        "up": {
            "status": "BLOCKED",
            "return_code": 2,
            "startup_stage": "okx-runtime-readiness",
            "startup_stage_elapsed_ms": 1234,
            "okx_runtime_failure_stage": "read-attestation",
            "okx_runtime_failure_category": "ATTESTATION",
            "okx_runtime_failure_type": "OkxDemoCredentialsUnavailable",
            "command_elapsed_ms": 2000,
        },
    }
    monkeypatch.setattr(
        supervisor,
        "run_runtime",
        lambda command: responses[command],
    )

    assert supervisor.supervise_once() is False

    emitted = capsys.readouterr().out
    assert '"okx_runtime_failure_stage": "read-attestation"' in emitted
    assert '"okx_runtime_failure_category": "ATTESTATION"' in emitted
    assert (
        '"okx_runtime_failure_type": "OkxDemoCredentialsUnavailable"'
        in emitted
    )
    assert '"terminal_for_credential_generation": true' in emitted
    assert (
        supervisor.LAST_TERMINAL_CREDENTIAL_GENERATION
        == "generation-new"
    )


def test_same_generation_attestation_failure_becomes_terminal(
    monkeypatch,
    capsys,
):
    supervisor = load_module(
        SUPERVISOR_PATH,
        "local_supervisor_same_generation_terminal",
    )
    supervisor.LAST_CREDENTIAL_GENERATION = "generation-7"
    calls = []
    responses = {
        "supervisor-capability": {
            "status": "READY",
            "_generation": "generation-7",
            "return_code": 0,
        },
        "supervisor-thaw-openings": {
            "status": "BLOCKED",
            "return_code": 2,
        },
        "verify": {
            "status": "BLOCKED",
            "reason": "runtime unavailable",
            "return_code": 2,
        },
        "down": {"services": [], "return_code": 0},
        "up": {
            "status": "BLOCKED",
            "reason": "credential detail must not be emitted",
            "return_code": 2,
            "startup_stage": "okx-runtime-readiness",
            "okx_runtime_failure_stage": "read-attestation",
            "okx_runtime_failure_category": "ATTESTATION",
            "okx_runtime_failure_type": "OkxDemoCredentialsUnavailable",
        },
    }
    monkeypatch.setattr(
        supervisor,
        "run_runtime",
        lambda command: calls.append(command) or responses[command],
    )

    assert supervisor.supervise_once() is False
    assert calls == [
        "supervisor-capability",
        "supervisor-thaw-openings",
        "verify",
        "down",
        "up",
    ]
    assert (
        supervisor.LAST_TERMINAL_CREDENTIAL_GENERATION
        == "generation-7"
    )
    emitted = capsys.readouterr().out
    assert '"terminal_for_credential_generation": true' in emitted
    assert "credential detail must not be emitted" not in emitted

    calls.clear()
    assert supervisor.supervise_once() is False
    assert calls == ["supervisor-capability"]
    assert '"event": "runtime_recovery_suppressed"' in capsys.readouterr().out


def test_new_generation_clears_terminal_attestation_latch(monkeypatch):
    supervisor = load_module(
        SUPERVISOR_PATH,
        "local_supervisor_new_generation_after_terminal",
    )
    supervisor.LAST_CREDENTIAL_GENERATION = "generation-7"
    supervisor.LAST_TERMINAL_CREDENTIAL_GENERATION = "generation-7"
    calls = []
    responses = {
        "supervisor-capability": {
            "status": "READY",
            "_generation": "generation-8",
            "return_code": 0,
        },
        "down": {"services": [], "return_code": 0},
        "up": {"status": "RUNNING", "return_code": 0},
        "verify": {"status": "VERIFIED", "return_code": 0},
    }
    monkeypatch.setattr(
        supervisor,
        "run_runtime",
        lambda command: calls.append(command) or responses[command],
    )

    assert supervisor.supervise_once() is True
    assert calls == ["supervisor-capability", "down", "up", "verify"]
    assert supervisor.LAST_CREDENTIAL_GENERATION == "generation-8"
    assert supervisor.LAST_TERMINAL_CREDENTIAL_GENERATION is None


def test_unavailable_capability_does_not_bypass_terminal_latch(
    monkeypatch,
    capsys,
):
    supervisor = load_module(
        SUPERVISOR_PATH,
        "local_supervisor_terminal_capability_unavailable",
    )
    supervisor.LAST_TERMINAL_CREDENTIAL_GENERATION = "generation-7"
    calls = []
    monkeypatch.setattr(
        supervisor,
        "run_runtime",
        lambda command: (
            calls.append(command)
            or {
                "status": "BLOCKED",
                "return_code": 2,
            }
        ),
    )

    assert supervisor.supervise_once() is False
    assert calls == ["supervisor-capability"]
    emitted = capsys.readouterr().out
    assert '"credential_generation_status": "UNAVAILABLE"' in emitted
    assert '"event": "runtime_recovery_suppressed"' in emitted


def test_near_miss_attestation_diagnostic_is_not_terminal(monkeypatch):
    supervisor = load_module(
        SUPERVISOR_PATH,
        "local_supervisor_nonterminal_attestation",
    )
    responses = {
        "verify": {"status": "BLOCKED", "return_code": 2},
        "down": {"services": [], "return_code": 0},
        "up": {
            "status": "BLOCKED",
            "return_code": 2,
            "okx_runtime_failure_stage": "read-attestation",
            "okx_runtime_failure_category": "RUNTIME",
            "okx_runtime_failure_type": "OkxDemoCredentialsUnavailable",
        },
    }
    monkeypatch.setattr(
        supervisor,
        "run_runtime",
        lambda command: responses[command],
    )

    assert supervisor.verify_or_recover("generation-7") is False
    assert supervisor.LAST_TERMINAL_CREDENTIAL_GENERATION is None


def test_factory_diagnostic_crosses_runtime_sidecar_and_supervisor_allowlist(
    monkeypatch,
    capsys,
    tmp_path,
):
    lock_events = []

    class BlockedWriterLock:
        def __init__(self, path):
            lock_events.append(("init", path))

        def acquire(self):
            lock_events.append(("acquire",))
            raise OkxDemoWriteBlocked(
                "signature=secret-must-not-cross"
            )

        def release(self):
            lock_events.append(("release",))

    monkeypatch.setattr(
        server_factory,
        "OkxDemoWriterProcessLock",
        BlockedWriterLock,
    )
    with pytest.raises(
        runtime_service.OkxDemoRuntimeStartupBlocked,
    ) as captured:
        runtime_service._startup_call(
            "server-session",
            lambda: server_factory.create_okx_demo_server_session(
                {},
                lock_path=tmp_path / "writer.lock",
            ),
        )

    assert captured.value.stage == "writer-lock"
    assert captured.value.category == "WRITER"
    assert captured.value.cause_type == "OkxDemoWriteBlocked"
    assert [event[0] for event in lock_events] == [
        "init",
        "acquire",
        "release",
    ]
    failure_path = tmp_path / runtime_service.FAILURE_FILENAME
    assert runtime_service._write_startup_failure(
        failure_path,
        captured.value,
    )

    local_runtime = load_module(
        RUNTIME_PATH,
        "local_runtime_cross_layer_diagnostic",
    )
    parsed = local_runtime.okx_runtime_failure(tmp_path)
    assert parsed == {
        "stage": "writer-lock",
        "category": "WRITER",
        "cause_type": "OkxDemoWriteBlocked",
    }

    supervisor = load_module(
        SUPERVISOR_PATH,
        "local_supervisor_cross_layer_diagnostic",
    )
    responses = {
        "down": {"services": [], "return_code": 0},
        "up": {
            "status": "BLOCKED",
            "return_code": 2,
            "startup_stage": "okx-runtime-readiness",
            "startup_stage_elapsed_ms": 100,
            "okx_runtime_failure_stage": parsed["stage"],
            "okx_runtime_failure_category": parsed["category"],
            "okx_runtime_failure_type": parsed["cause_type"],
        },
    }
    monkeypatch.setattr(
        supervisor,
        "run_runtime",
        lambda command: responses[command],
    )

    assert supervisor.controlled_credential_restart("generation") is False

    emitted = capsys.readouterr().out
    assert '"okx_runtime_failure_stage": "writer-lock"' in emitted
    assert '"okx_runtime_failure_category": "WRITER"' in emitted
    assert "OkxDemoWriteBlocked" in emitted
    assert "secret-must-not-cross" not in emitted


def test_supervisor_rotates_credentials_through_controlled_down_up_verify(
    monkeypatch,
):
    supervisor = load_module(SUPERVISOR_PATH, "local_supervisor_rotation")
    calls = []
    supervisor.LAST_CREDENTIAL_GENERATION = "generation-old"
    responses = {
        "supervisor-capability": {
            "status": "READY",
            "_generation": "generation-new",
            "return_code": 0,
        },
        "down": {"services": [], "return_code": 0},
        "up": {"status": "RUNNING", "return_code": 0},
        "verify": {"status": "VERIFIED", "return_code": 0},
    }

    def fake_run(command):
        calls.append(command)
        return responses[command]

    monkeypatch.setattr(supervisor, "run_runtime", fake_run)

    assert supervisor.supervise_once() is True
    assert calls == ["supervisor-capability", "down", "up", "verify"]
    assert supervisor.LAST_CREDENTIAL_GENERATION == "generation-new"


def test_missing_keychain_does_not_stop_running_cleanup_capability(monkeypatch):
    supervisor = load_module(
        SUPERVISOR_PATH,
        "local_supervisor_missing_keychain",
    )
    calls = []
    responses = {
        "supervisor-capability": {
            "status": "BLOCKED",
            "return_code": 2,
        },
        "supervisor-freeze-openings": {
            "status": "BLOCKED_OPENINGS",
            "return_code": 0,
        },
        "verify": {"status": "BLOCKED_OPENINGS", "return_code": 0},
    }

    def fake_run(command):
        calls.append(command)
        return responses[command]

    monkeypatch.setattr(supervisor, "run_runtime", fake_run)

    assert supervisor.supervise_once() is True
    assert calls == [
        "supervisor-capability",
        "supervisor-freeze-openings",
        "verify",
    ]


def test_same_non_secret_generation_thaws_then_verifies(monkeypatch):
    supervisor = load_module(
        SUPERVISOR_PATH,
        "local_supervisor_same_generation",
    )
    supervisor.LAST_CREDENTIAL_GENERATION = "generation-7"
    calls = []
    responses = {
        "supervisor-capability": {
            "status": "READY",
            "_generation": "generation-7",
            "return_code": 0,
        },
        "supervisor-thaw-openings": {
            "status": "READY",
            "return_code": 0,
        },
        "verify": {"status": "VERIFIED", "return_code": 0},
    }
    monkeypatch.setattr(
        supervisor,
        "run_runtime",
        lambda command: calls.append(command) or responses[command],
    )

    assert supervisor.supervise_once() is True
    assert calls == [
        "supervisor-capability",
        "supervisor-thaw-openings",
        "verify",
    ]


def test_failed_rotation_does_not_commit_generation(monkeypatch):
    supervisor = load_module(
        SUPERVISOR_PATH,
        "local_supervisor_failed_rotation",
    )
    supervisor.LAST_CREDENTIAL_GENERATION = "generation-old"
    responses = {
        "supervisor-capability": {
            "status": "READY",
            "_generation": "generation-new",
            "return_code": 0,
        },
        "down": {
            "services": [
                {"service": "okx_runtime", "status": "BLOCKED"}
            ],
            "return_code": 0,
        },
    }
    monkeypatch.setattr(
        supervisor,
        "run_runtime",
        lambda command: responses[command],
    )

    assert supervisor.supervise_once() is False
    assert supervisor.LAST_CREDENTIAL_GENERATION == "generation-old"


def test_failed_rotation_emits_only_allowlisted_startup_diagnostic(
    monkeypatch,
    capsys,
):
    supervisor = load_module(
        SUPERVISOR_PATH,
        "local_supervisor_safe_startup_diagnostic",
    )
    responses = {
        "down": {"services": [], "return_code": 0},
        "up": {
            "status": "BLOCKED",
            "reason": "untrusted detail must not be logged",
            "startup_stage": "backend-readiness",
            "startup_stage_elapsed_ms": 23456,
            "command_elapsed_ms": 30000,
            "return_code": 2,
        },
    }
    monkeypatch.setattr(
        supervisor,
        "run_runtime",
        lambda command: responses[command],
    )

    assert supervisor.controlled_credential_restart("generation-new") is False

    emitted = capsys.readouterr().out
    assert '"runtime_stage": "backend-readiness"' in emitted
    assert '"runtime_stage_elapsed_ms": 23456' in emitted
    assert '"runtime_command_elapsed_ms": 30000' in emitted
    assert "untrusted detail" not in emitted


def test_failed_rotation_same_generation_backs_off_then_retries(monkeypatch):
    supervisor = load_module(
        SUPERVISOR_PATH,
        "local_supervisor_failed_rotation_cooldown",
    )
    supervisor.LAST_CREDENTIAL_GENERATION = "generation-old"
    now = [100.0]
    calls = []
    responses = {
        "supervisor-capability": {
            "status": "READY",
            "_generation": "generation-new",
            "return_code": 0,
        },
        "down": {
            "services": [
                {"service": "okx_runtime", "status": "BLOCKED"}
            ],
            "return_code": 0,
        },
    }
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        supervisor,
        "run_runtime",
        lambda command: calls.append(command) or responses[command],
    )

    assert supervisor.supervise_once() is False
    assert calls == ["supervisor-capability", "down"]
    assert supervisor.LAST_FAILED_CREDENTIAL_GENERATION == "generation-new"

    calls.clear()
    now[0] += supervisor.CREDENTIAL_RETRY_COOLDOWN_SECONDS - 1
    assert supervisor.supervise_once() is False
    assert calls == ["supervisor-capability"]

    calls.clear()
    now[0] += 1
    assert supervisor.supervise_once() is False
    assert calls == ["supervisor-capability", "down"]


def test_supervisor_cold_start_replaces_unknown_existing_child(monkeypatch):
    supervisor = load_module(
        SUPERVISOR_PATH,
        "local_supervisor_cold_start",
    )
    supervisor.LAST_CREDENTIAL_GENERATION = None
    calls = []
    responses = {
        "supervisor-capability": {
            "status": "READY",
            "_generation": "gen-2",
            "return_code": 0,
        },
        "down": {"services": [], "return_code": 0},
        "up": {"status": "RUNNING", "return_code": 0},
        "verify": {"status": "VERIFIED", "return_code": 0},
    }
    monkeypatch.setattr(
        supervisor,
        "run_runtime",
        lambda command: calls.append(command) or responses[command],
    )

    assert supervisor.supervise_once() is True
    assert calls == ["supervisor-capability", "down", "up", "verify"]
    assert supervisor.LAST_CREDENTIAL_GENERATION == "gen-2"
