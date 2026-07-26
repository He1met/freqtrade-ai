import importlib.util
import json
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
        "UNRELATED_SECRET_TOKEN": "unknown-secret-must-not-inherit",
        "STRATEGY_BLUEPRINT_PROVIDER": "fake",
        "STRATEGY_BLUEPRINT_MODEL": "shell-model",
        "STRATEGY_BLUEPRINT_BASE_URL": "https://attacker.invalid",
        "STRATEGY_BLUEPRINT_API_KEY_ENV": "FREQTRADE_AI_OPERATOR_TOKEN",
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
    assert backend["STRATEGY_BLUEPRINT_PROVIDER"] == "deepseek"
    assert backend["STRATEGY_BLUEPRINT_MODEL"] == "deepseek-v4-pro"
    assert backend["FREQTRADE_AI_OPERATOR_TOKEN"] == "operator-token"
    assert not (
        set(inherited_secrets)
        - {
            "DEEPSEEK_API_KEY",
            "FREQTRADE_AI_OPERATOR_TOKEN",
            "STRATEGY_BLUEPRINT_PROVIDER",
            "STRATEGY_BLUEPRINT_MODEL",
        }
    ) & set(backend)

    assert worker["DATABASE_URL"] == database_url
    assert worker["DEEPSEEK_API_KEY"] == sentinel
    assert worker["STRATEGY_BLUEPRINT_PROVIDER"] == "deepseek"
    assert worker["STRATEGY_BLUEPRINT_MODEL"] == "deepseek-v4-pro"
    assert not (
        set(inherited_secrets)
        - {
            "DEEPSEEK_API_KEY",
            "STRATEGY_BLUEPRINT_PROVIDER",
            "STRATEGY_BLUEPRINT_MODEL",
        }
    ) & set(worker)

    assert "DATABASE_URL" not in frontend
    assert not set(inherited_secrets) & set(frontend)
    assert frontend["APP_ENV"] == "local"
    assert frontend["VITE_ENABLE_DEV_FIXTURES"] == "false"
    assert frontend["VITE_FREQUI_URL"] == ""
    assert frontend["PATH"] == "/safe/path"
    assert all(
        environment["FREQTRADE_AI_DISABLE_ENV_FILE"] == "1"
        for environment in (backend, worker, frontend)
    )
    assert runtime.os.environ["DEEPSEEK_API_KEY"] == "stale-shell-key"


def test_clean_environment_remains_database_only(monkeypatch):
    runtime = load_runtime_module()
    monkeypatch.setenv("STRATEGY_BLUEPRINT_PROVIDER", "deepseek")
    monkeypatch.setenv("STRATEGY_BLUEPRINT_MODEL", "shell-model")

    environment = runtime.clean_environment(runtime.DEFAULT_DATABASE_URL)

    assert environment["DATABASE_URL"] == runtime.DEFAULT_DATABASE_URL
    assert environment["APP_ENV"] == "local"
    assert "STRATEGY_BLUEPRINT_PROVIDER" not in environment
    assert "STRATEGY_BLUEPRINT_MODEL" not in environment


def test_managed_child_environment_never_reads_or_reconstructs_repo_dotenv(
    monkeypatch,
):
    runtime = load_runtime_module()
    forbidden = {
        "STRATEGY_BLUEPRINT_PROVIDER": "deepseek",
        "STRATEGY_BLUEPRINT_BASE_URL": "https://attacker.invalid",
        "STRATEGY_BLUEPRINT_API_KEY_ENV": "OKX_DEMO_API_SECRET",
        "OKX_DEMO_API_KEY": "must-not-load",
        "OKX_DEMO_API_SECRET": "must-not-load",
        "OKX_DEMO_API_PASSPHRASE": "must-not-load",
    }
    for name, value in forbidden.items():
        monkeypatch.setenv(name, value)

    environment = runtime.base_service_environment()

    assert not set(forbidden) & set(environment)
    assert "must-not-load" not in str(environment)


