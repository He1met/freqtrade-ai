import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[2]
SUPERVISOR_PATH = REPO_ROOT / "scripts" / "local_supervisor.py"
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


def test_launch_agent_plist_has_one_keepalive_supervisor(tmp_path):
    agent = load_module(LAUNCH_AGENT_PATH, "macos_launch_agent_plist")
    binary = tmp_path / "freqtrade"
    binary.write_text("", encoding="utf-8")

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
    assert not any("KEY" in key for key in payload["EnvironmentVariables"])


def test_launch_agent_install_is_idempotent(monkeypatch, tmp_path):
    agent = load_module(LAUNCH_AGENT_PATH, "macos_launch_agent_install")
    binary = tmp_path / "freqtrade"
    binary.write_text("", encoding="utf-8")
    binary.chmod(0o755)
    calls = []

    monkeypatch.setattr(agent.sys, "platform", "darwin")
    monkeypatch.setattr(agent.shutil, "which", lambda _name: "/bin/launchctl")
    monkeypatch.setattr(agent, "resolve_freqtrade_binary", lambda: binary)
    monkeypatch.setattr(agent, "write_plist", lambda payload: calls.append(("write", payload)))
    monkeypatch.setattr(agent, "bootout", lambda: calls.append(("bootout",)))
    monkeypatch.setattr(agent, "wait_until_running", lambda: True)
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
    assert calls[1] == ("bootout",)
    assert ("launchctl", "enable", agent.launchd_target()) in calls
    assert ("launchctl", "bootstrap", agent.launchd_domain(), str(agent.PLIST_PATH)) in calls


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
    }
