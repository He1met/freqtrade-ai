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


def test_read_deepseek_api_key_uses_fixed_macos_keychain_contract(monkeypatch):
    runtime = load_runtime_module()
    sentinel = "test-keychain-value-not-for-logs"
    observed = {}

    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(runtime.Path, "is_file", lambda _path: True)
    monkeypatch.setattr(runtime.os, "getuid", lambda: 501)
    monkeypatch.setattr(
        runtime.pwd,
        "getpwuid",
        lambda uid: SimpleNamespace(pw_name="local-user") if uid == 501 else None,
    )

    def fake_run(command, **kwargs):
        observed["command"] = list(command)
        observed["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout=sentinel + "\n", stderr="")

    monkeypatch.setattr(runtime.subprocess, "run", fake_run)

    value, metadata = runtime.read_deepseek_api_key()

    assert value == sentinel
    assert metadata == {
        "status": "READY",
        "configured": True,
        "source": "keychain",
    }
    assert sentinel not in str(metadata)
    assert observed["command"] == [
        "/usr/bin/security",
        "find-generic-password",
        "-a",
        "local-user",
        "-s",
        runtime.DEEPSEEK_KEYCHAIN_SERVICE,
        "-w",
    ]
    assert observed["kwargs"] == {
        "cwd": str(REPO_ROOT),
        "check": False,
        "capture_output": True,
        "text": True,
        "timeout": runtime.KEYCHAIN_TIMEOUT_SECONDS,
        "stdin": runtime.subprocess.DEVNULL,
    }


@pytest.mark.parametrize(
    ("result", "raised"),
    [
        (SimpleNamespace(returncode=44, stdout="", stderr="item not found"), None),
        (SimpleNamespace(returncode=0, stdout="\n", stderr=""), None),
        (None, TimeoutError),
    ],
)
def test_read_deepseek_api_key_fails_closed_without_exposing_keychain_errors(
    monkeypatch,
    result,
    raised,
):
    runtime = load_runtime_module()
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(runtime.Path, "is_file", lambda _path: True)
    monkeypatch.setattr(runtime.os, "getuid", lambda: 501)
    monkeypatch.setattr(
        runtime.pwd,
        "getpwuid",
        lambda _uid: SimpleNamespace(pw_name="local-user"),
    )

    def fake_run(*_args, **_kwargs):
        if raised is TimeoutError:
            raise runtime.subprocess.TimeoutExpired(
                cmd="/usr/bin/security",
                timeout=runtime.KEYCHAIN_TIMEOUT_SECONDS,
                output="must-not-be-rendered",
                stderr="keychain-error-must-not-be-rendered",
            )
        return result

    monkeypatch.setattr(runtime.subprocess, "run", fake_run)

    value, metadata = runtime.read_deepseek_api_key()

    assert value is None
    assert metadata == {
        "status": "UNAVAILABLE",
        "configured": False,
        "source": "keychain",
        "reason": "Keychain item is missing or inaccessible",
    }
    assert "must-not-be-rendered" not in str(metadata)
    assert "item not found" not in str(metadata)


def test_service_environment_limits_credentials_to_required_services(monkeypatch):
    runtime = load_runtime_module()
    database_url = runtime.DEFAULT_DATABASE_URL
    sentinel = "test-deepseek-key"
    inherited_secrets = {
        "DEEPSEEK_API_KEY": "stale-shell-key",
        "FREQTRADE_AI_OPERATOR_TOKEN": "operator-token",
        "BINANCE_API_KEY": "binance-key",
        "BINANCE_API_SECRET": "binance-secret",
        "OKX_API_KEY": "okx-key",
        "OKX_API_SECRET": "okx-secret",
        "OKX_API_PASSPHRASE": "okx-passphrase",
        "OKX_DEMO_API_KEY": "okx-demo-key",
        "OKX_DEMO_API_SECRET": "okx-demo-secret",
        "OKX_DEMO_API_PASSPHRASE": "okx-demo-passphrase",
        "MIMO_API_KEY": "mimo-key",
        "OPENAI_API_KEY": "openai-key",
    }
    for key, value in inherited_secrets.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DATABASE_URL", "postgresql://stale.invalid/other")
    monkeypatch.setenv("PATH", "/safe/path")

    backend = runtime.service_environment("backend", database_url, sentinel)
    worker = runtime.service_environment("worker", database_url, sentinel)
    frontend = runtime.service_environment("frontend", database_url, sentinel)

    assert backend["DATABASE_URL"] == database_url
    assert backend["DEEPSEEK_API_KEY"] == sentinel
    for key, value in inherited_secrets.items():
        if key != "DEEPSEEK_API_KEY":
            assert backend[key] == value

    assert worker["DATABASE_URL"] == database_url
    assert worker["DEEPSEEK_API_KEY"] == sentinel
    assert not (set(inherited_secrets) - {"DEEPSEEK_API_KEY"}) & set(worker)

    assert "DATABASE_URL" not in frontend
    assert not set(inherited_secrets) & set(frontend)
    assert frontend["APP_ENV"] == "local"
    assert frontend["VITE_ENABLE_DEV_FIXTURES"] == "false"
    assert frontend["VITE_FREQUI_URL"] == ""
    assert frontend["PATH"] == "/safe/path"
    assert runtime.os.environ["DEEPSEEK_API_KEY"] == "stale-shell-key"


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


def test_start_injects_keychain_key_only_into_backend_and_worker(monkeypatch, tmp_path):
    runtime = load_runtime_module()
    sentinel = "test-keychain-runtime-value"
    observed = {}
    monkeypatch.setenv("DEEPSEEK_API_KEY", "stale-inherited-value")
    monkeypatch.setattr(runtime, "backend_python", lambda: Path("/venv/bin/python"))
    monkeypatch.setattr(runtime, "frontend_vite", lambda: Path("/frontend/vite"))
    monkeypatch.setattr(runtime, "port_available", lambda _port: True)
    monkeypatch.setattr(runtime, "ensure_schema", lambda _url: None)
    monkeypatch.setattr(runtime, "ensure_worker_queue_idle", lambda _url: None)
    monkeypatch.setattr(runtime, "wait_for_url", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime, "wait_for_process", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        runtime,
        "read_deepseek_api_key",
        lambda: (
            sentinel,
            {"status": "READY", "configured": True, "source": "keychain"},
        ),
    )

    def fake_start_service(service, _command, **kwargs):
        observed[service] = kwargs["environment"]

    monkeypatch.setattr(runtime, "start_service", fake_start_service)

    payload = runtime.start(tmp_path)

    assert observed["backend"]["DEEPSEEK_API_KEY"] == sentinel
    assert observed["worker"]["DEEPSEEK_API_KEY"] == sentinel
    assert "DEEPSEEK_API_KEY" not in observed["frontend"]
    assert payload["credentials"]["deepseek_api_key"] == {
        "status": "READY",
        "configured": True,
        "source": "keychain",
    }
    assert sentinel not in str(payload)
    assert runtime.os.environ["DEEPSEEK_API_KEY"] == "stale-inherited-value"


def test_start_omits_stale_inherited_key_when_keychain_is_unavailable(monkeypatch, tmp_path):
    runtime = load_runtime_module()
    observed = {}
    monkeypatch.setenv("DEEPSEEK_API_KEY", "stale-inherited-value")
    monkeypatch.setattr(runtime, "backend_python", lambda: Path("/venv/bin/python"))
    monkeypatch.setattr(runtime, "frontend_vite", lambda: Path("/frontend/vite"))
    monkeypatch.setattr(runtime, "port_available", lambda _port: True)
    monkeypatch.setattr(runtime, "ensure_schema", lambda _url: None)
    monkeypatch.setattr(runtime, "ensure_worker_queue_idle", lambda _url: None)
    monkeypatch.setattr(runtime, "wait_for_url", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime, "wait_for_process", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        runtime,
        "read_deepseek_api_key",
        lambda: (
            None,
            {
                "status": "UNAVAILABLE",
                "configured": False,
                "source": "keychain",
                "reason": "Keychain item is missing or inaccessible",
            },
        ),
    )

    def fake_start_service(service, _command, **kwargs):
        observed[service] = kwargs["environment"]

    monkeypatch.setattr(runtime, "start_service", fake_start_service)

    payload = runtime.start(tmp_path)

    assert all("DEEPSEEK_API_KEY" not in environment for environment in observed.values())
    assert payload["status"] == "RUNNING"
    assert payload["credentials"]["deepseek_api_key"]["status"] == "UNAVAILABLE"
    assert "stale-inherited-value" not in str(payload)


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