def test_okx_adapter_is_the_only_environment_receiving_complete_bundle(monkeypatch):
    runtime = load_runtime_module()
    bundle = {
        "OKX_DEMO_API_KEY": "adapter-key",
        "OKX_DEMO_API_SECRET": "adapter-secret",
        "OKX_DEMO_API_PASSPHRASE": "adapter-passphrase",
        "OKX_DEMO_ACCOUNT_FINGERPRINT": "a" * 64,
    }
    for key, value in bundle.items():
        monkeypatch.setenv(key, "stale-" + value)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setenv("ALL_PROXY", "socks5://proxy.invalid:1080")
    monkeypatch.setenv("SSL_CERT_FILE", "/tmp/untrusted-ca.pem")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/tmp/untrusted-requests-ca.pem")

    adapter = runtime.service_environment(
        "okx_adapter",
        runtime.DEFAULT_DATABASE_URL,
        None,
        bundle,
    )
    backend = runtime.service_environment(
        "backend",
        runtime.DEFAULT_DATABASE_URL,
        None,
        bundle,
    )

    assert {name: adapter[name] for name in bundle} == bundle
    assert adapter["FREQTRADE_AI_EXECUTION_TARGET"] == "OKX_DEMO"
    assert adapter["FREQTRADE_AI_ALLOW_REAL_FUNDS"] == "false"
    assert adapter["FREQTRADE_AI_OKX_DEMO_REST_URL"] == "https://openapi.okx.com"
    assert "DATABASE_URL" not in adapter
    assert not {
        "HTTPS_PROXY",
        "ALL_PROXY",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
    } & set(adapter)
    assert not set(bundle) & set(backend)


@pytest.mark.parametrize(
    "bundle",
    [
        None,
        {"OKX_DEMO_API_KEY": "key"},
        {
            "OKX_DEMO_API_KEY": "key",
            "OKX_DEMO_API_SECRET": "secret",
            "OKX_DEMO_API_PASSPHRASE": "passphrase",
            "OKX_DEMO_ACCOUNT_FINGERPRINT": "a" * 64,
            "OKX_API_KEY": "unexpected",
        },
    ],
)
def test_okx_adapter_environment_rejects_non_exact_bundle(bundle):
    runtime = load_runtime_module()

    with pytest.raises(runtime.RuntimeBlocked, match="bundle is incomplete"):
        runtime.service_environment(
            "okx_adapter",
            runtime.DEFAULT_DATABASE_URL,
            None,
            bundle,
        )


def test_read_okx_demo_credentials_uses_four_fixed_keychain_items(monkeypatch):
    runtime = load_runtime_module()
    observed_services = []
    values = {
        service: "value-{}".format(index)
        for index, service in enumerate(
            runtime.OKX_DEMO_KEYCHAIN_SERVICES.values(),
            start=1,
        )
    }
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(runtime, "validate_okx_demo_execution_target", lambda: None)

    def fake_read(service):
        observed_services.append(service)
        return values[service]

    monkeypatch.setattr(runtime, "_read_macos_keychain_item", fake_read)

    credentials, metadata = runtime.read_okx_demo_credentials()

    assert observed_services == list(runtime.OKX_DEMO_KEYCHAIN_SERVICES.values())
    assert credentials == {
        name: values[service]
        for name, service in runtime.OKX_DEMO_KEYCHAIN_SERVICES.items()
    }
    assert metadata == {
        "status": "READY",
        "configured": True,
        "source": "keychain",
    }
    assert not any(value in str(metadata) for value in values.values())


