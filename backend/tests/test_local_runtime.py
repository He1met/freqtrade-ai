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


def test_runtime_database_defaults_to_one_canonical_postgres(monkeypatch):
    runtime = load_runtime_module()
    monkeypatch.delenv("DATABASE_URL", raising=False)

    assert runtime.runtime_database_url() == runtime.DEFAULT_DATABASE_URL


def test_runtime_environment_file_loads_only_non_secret_selectors(monkeypatch, tmp_path):
    runtime = load_runtime_module()
    config = tmp_path / "runtime.env"
    config.write_text(
        "DATABASE_URL=postgresql+psycopg://freqtrade:change_me@localhost:5432/freqtrade_ai\n"
        "FREQTRADE_BINARY=/Users/local/freqtrade_venv/bin/freqtrade\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("FREQTRADE_BINARY", raising=False)

    runtime.load_runtime_environment(config)

    assert runtime.os.environ["DATABASE_URL"].endswith("/freqtrade_ai")
    assert runtime.os.environ["FREQTRADE_BINARY"].endswith("/bin/freqtrade")


def test_runtime_environment_file_rejects_secret_or_unknown_keys(tmp_path):
    runtime = load_runtime_module()
    config = tmp_path / "runtime.env"
    config.write_text("DEEPSEEK_API_KEY=not-allowed\n", encoding="utf-8")

    with pytest.raises(runtime.RuntimeBlocked, match="not allowed"):
        runtime.load_runtime_environment(config)


def test_runtime_rejects_remote_or_noncanonical_database(monkeypatch):
    runtime = load_runtime_module()
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://freqtrade:change_me@example.com:5432/freqtrade_ai",
    )
    with pytest.raises(runtime.RuntimeBlocked, match="localhost PostgreSQL"):
        runtime.runtime_database_url()

    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://freqtrade:change_me@localhost:5432/another_database",
    )
    with pytest.raises(runtime.RuntimeBlocked, match="canonical freqtrade_ai"):
        runtime.runtime_database_url()


def test_log_redaction_does_not_echo_secret_values():
    runtime = load_runtime_module()

    redacted = runtime.redact_line("DEEPSEEK_API_KEY=should-not-appear password: also-hidden")

    assert "should-not-appear" not in redacted
    assert "also-hidden" not in redacted
    assert redacted.count("***") == 2


def test_doctor_uses_explicit_freqtrade_binary(monkeypatch, tmp_path):
    runtime = load_runtime_module()
    binary = tmp_path / "freqtrade"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("FREQTRADE_BINARY", str(binary))

    monkeypatch.setattr(runtime, "ensure_schema", lambda _url: None)
    payload = runtime.doctor(REPO_ROOT / ".freqtrade-ai" / "runtime-not-created")

    assert payload["checks"]["freqtrade_binary"] is True
    assert payload["database"]["kind"] == "postgresql"
    assert payload["schema"]["status"] == "READY"
    assert payload["freqtrade"]["status"] == "READY"
    assert payload["freqtrade"]["resolved_path"] == str(binary.resolve())


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
    monkeypatch.setattr(runtime, "ensure_schema", lambda _url: None)

    payload = runtime.current_status(tmp_path)

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
    monkeypatch.setattr(runtime, "ensure_schema", lambda _url: None)
    monkeypatch.setattr(runtime, "ensure_worker_queue_idle", lambda _url: None)
    monkeypatch.setattr(runtime, "wait_for_url", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime, "wait_for_process", lambda *args, **kwargs: None)

    def fake_start_service(service, command, **kwargs):
        observed.append((service, list(command), kwargs["cwd"]))

    monkeypatch.setattr(runtime, "start_service", fake_start_service)

    runtime.start(tmp_path)

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
        lambda state_dir: {
            "environment": "local",
            "services": [
                {"service": "backend", "running": True},
                {"service": "worker", "running": False},
                {"service": "frontend", "running": True},
            ],
        },
    )
    monkeypatch.setattr(runtime, "ensure_schema", lambda _url: None)

    exit_code = runtime.main(["verify"])

    assert exit_code == 2
    assert "backend, worker, and frontend must all be running" in capsys.readouterr().out


def test_worker_queue_must_be_idle(monkeypatch):
    runtime = load_runtime_module()
    monkeypatch.setattr(runtime, "backend_python", lambda: Path("/venv/bin/python"))
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=3),
    )

    with pytest.raises(runtime.RuntimeBlocked, match="worker queue is not idle"):
        runtime.ensure_worker_queue_idle(runtime.DEFAULT_DATABASE_URL)
