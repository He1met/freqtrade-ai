import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "local_runtime.py"


def load_runtime_module():
    spec = importlib.util.spec_from_file_location("local_runtime", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_demo_database_is_persistent_inside_repository():
    runtime = load_runtime_module()

    state_dir = runtime.runtime_dir(None)

    assert state_dir == REPO_ROOT / ".freqtrade-ai" / "runtime"
    assert str(runtime.demo_database_url(state_dir)).endswith(".freqtrade-ai/runtime/demo.sqlite3")
    assert "/tmp/" not in runtime.demo_database_url(state_dir)


def test_dev_mode_ignores_inherited_database_url(monkeypatch):
    runtime = load_runtime_module()
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://unsafe:secret@example.com:5432/prod")
    monkeypatch.delenv("FREQTRADE_AI_DEV_DATABASE_URL", raising=False)

    with pytest.raises(runtime.RuntimeBlocked, match="inherited DATABASE_URL is ignored"):
        runtime.dev_database_url()


def test_dev_mode_rejects_remote_postgres(monkeypatch):
    runtime = load_runtime_module()
    monkeypatch.setenv(
        "FREQTRADE_AI_DEV_DATABASE_URL",
        "postgresql+psycopg://freqtrade:change_me@example.com:5432/freqtrade_ai",
    )

    with pytest.raises(runtime.RuntimeBlocked, match="localhost PostgreSQL"):
        runtime.dev_database_url()


def test_log_redaction_does_not_echo_secret_values():
    runtime = load_runtime_module()

    redacted = runtime.redact_line("DEEPSEEK_API_KEY=should-not-appear password: also-hidden")

    assert "should-not-appear" not in redacted
    assert "also-hidden" not in redacted
    assert redacted.count("***") == 2


def test_doctor_reports_missing_demo_schema_as_blocked():
    runtime = load_runtime_module()

    payload = runtime.doctor("demo", REPO_ROOT / ".freqtrade-ai" / "runtime-not-created")

    assert payload["schema"]["status"] == "BLOCKED"


def test_worker_has_dedicated_pid_log_and_backend_working_directory():
    runtime = load_runtime_module()

    assert runtime.PID_FILES["worker"] == "worker.pid"
    assert runtime.LOG_FILES["worker"] == "worker.log"
    assert runtime.SERVICE_PROCESS_MARKERS["worker"] == "app.workers.deepseek_backtest_worker"
    assert runtime.SERVICE_WORKING_DIRECTORIES["worker"] == REPO_ROOT / "backend"


def test_worker_pid_validation_requires_command_marker_and_backend_cwd(monkeypatch):
    runtime = load_runtime_module()
    responses = iter(
        (
            SimpleNamespace(stdout="python -m app.workers.deepseek_backtest_worker\n"),
            SimpleNamespace(stdout="n{}\n".format(REPO_ROOT / "backend")),
        )
    )
    monkeypatch.setattr(runtime.subprocess, "run", lambda *args, **kwargs: next(responses))

    assert runtime.is_managed_process(1234, "worker") is True


def test_worker_pid_validation_rejects_unrelated_process(monkeypatch):
    runtime = load_runtime_module()
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout="python unrelated.py\n"),
    )

    assert runtime.is_managed_process(1234, "worker") is False


def test_down_stops_worker_before_frontend_and_backend(monkeypatch, tmp_path):
    runtime = load_runtime_module()
    observed = []

    def fake_stop_service(state_dir, service):
        observed.append((state_dir, service))
        return {"service": service, "status": "stopped"}

    monkeypatch.setattr(runtime, "stop_service", fake_stop_service)

    payload = runtime.stop_all(tmp_path)

    assert [service for _, service in observed] == ["worker", "frontend", "backend"]
    assert [service["service"] for service in payload["services"]] == [
        "worker",
        "frontend",
        "backend",
    ]


def test_status_includes_backend_worker_and_frontend(monkeypatch, tmp_path):
    runtime = load_runtime_module()
    monkeypatch.setattr(
        runtime,
        "process_status",
        lambda state_dir, service: {"service": service, "running": True},
    )
    monkeypatch.setattr(runtime, "verify_demo_database", lambda state_dir: {"tables": 1})

    payload = runtime.current_status("demo", tmp_path)

    assert [service["service"] for service in payload["services"]] == [
        "backend",
        "worker",
        "frontend",
    ]


def test_start_launches_worker_with_expected_module(monkeypatch, tmp_path):
    runtime = load_runtime_module()
    observed = []
    monkeypatch.setattr(runtime, "backend_python", lambda: Path("/venv/bin/python"))
    monkeypatch.setattr(runtime, "frontend_vite", lambda: Path("/frontend/vite"))
    monkeypatch.setattr(runtime, "port_available", lambda port: True)
    monkeypatch.setattr(runtime, "initialise_demo_database", lambda state_dir: None)
    monkeypatch.setattr(runtime, "verify_demo_database", lambda state_dir: {"tables": 1})
    monkeypatch.setattr(runtime, "wait_for_url", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime, "wait_for_process", lambda *args, **kwargs: None)

    def fake_start_service(service, command, **kwargs):
        observed.append((service, list(command), kwargs["cwd"]))

    monkeypatch.setattr(runtime, "start_service", fake_start_service)

    runtime.start("demo", tmp_path)

    worker = next(item for item in observed if item[0] == "worker")
    assert worker[1] == [
        "/venv/bin/python",
        "-m",
        "app.workers.deepseek_backtest_worker",
    ]
    assert worker[2] == REPO_ROOT / "backend"


def test_verify_fails_closed_when_worker_is_not_running(monkeypatch, capsys):
    runtime = load_runtime_module()
    monkeypatch.setattr(
        runtime,
        "current_status",
        lambda mode, state_dir: {
            "mode": mode,
            "services": [
                {"service": "backend", "running": True},
                {"service": "worker", "running": False},
                {"service": "frontend", "running": True},
            ],
        },
    )
    monkeypatch.setattr(runtime, "verify_demo_database", lambda state_dir: {"tables": 1})

    exit_code = runtime.main(["verify", "--mode", "demo"])

    assert exit_code == 2
    assert "backend, worker, and frontend must all be running" in capsys.readouterr().out