def test_onboarding_reader_uses_only_three_signing_keychain_items(monkeypatch):
    runtime = load_runtime_module()
    observed_services = []
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(runtime, "validate_okx_demo_execution_target", lambda: None)

    def fake_read(service):
        observed_services.append(service)
        return "value-{}".format(len(observed_services))

    monkeypatch.setattr(runtime, "_read_macos_keychain_item", fake_read)

    credentials, metadata = runtime.read_okx_demo_onboarding_credentials()

    assert observed_services == [
        runtime.OKX_DEMO_KEYCHAIN_SERVICES[name]
        for name in runtime.OKX_DEMO_CREDENTIAL_ENV_NAMES
    ]
    assert set(credentials or {}) == set(runtime.OKX_DEMO_CREDENTIAL_ENV_NAMES)
    assert (
        runtime.OKX_DEMO_KEYCHAIN_SERVICES["OKX_DEMO_ACCOUNT_FINGERPRINT"]
        not in observed_services
    )
    assert metadata == {
        "status": "READY",
        "configured": True,
        "source": "keychain",
    }


def test_read_okx_demo_credentials_fails_atomically_without_env_fallback(monkeypatch):
    runtime = load_runtime_module()
    sentinels = {
        "OKX_DEMO_API_KEY": "shell-key-must-be-ignored",
        "OKX_DEMO_API_SECRET": "shell-secret-must-be-ignored",
        "OKX_DEMO_API_PASSPHRASE": "shell-passphrase-must-be-ignored",
        "OKX_DEMO_ACCOUNT_FINGERPRINT": "b" * 64,
    }
    for name, value in sentinels.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(runtime, "validate_okx_demo_execution_target", lambda: None)
    responses = iter(("keychain-key", None))
    monkeypatch.setattr(
        runtime,
        "_read_macos_keychain_item",
        lambda _service: next(responses),
    )

    credentials, metadata = runtime.read_okx_demo_credentials()

    assert credentials is None
    assert metadata["status"] == "BLOCKED"
    assert metadata["configured"] is False
    assert not any(value in str(metadata) for value in sentinels.values())


def test_macos_keychain_reader_uses_service_without_rendering_errors(monkeypatch):
    runtime = load_runtime_module()
    sentinel = "keychain-value-not-for-output"
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

    value = runtime._read_macos_keychain_item(
        runtime.OKX_DEMO_KEYCHAIN_SERVICES["OKX_DEMO_API_KEY"]
    )

    assert value == sentinel
    assert observed["command"] == [
        "/usr/bin/security",
        "find-generic-password",
        "-a",
        "local-user",
        "-s",
        "freqtrade-ai/okx-demo-api-key",
        "-w",
    ]
    assert observed["kwargs"]["timeout"] == runtime.KEYCHAIN_TIMEOUT_SECONDS
    assert observed["kwargs"]["stdin"] is runtime.subprocess.DEVNULL


def test_okx_preflight_child_receives_bundle_and_parent_returns_only_attestation(
    monkeypatch,
):
    runtime = load_runtime_module()
    credential_bundle = {
        "OKX_DEMO_API_KEY": "child-key",
        "OKX_DEMO_API_SECRET": "child-secret",
        "OKX_DEMO_API_PASSPHRASE": "child-passphrase",
        "OKX_DEMO_ACCOUNT_FINGERPRINT": "c" * 64,
    }
    captured = {}
    ready = {
        "status": "READY",
        "execution_target": "OKX_DEMO",
        "remote_account_evidence": {
            "authenticated_demo_response": True,
            "identity_present": True,
            "fingerprint_match": True,
            "permissions": {"read": True, "trade": True, "withdraw": False},
            "account_level": "2",
            "position_mode": "net_mode",
        },
        "local_target_contract": {
            "product_type": "SWAP",
            "margin_mode": "isolated",
            "allow_real_funds": False,
        },
        "request_contract": {
            "method": "GET",
            "path": "/api/v5/account/config",
            "simulated_trading_header": True,
        },
        "unexpected_remote_field": "child-output-must-not-be-forwarded",
    }
    monkeypatch.setattr(
        runtime,
        "read_okx_demo_credentials",
        lambda: (
            credential_bundle,
            {"status": "READY", "configured": True, "source": "keychain"},
        ),
    )
    monkeypatch.setattr(runtime, "backend_python", lambda: Path("/venv/bin/python"))

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["environment"] = dict(kwargs["env"])
        return SimpleNamespace(returncode=0, stdout=json.dumps(ready), stderr="")

    monkeypatch.setattr(runtime.subprocess, "run", fake_run)

    payload = runtime.run_okx_demo_preflight()

    assert captured["command"] == [
        "/venv/bin/python",
        "-m",
        "app.adapters.okx_demo.credential_preflight",
    ]
    assert {
        name: captured["environment"][name]
        for name in runtime.OKX_DEMO_REQUIRED_ENV_NAMES
    } == {
        "OKX_DEMO_API_KEY": "child-key",
        "OKX_DEMO_API_SECRET": "child-secret",
        "OKX_DEMO_API_PASSPHRASE": "child-passphrase",
        "OKX_DEMO_ACCOUNT_FINGERPRINT": "c" * 64,
    }
    assert payload["status"] == "READY"
    assert payload["credentials"]["source"] == "keychain"
    assert not any(
        value in str(payload)
        for value in (
            "child-key",
            "child-secret",
            "child-passphrase",
            "c" * 64,
        )
    )
    assert "child-output-must-not-be-forwarded" not in str(payload)
    assert credential_bundle == {}
    assert "okx_adapter" not in runtime.PID_FILES


def test_okx_preflight_does_not_spawn_when_keychain_bundle_is_missing(monkeypatch):
    runtime = load_runtime_module()
    monkeypatch.setattr(
        runtime,
        "read_okx_demo_credentials",
        lambda: (
            None,
            {
                "status": "BLOCKED",
                "configured": False,
                "source": "keychain",
                "reason": "bundle unavailable",
            },
        ),
    )
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("preflight child must not start"),
    )

    payload = runtime.run_okx_demo_preflight()

    assert payload["status"] == "BLOCKED"
    assert payload["credentials"]["configured"] is False


def test_okx_account_pin_child_receives_only_signing_bundle_and_is_redacted(
    monkeypatch,
):
    runtime = load_runtime_module()
    credential_bundle = {
        "OKX_DEMO_API_KEY": "onboarding-key",
        "OKX_DEMO_API_SECRET": "onboarding-secret",
        "OKX_DEMO_API_PASSPHRASE": "onboarding-passphrase",
    }
    captured = {}
    monkeypatch.setattr(
        runtime,
        "read_okx_demo_onboarding_credentials",
        lambda: (
            credential_bundle,
            {"status": "READY", "configured": True, "source": "keychain"},
        ),
    )
    monkeypatch.setattr(runtime, "backend_python", lambda: Path("/venv/bin/python"))

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["environment"] = dict(kwargs["env"])
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "status": "READY",
                    "execution_target": "OKX_DEMO",
                    "account_fingerprint_pinned": True,
                }
            ),
            stderr="untrusted-child-output",
        )

    monkeypatch.setattr(runtime.subprocess, "run", fake_run)

    payload = runtime.run_okx_demo_account_pin()

    assert captured["command"] == [
        "/venv/bin/python",
        "-m",
        "app.adapters.okx_demo.credential_preflight",
        "--pin-account",
    ]
    assert {
        name: captured["environment"][name]
        for name in runtime.OKX_DEMO_CREDENTIAL_ENV_NAMES
    } == {
        "OKX_DEMO_API_KEY": "onboarding-key",
        "OKX_DEMO_API_SECRET": "onboarding-secret",
        "OKX_DEMO_API_PASSPHRASE": "onboarding-passphrase",
    }
    assert "OKX_DEMO_ACCOUNT_FINGERPRINT" not in captured["environment"]
    assert payload["account_fingerprint_pinned"] is True
    assert not any(
        value in str(payload)
        for value in (
            "onboarding-key",
            "onboarding-secret",
            "onboarding-passphrase",
            "untrusted-child-output",
        )
    )
    assert credential_bundle == {}
    assert "okx_onboarding" not in runtime.PID_FILES


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
