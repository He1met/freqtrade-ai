import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "local_runtime.py"


def test_make_runtime_commands_use_the_one_project_virtualenv():
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")

    assert "python3 scripts/local_runtime.py" not in makefile
    assert (
        makefile.count(
            "backend/.venv/bin/python scripts/local_runtime.py"
        )
        == 13
    )
    assert "python3 scripts/okx_demo_e2e.py" not in makefile
    assert (
        makefile.count(
            "backend/.venv/bin/python scripts/okx_demo_e2e.py"
        )
        == 2
    )


def load_runtime_module():
    spec = importlib.util.spec_from_file_location("local_runtime", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_wait_for_url_uses_a_bounded_slow_probe_timeout(monkeypatch):
    runtime = load_runtime_module()
    calls = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_urlopen(url, *, timeout):
        calls.append((url, timeout))
        return Response()

    monkeypatch.setattr(runtime, "urlopen", fake_urlopen)

    runtime.wait_for_url(
        "http://127.0.0.1:8000/readyz",
        "backend readiness",
        timeout_seconds=45,
    )

    assert calls == [
        (
            "http://127.0.0.1:8000/readyz",
            runtime.READINESS_PROBE_TIMEOUT_SECONDS,
        )
    ]


def test_wait_for_url_accepts_readiness_after_legacy_twenty_second_budget(
    monkeypatch,
):
    runtime = load_runtime_module()
    elapsed = [0.0]

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(runtime.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(
        runtime.time,
        "sleep",
        lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
    )

    def delayed_urlopen(_url, *, timeout):
        assert 0 < timeout <= runtime.READINESS_PROBE_TIMEOUT_SECONDS
        if elapsed[0] <= 20:
            raise runtime.URLError("backend still starting")
        return Response()

    monkeypatch.setattr(runtime, "urlopen", delayed_urlopen)

    runtime.wait_for_url(
        "http://127.0.0.1:8000/readyz",
        "backend readiness",
        timeout_seconds=runtime.BACKEND_STARTUP_TIMEOUT_SECONDS,
    )

    assert elapsed[0] > 20


def test_wait_for_url_still_fails_closed_at_explicit_budget(monkeypatch):
    runtime = load_runtime_module()
    elapsed = [0.0]

    monkeypatch.setattr(runtime.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(
        runtime.time,
        "sleep",
        lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
    )
    monkeypatch.setattr(
        runtime,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            runtime.URLError("backend unavailable")
        ),
    )

    with pytest.raises(
        runtime.RuntimeBlocked,
        match="backend readiness did not become reachable within 20 seconds",
    ):
        runtime.wait_for_url(
            "http://127.0.0.1:8000/readyz",
            "backend readiness",
            timeout_seconds=20,
        )

    assert elapsed[0] == 20


def test_supervisor_capability_short_process_avoids_heavy_app_imports_and_gc():
    harness = """
import importlib.util
import json
from pathlib import Path
import sys

script_path = Path({script_path!r})
spec = importlib.util.spec_from_file_location("isolated_local_runtime", script_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class ClearingBundle(dict):
    cleared = False

    def clear(self):
        self.cleared = True
        super().clear()

bundle = ClearingBundle({{
    "OKX_DEMO_API_KEY": "must-not-be-printed",
    "OKX_DEMO_API_SECRET": "must-not-be-printed",
}})
module.DEFAULT_RUNTIME_ENV_FILE = script_path.parent / "missing-runtime.env"
module.validate_okx_demo_execution_target()
module.read_okx_runtime_capability = lambda: (
    bundle,
    {{
        "status": "READY",
        "configured": True,
        "source": "keychain",
        "_generation": "fixture-generation",
    }},
)
exit_code = module.main(["supervisor-capability", "--json"])
print("RESULT:" + json.dumps({{
    "exit_code": exit_code,
    "cleared": bundle.cleared,
    "app_imported": any(
        name == "app" or name.startswith("app.") for name in sys.modules
    ),
    "pydantic_imported": any(
        name == "pydantic" or name.startswith("pydantic.") for name in sys.modules
    ),
}}))
raise SystemExit(exit_code)
""".format(script_path=str(SCRIPT_PATH))

    started = time.monotonic()
    completed = subprocess.run(
        [sys.executable, "-c", harness],
        cwd=str(REPO_ROOT),
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    elapsed = time.monotonic() - started

    assert completed.returncode == 0, completed.stderr
    assert elapsed < 5
    assert "must-not-be-printed" not in completed.stdout
    result_line = next(
        line for line in completed.stdout.splitlines() if line.startswith("RESULT:")
    )
    result = json.loads(result_line.removeprefix("RESULT:"))
    assert result == {
        "exit_code": 0,
        "cleared": True,
        "app_imported": False,
        "pydantic_imported": False,
    }


def test_down_main_releases_control_lock_on_normal_return(monkeypatch, tmp_path):
    runtime = load_runtime_module()
    runtime.REPO_ROOT = tmp_path
    runtime.DEFAULT_RUNTIME_ENV_FILE = tmp_path / "missing-runtime.env"
    state_dir = tmp_path / "runtime"
    monkeypatch.setattr(
        runtime,
        "stop_all",
        lambda _state_dir: {"status": "STOPPED", "services": []},
    )

    assert (
        runtime.main(
            ["down", "--runtime-dir", str(state_dir), "--json"]
        )
        == 0
    )

    lock_path = state_dir / runtime.CONTROL_LOCK_FILE
    with lock_path.open("r+") as handle:
        runtime.fcntl.flock(
            handle.fileno(),
            runtime.fcntl.LOCK_EX | runtime.fcntl.LOCK_NB,
        )
        runtime.fcntl.flock(handle.fileno(), runtime.fcntl.LOCK_UN)


def test_lightweight_constants_match_backend_okx_contract():
    runtime = load_runtime_module()
    from app.adapters.okx_demo import attestation_proof, credential_preflight
    from app.adapters.okx_demo import demo_canary

    assert runtime.EXECUTION_TARGET_ENV == credential_preflight.EXECUTION_TARGET_ENV
    assert runtime.ALLOW_REAL_FUNDS_ENV == credential_preflight.ALLOW_REAL_FUNDS_ENV
    assert runtime.REST_URL_ENV == credential_preflight.REST_URL_ENV
    assert runtime.OKX_DEMO_REST_URL == credential_preflight.OKX_DEMO_REST_URL
    assert (
        runtime.OKX_DEMO_REQUIRED_ENV_NAMES
        == credential_preflight.OKX_DEMO_REQUIRED_ENV_NAMES
    )
    assert (
        runtime.SAFE_OPERATOR_PREFLIGHT_REASONS
        == credential_preflight.SAFE_OPERATOR_PREFLIGHT_REASONS
    )
    assert runtime.ALLOW_DEMO_ORDER_ENV == demo_canary.ALLOW_DEMO_ORDER_ENV
    assert (
        runtime.OKX_DEMO_CANARY_ALLOWED_INSTRUMENTS
        == demo_canary.ALLOWED_INSTRUMENTS
    )
    assert (
        runtime.ATTESTATION_PROOF_KEY_ENV
        == attestation_proof.ATTESTATION_PROOF_KEY_ENV
    )


def install_ready_okx_runtime(monkeypatch, runtime):
    bundle = {
        "OKX_DEMO_API_KEY": "runtime-key",
        "OKX_DEMO_API_SECRET": "runtime-secret",
        "OKX_DEMO_API_PASSPHRASE": "runtime-passphrase",
        "OKX_DEMO_ACCOUNT_FINGERPRINT": "a" * 64,
        "FREQTRADE_AI_OKX_DEMO_ATTESTATION_PROOF_KEY": "74" * 32,
    }
    monkeypatch.setattr(
        runtime,
        "read_okx_runtime_capability",
        lambda: (
            dict(bundle),
            {
                "status": "READY",
                "configured": True,
                "source": "keychain",
                "_generation": "generation-test-1",
            },
        ),
    )
    monkeypatch.setattr(
        runtime,
        "validate_okx_demo_execution_target",
        lambda: None,
    )
    monkeypatch.setattr(
        runtime,
        "wait_for_okx_runtime",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        runtime,
        "cleanup_orphaned_managed_processes",
        lambda _state_dir: None,
    )
    monkeypatch.setattr(
        runtime,
        "read_operator_token",
        lambda: (
            "test-operator-token-with-at-least-32-characters",
            {
                "status": "READY",
                "configured": True,
                "source": "keychain",
            },
        ),
    )
    return bundle


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


@pytest.mark.parametrize(
    "line",
    [
        "postgresql://runtime:database-password@localhost/freqtrade_ai",
        (
            '{"database":"postgresql+psycopg://runtime:database-password'
            '@127.0.0.1/freqtrade_ai"}'
        ),
    ],
)
def test_log_redaction_hides_postgresql_passwords(line):
    runtime = load_runtime_module()

    redacted = runtime.redact_line(line)

    assert "database-password" not in redacted
    assert ":***@" in redacted


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


def test_read_operator_token_uses_only_the_dedicated_keychain_item(
    monkeypatch,
):
    runtime = load_runtime_module()
    sentinel = "operator-token-with-at-least-32-characters"
    observed = []
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setenv(runtime.OPERATOR_TOKEN_ENV, "stale-shell-value")
    monkeypatch.setattr(
        runtime,
        "_read_macos_keychain_item",
        lambda service: observed.append(service) or sentinel,
    )

    value, metadata = runtime.read_operator_token()

    assert value == sentinel
    assert metadata == {
        "status": "READY",
        "configured": True,
        "source": "keychain",
    }
    assert observed == [runtime.OPERATOR_TOKEN_KEYCHAIN_SERVICE]
    assert "stale-shell-value" not in str(metadata)
    assert sentinel not in str(metadata)


def test_operator_token_init_uses_interactive_keychain_prompt_without_argv_secret(
    monkeypatch,
):
    runtime = load_runtime_module()
    results = iter(
        (
            (None, {"status": "UNAVAILABLE"}),
            (
                "operator-token-with-at-least-32-characters",
                {"status": "READY"},
            ),
        )
    )
    observed = {}
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(runtime, "read_operator_token", lambda: next(results))
    monkeypatch.setattr(runtime.Path, "is_file", lambda _path: True)
    monkeypatch.setattr(runtime.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(runtime.os, "getuid", lambda: 501)
    monkeypatch.setattr(
        runtime.pwd,
        "getpwuid",
        lambda _uid: SimpleNamespace(pw_name="local-user"),
    )

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runtime.subprocess, "run", fake_run)

    payload = runtime.configure_operator_token()

    assert payload["status"] == "READY"
    assert payload["changed"] is True
    assert observed["command"] == [
        "/usr/bin/security",
        "add-generic-password",
        "-a",
        "local-user",
        "-s",
        runtime.OPERATOR_TOKEN_KEYCHAIN_SERVICE,
        "-w",
    ]
    assert observed["kwargs"] == {
        "cwd": str(REPO_ROOT),
        "check": False,
    }


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

    backend = runtime.service_environment(
        "backend",
        database_url,
        sentinel,
        operator_token="operator-token-from-keychain-123456",
    )
    worker = runtime.service_environment("worker", database_url, sentinel)
    frontend = runtime.service_environment("frontend", database_url, sentinel)

    assert backend["DATABASE_URL"] == database_url
    assert backend["DEEPSEEK_API_KEY"] == sentinel
    assert backend["STRATEGY_BLUEPRINT_PROVIDER"] == "deepseek"
    assert backend["STRATEGY_BLUEPRINT_MODEL"] == "deepseek-v4-pro"
    assert (
        backend["FREQTRADE_AI_OPERATOR_TOKEN"]
        == "operator-token-from-keychain-123456"
    )
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
        "FREQTRADE_AI_OKX_DEMO_ATTESTATION_PROOF_KEY": "74" * 32,
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


def test_runtime_capability_uses_non_secret_keychain_generation(monkeypatch):
    runtime = load_runtime_module()
    bundle = {
        name: "value-{}".format(index)
        for index, name in enumerate(
            runtime.OKX_DEMO_REQUIRED_ENV_NAMES,
            start=1,
        )
    }
    monkeypatch.setattr(
        runtime,
        "read_okx_demo_credentials",
        lambda: (
            dict(bundle),
            {"status": "READY", "configured": True, "source": "keychain"},
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_read_macos_keychain_item",
        lambda service: (
            "74" * 32
            if service == runtime.ATTESTATION_PROOF_KEYCHAIN_SERVICE
            else "generation-9"
        ),
    )

    credentials, metadata = runtime.read_okx_runtime_capability()

    assert credentials is not None
    assert metadata["_generation"] == "generation-9"
    assert "_revision" not in metadata
    assert not any(value in str(metadata) for value in bundle.values())
    credentials.clear()


def test_explicit_generation_rotation_writes_only_non_secret_metadata(
    monkeypatch,
):
    runtime = load_runtime_module()
    captured = {}
    secret_bundle = {
        name: "secret-{}".format(index)
        for index, name in enumerate(
            runtime.OKX_DEMO_REQUIRED_ENV_NAMES,
            start=1,
        )
    }
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(runtime.Path, "is_file", lambda _path: True)
    monkeypatch.setattr(
        runtime,
        "read_okx_demo_credentials",
        lambda: (
            secret_bundle,
            {"status": "READY", "configured": True, "source": "keychain"},
        ),
    )
    monkeypatch.setattr(runtime.os, "getuid", lambda: 501)
    monkeypatch.setattr(
        runtime.pwd,
        "getpwuid",
        lambda _uid: SimpleNamespace(pw_name="local-user"),
    )
    monkeypatch.setattr(
        runtime,
        "uuid4",
        lambda: SimpleNamespace(hex="generationvalue123"),
    )

    def fake_run(command, **kwargs):
        captured["command"] = list(command)
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(runtime.subprocess, "run", fake_run)

    payload = runtime.configure_okx_credential_generation()

    assert captured["command"][-2:] == ["-w", "generationvalue123"]
    assert runtime.OKX_DEMO_CREDENTIAL_GENERATION_SERVICE in captured["command"]
    assert payload["credential_generation"] == "UPDATED"
    assert "generationvalue123" not in str(payload)
    assert secret_bundle == {}


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
            "position_mode": "long_short_mode",
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
    monkeypatch.setattr(
        runtime,
        "_read_macos_keychain_item",
        lambda service: "74" * 32,
    )

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
    assert (
        captured["environment"][
            "FREQTRADE_AI_OKX_DEMO_ATTESTATION_PROOF_KEY"
        ]
        == "74" * 32
    )
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


def test_okx_preflight_surfaces_only_allowlisted_safe_child_reason(monkeypatch):
    runtime = load_runtime_module()
    credential_bundle = {
        "OKX_DEMO_API_KEY": "child-key",
        "OKX_DEMO_API_SECRET": "child-secret",
        "OKX_DEMO_API_PASSPHRASE": "child-passphrase",
        "OKX_DEMO_ACCOUNT_FINGERPRINT": "c" * 64,
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
    monkeypatch.setattr(
        runtime,
        "_read_macos_keychain_item",
        lambda _service: "74" * 32,
    )
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=2,
            stdout=json.dumps(
                {
                    "status": "BLOCKED",
                    "reason": runtime.IP_WHITELIST_REJECTED_REASON,
                }
            ),
            stderr="untrusted-child-output",
        ),
    )

    payload = runtime.run_okx_demo_preflight()

    assert payload["status"] == "BLOCKED"
    assert payload["reason"] == runtime.IP_WHITELIST_REJECTED_REASON
    assert "untrusted-child-output" not in str(payload)
    assert credential_bundle == {}


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


def test_okx_canary_without_explicit_flag_is_zero_keychain_and_zero_child(
    monkeypatch,
):
    runtime = load_runtime_module()
    monkeypatch.setattr(
        runtime,
        "read_okx_demo_credentials",
        lambda: pytest.fail("Keychain must not be read without explicit authorization"),
    )
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail(
            "child must not start without explicit authorization"
        ),
    )

    payload = runtime.run_okx_demo_canary(
        allow_demo_order=False,
        instrument="BTC-USDT-SWAP",
    )

    assert payload == {
        "status": "BLOCKED",
        "execution_target": "OKX_DEMO",
        "reason": "direct OKX Demo canary is permanently disabled; use canonical runtime one-shot grant",
    }


def test_okx_canary_cli_tombstone_is_exit_two_and_zero_capability(
    monkeypatch,
    capsys,
):
    runtime = load_runtime_module()
    monkeypatch.setattr(
        runtime,
        "read_okx_demo_credentials",
        lambda: pytest.fail("tombstone must not read Keychain"),
    )
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail(
            "tombstone must not start a child or network path"
        ),
    )

    exit_code = runtime.main(
        [
            "okx-demo-canary",
            "--allow-demo-order",
            "--instrument",
            "NOT-ALLOWLISTED",
            "--json",
        ]
    )

    assert exit_code == 2
    assert json.loads(capsys.readouterr().out)["status"] == "BLOCKED"


def test_okx_canary_with_missing_keychain_bundle_is_zero_child(monkeypatch):
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
        lambda *_args, **_kwargs: pytest.fail(
            "child must not start without complete Keychain bundle"
        ),
    )

    payload = runtime.run_okx_demo_canary(
        allow_demo_order=True,
        instrument="BTC-USDT-SWAP",
    )

    assert payload["status"] == "BLOCKED"
    assert payload["reason"] == (
        "direct OKX Demo canary is permanently disabled; "
        "use canonical runtime one-shot grant"
    )


def test_okx_canary_child_receives_exact_bundle_and_returns_only_safe_evidence(
    monkeypatch,
):
    runtime = load_runtime_module()
    monkeypatch.setattr(
        runtime,
        "read_okx_demo_credentials",
        lambda: pytest.fail("retired canary must never read Keychain"),
    )
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail(
            "retired canary must never start a subprocess or network child"
        ),
    )
    assert runtime.run_okx_demo_canary(
        allow_demo_order=True,
        instrument="NOT-ALLOWLISTED",
    )["status"] == "BLOCKED"


def test_okx_canary_parent_rejects_extra_or_raw_child_evidence(monkeypatch):
    runtime = load_runtime_module()
    payload = runtime._validate_okx_demo_canary_payload(
        {
            "status": "BLOCKED",
            "execution_target": "OKX_DEMO",
            "artifact_id": "b" * 32,
            "instrument": "BTC-USDT-SWAP",
            "evidence": {
                "cl_ord_id_sha256": "c" * 64,
                "order_id_sha256": None,
                "cleanup_cl_ord_id_sha256": None,
                "simulated_trading_header": True,
                "sequence": [],
            },
            "reason_code": "HISTORICAL_VALIDATOR_ONLY",
        }
    )
    assert payload["status"] == "BLOCKED"


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

    assert [service for _, service in observed] == [
        "okx_runtime",
        "frontend",
        "worker",
        "backend",
    ]
    assert [service["service"] for service in payload["services"]] == [
        "okx_runtime",
        "frontend",
        "worker",
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
        "okx_runtime",
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
    install_ready_okx_runtime(monkeypatch, runtime)

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


def test_start_uses_explicit_per_stage_readiness_budgets(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    url_waits = []
    process_waits = []
    monkeypatch.setattr(
        runtime,
        "backend_python",
        lambda: Path("/venv/bin/python"),
    )
    monkeypatch.setattr(
        runtime,
        "frontend_vite",
        lambda: Path("/frontend/vite"),
    )
    monkeypatch.setattr(runtime, "port_available", lambda _port: True)
    monkeypatch.setattr(runtime, "ensure_schema", lambda _url: None)
    monkeypatch.setattr(
        runtime,
        "ensure_worker_queue_idle",
        lambda _url: None,
    )
    monkeypatch.setattr(
        runtime,
        "wait_for_url",
        lambda url, description, timeout_seconds=20: url_waits.append(
            (url, description, timeout_seconds)
        ),
    )
    monkeypatch.setattr(
        runtime,
        "wait_for_process",
        lambda state_dir, service, timeout_seconds=2.0: process_waits.append(
            (state_dir, service, timeout_seconds)
        ),
    )
    install_ready_okx_runtime(monkeypatch, runtime)
    monkeypatch.setattr(
        runtime,
        "start_service",
        lambda *_args, **_kwargs: None,
    )

    payload = runtime.start(tmp_path)

    assert [item[2] for item in url_waits] == [
        runtime.BACKEND_STARTUP_TIMEOUT_SECONDS,
        runtime.FRONTEND_STARTUP_TIMEOUT_SECONDS,
    ]
    assert process_waits == [
        (tmp_path, "worker", runtime.WORKER_STARTUP_TIMEOUT_SECONDS)
    ]
    assert runtime.BACKEND_STARTUP_TIMEOUT_SECONDS == 240
    assert runtime.STARTUP_COMMAND_BUDGET_SECONDS == 830
    assert payload["startup"]["status"] == "READY"
    assert set(payload["startup"]["stage_elapsed_ms"]) == (
        runtime.SAFE_STARTUP_STAGES
    )


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
    install_ready_okx_runtime(monkeypatch, runtime)
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
    assert "FREQTRADE_AI_OPERATOR_TOKEN" in observed["backend"]
    assert "FREQTRADE_AI_OPERATOR_TOKEN" not in observed["worker"]
    assert "FREQTRADE_AI_OPERATOR_TOKEN" not in observed["frontend"]
    assert payload["credentials"]["deepseek_provider"] == {
        "status": "READY",
        "configured": True,
        "source": "keychain",
    }
    assert payload["credentials"]["local_action"] == {
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
    install_ready_okx_runtime(monkeypatch, runtime)
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
    assert payload["credentials"]["deepseek_provider"]["status"] == "UNAVAILABLE"
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
    assert (
        "backend, worker, frontend, and OKX runtime must all be running"
        in capsys.readouterr().out
    )


def test_verify_uses_canonical_readiness_budgets(monkeypatch, tmp_path):
    runtime = load_runtime_module()
    runtime.REPO_ROOT = tmp_path.resolve()
    runtime.DEFAULT_RUNTIME_ENV_FILE = tmp_path / "missing-runtime.env"
    state_dir = runtime.REPO_ROOT / "runtime"
    waits = []
    services = [
        {"service": service, "running": True}
        for service in ("backend", "worker", "frontend", "okx_runtime")
    ]
    monkeypatch.setattr(
        runtime,
        "current_status",
        lambda _state_dir: {
            "environment": "local",
            "services": services,
            "execution_target": {"status": "READY", "active": "OKX_DEMO"},
            "credentials": {
                "okx_demo": {"status": "READY"},
                "local_action": {"status": "READY"},
            },
            "database": {"schema": "verified"},
            "okx_runtime": {"status": "READY"},
        },
    )
    monkeypatch.setattr(runtime, "ensure_schema", lambda _url: None)
    monkeypatch.setattr(
        runtime,
        "wait_for_url",
        lambda url, description, timeout_seconds=20: waits.append(
            (url, description, timeout_seconds)
        ),
    )

    assert runtime.main(["verify", "--runtime-dir", str(state_dir)]) == 0

    assert waits == [
        (
            "http://127.0.0.1:{}/readyz".format(runtime.BACKEND_PORT),
            "backend readiness",
            runtime.BACKEND_STARTUP_TIMEOUT_SECONDS,
        ),
        (
            "http://127.0.0.1:{}/".format(runtime.FRONTEND_PORT),
            "frontend",
            runtime.FRONTEND_STARTUP_TIMEOUT_SECONDS,
        ),
    ]


def test_worker_queue_must_be_idle(monkeypatch):
    runtime = load_runtime_module()
    calls = []
    monkeypatch.setattr(runtime, "backend_python", lambda: Path("/venv/bin/python"))

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=3)

    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        fake_run,
    )

    with pytest.raises(runtime.RuntimeBlocked, match="worker queue is not idle"):
        runtime.ensure_worker_queue_idle(runtime.DEFAULT_DATABASE_URL)

    command = calls[0][0][0]
    assert "status IN ('PENDING','RUNNING')" in command[2]
    assert "status IN ('pending','running')" not in command[2]


def test_worker_queue_read_failure_reports_acl_or_schema_problem(monkeypatch):
    runtime = load_runtime_module()
    monkeypatch.setattr(runtime, "backend_python", lambda: Path("/venv/bin/python"))
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1),
    )

    with pytest.raises(
        runtime.RuntimeBlocked,
        match="worker queue read failed; verify runtime database ACL and schema",
    ):
        runtime.ensure_worker_queue_idle(runtime.DEFAULT_DATABASE_URL)


def test_start_missing_okx_keychain_is_zero_process(monkeypatch, tmp_path):
    runtime = load_runtime_module()
    started = []
    monkeypatch.setattr(
        runtime,
        "read_operator_token",
        lambda: (
            "test-operator-token-with-at-least-32-characters",
            {"status": "READY", "configured": True, "source": "keychain"},
        ),
    )
    monkeypatch.setattr(runtime, "backend_python", lambda: Path("/venv/bin/python"))
    monkeypatch.setattr(runtime, "frontend_vite", lambda: Path("/frontend/vite"))
    monkeypatch.setattr(runtime, "port_available", lambda _port: True)
    monkeypatch.setattr(runtime, "ensure_schema", lambda _url: None)
    monkeypatch.setattr(runtime, "ensure_worker_queue_idle", lambda _url: None)
    monkeypatch.setattr(
        runtime,
        "validate_okx_demo_execution_target",
        lambda: None,
    )
    monkeypatch.setattr(
        runtime,
        "read_okx_runtime_capability",
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
        runtime,
        "start_service",
        lambda service, *_args, **_kwargs: started.append(service),
    )

    with pytest.raises(runtime.RuntimeBlocked, match="bundle unavailable"):
        runtime.start(tmp_path)

    assert started == []


def test_partial_start_failure_cleans_all_services(monkeypatch, tmp_path):
    runtime = load_runtime_module()
    install_ready_okx_runtime(monkeypatch, runtime)
    started = []
    stopped = []
    monkeypatch.setattr(runtime, "backend_python", lambda: Path("/venv/bin/python"))
    monkeypatch.setattr(runtime, "frontend_vite", lambda: Path("/frontend/vite"))
    monkeypatch.setattr(runtime, "port_available", lambda _port: True)
    monkeypatch.setattr(runtime, "ensure_schema", lambda _url: None)
    monkeypatch.setattr(runtime, "ensure_worker_queue_idle", lambda _url: None)
    monkeypatch.setattr(runtime, "wait_for_url", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime, "wait_for_process", lambda *_args, **_kwargs: None)

    def fake_start(service, *_args, **_kwargs):
        started.append(service)
        if service == "frontend":
            raise RuntimeError("crash")

    monkeypatch.setattr(runtime, "start_service", fake_start)
    monkeypatch.setattr(
        runtime,
        "stop_service",
        lambda _state_dir, service: (
            stopped.append(service)
            or {"service": service, "status": "stopped"}
        ),
    )

    with pytest.raises(
        runtime.RuntimeBlocked,
        match="managed stage.*cleaned up",
    ) as raised:
        runtime.start(tmp_path)

    assert raised.value.safe_stage == "frontend-readiness"
    assert isinstance(raised.value.elapsed_ms, int)
    assert started == ["backend", "worker", "frontend"]
    assert stopped == list(runtime.SERVICE_STOP_ORDER)


def test_okx_startup_failure_diagnostic_survives_cleanup(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    install_ready_okx_runtime(monkeypatch, runtime)
    started = []
    stopped = []
    monkeypatch.setattr(
        runtime,
        "backend_python",
        lambda: Path("/venv/bin/python"),
    )
    monkeypatch.setattr(
        runtime,
        "frontend_vite",
        lambda: Path("/frontend/vite"),
    )
    monkeypatch.setattr(runtime, "port_available", lambda _port: True)
    monkeypatch.setattr(runtime, "ensure_schema", lambda _url: None)
    monkeypatch.setattr(runtime, "ensure_worker_queue_idle", lambda _url: None)
    monkeypatch.setattr(runtime, "wait_for_url", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runtime,
        "wait_for_process",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        runtime,
        "start_service",
        lambda service, *_args, **_kwargs: started.append(service),
    )
    monkeypatch.setattr(
        runtime,
        "wait_for_okx_runtime",
        lambda _state_dir: (_ for _ in ()).throw(
            runtime.RuntimeBlocked(
                "safe generic failure",
                okx_runtime_failure_stage="writer-capability",
                okx_runtime_failure_category="WRITER",
                okx_runtime_failure_type="IntegrityError",
            )
        ),
    )
    monkeypatch.setattr(
        runtime,
        "stop_service",
        lambda _state_dir, service: (
            stopped.append(service)
            or {"service": service, "status": "stopped"}
        ),
    )

    with pytest.raises(runtime.RuntimeBlocked) as raised:
        runtime.start(tmp_path)

    assert raised.value.safe_stage == "okx-runtime-readiness"
    assert (
        raised.value.okx_runtime_failure_stage
        == "writer-capability"
    )
    assert raised.value.okx_runtime_failure_type == "IntegrityError"
    assert raised.value.okx_runtime_failure_category == "WRITER"
    assert started == list(runtime.SERVICE_START_ORDER)
    assert stopped == list(runtime.SERVICE_STOP_ORDER)


def test_parent_clears_stale_failure_before_child_can_exit_without_main(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    install_ready_okx_runtime(monkeypatch, runtime)
    stale_path = tmp_path / runtime.OKX_RUNTIME_FAILURE_FILE
    stale_path.write_text(
        json.dumps(
            {
                "status": "BLOCKED",
                "stage": "read-attestation",
                "category": "ATTESTATION",
                "cause_type": "OkxDemoCredentialsUnavailable",
            }
        ),
        encoding="utf-8",
    )
    stale_path.chmod(0o600)
    started = []
    monkeypatch.setattr(
        runtime,
        "backend_python",
        lambda: Path("/venv/bin/python"),
    )
    monkeypatch.setattr(
        runtime,
        "frontend_vite",
        lambda: Path("/frontend/vite"),
    )
    monkeypatch.setattr(runtime, "port_available", lambda _port: True)
    monkeypatch.setattr(runtime, "ensure_schema", lambda _url: None)
    monkeypatch.setattr(runtime, "ensure_worker_queue_idle", lambda _url: None)
    monkeypatch.setattr(runtime, "wait_for_url", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runtime,
        "wait_for_process",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        runtime,
        "start_service",
        lambda service, *_args, **_kwargs: started.append(service),
    )

    def child_exited_before_main(_state_dir):
        assert not stale_path.exists()
        assert runtime.okx_runtime_failure(tmp_path) == {}
        raise runtime.RuntimeBlocked("child exited before main")

    monkeypatch.setattr(
        runtime,
        "wait_for_okx_runtime",
        child_exited_before_main,
    )
    monkeypatch.setattr(
        runtime,
        "stop_service",
        lambda _state_dir, service: {
            "service": service,
            "status": "stopped",
        },
    )

    with pytest.raises(runtime.RuntimeBlocked) as captured:
        runtime.start(tmp_path)

    assert started == list(runtime.SERVICE_START_ORDER)
    assert captured.value.okx_runtime_failure_stage is None
    assert captured.value.okx_runtime_failure_category is None
    assert captured.value.okx_runtime_failure_type is None


def test_cleanup_stale_runtime_removes_dead_pid_and_readiness(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    (tmp_path / runtime.PID_FILES["okx_runtime"]).write_text(
        "12345\n",
        encoding="utf-8",
    )
    (tmp_path / runtime.OKX_RUNTIME_READY_FILE).write_text(
        "{}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runtime, "process_running", lambda _pid: False)

    runtime.cleanup_stale_runtime_state(tmp_path)

    assert not (tmp_path / runtime.PID_FILES["okx_runtime"]).exists()
    assert not (tmp_path / runtime.OKX_RUNTIME_READY_FILE).exists()


def test_okx_runtime_readiness_reports_blocked_openings_without_secrets(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    pid = 4321
    (tmp_path / runtime.OKX_RUNTIME_READY_FILE).write_text(
        json.dumps(
            {
                "status": "BLOCKED_OPENINGS",
                "execution_target": "OKX_DEMO",
                "adapter": "ATTESTED",
                "reconciliation": "DRIFTED",
                "writer": "UNIQUE",
                "pid": pid,
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / runtime.OKX_RUNTIME_READY_FILE).chmod(0o600)
    monkeypatch.setattr(
        runtime,
        "process_status",
        lambda _state_dir, _service: {
            "service": "okx_runtime",
            "pid": pid,
            "running": True,
        },
    )
    monkeypatch.setattr(
        runtime,
        "is_managed_process",
        lambda candidate, service: (
            candidate == pid and service == "okx_runtime"
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_writer_lock_holder",
        lambda _state_dir: pid,
    )

    assert runtime.okx_runtime_readiness(tmp_path) == {
        "status": "BLOCKED_OPENINGS",
        "execution_target": "OKX_DEMO",
        "adapter": "ATTESTED",
        "reconciliation": "DRIFTED",
        "writer": "UNIQUE",
    }


def test_okx_runtime_readiness_accepts_recovery_only_without_opening_ready(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    pid = 4322
    (tmp_path / runtime.OKX_RUNTIME_READY_FILE).write_text(
        json.dumps(
            {
                "status": "RECOVERY_ONLY",
                "execution_target": "OKX_DEMO",
                "adapter": "ATTESTED",
                "reconciliation": "DRIFTED",
                "writer": "UNIQUE",
                "pid": pid,
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / runtime.OKX_RUNTIME_READY_FILE).chmod(0o600)
    monkeypatch.setattr(
        runtime,
        "process_status",
        lambda *_args: {"pid": pid, "running": True},
    )
    monkeypatch.setattr(runtime, "is_managed_process", lambda *_args: True)
    monkeypatch.setattr(runtime, "_writer_lock_holder", lambda *_args: pid)

    readiness = runtime.okx_runtime_readiness(tmp_path)
    assert readiness["status"] == "RECOVERY_ONLY"
    assert readiness["reconciliation"] == "DRIFTED"


def test_okx_runtime_startup_returns_for_recovery_only(monkeypatch, tmp_path):
    runtime = load_runtime_module()
    moments = iter((0.0, 1.0))
    monkeypatch.setattr(runtime.time, "monotonic", lambda: next(moments))
    monkeypatch.setattr(
        runtime,
        "okx_runtime_readiness",
        lambda _state_dir: {"status": "RECOVERY_ONLY"},
    )
    runtime.wait_for_okx_runtime(tmp_path)


def test_okx_runtime_startup_allows_authenticated_recovery_after_twenty_seconds(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    moments = iter((0.0, 21.0))
    monkeypatch.setattr(runtime.time, "monotonic", lambda: next(moments))
    monkeypatch.setattr(
        runtime,
        "okx_runtime_readiness",
        lambda _state_dir: {"status": "READY"},
    )

    runtime.wait_for_okx_runtime(tmp_path)

    assert runtime.OKX_RUNTIME_STARTUP_TIMEOUT_SECONDS == 300


def test_okx_runtime_startup_fails_closed_when_child_exits(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    moments = iter((0.0, 1.0))
    monkeypatch.setattr(runtime.time, "monotonic", lambda: next(moments))
    monkeypatch.setattr(
        runtime,
        "okx_runtime_readiness",
        lambda _state_dir: {"status": "BLOCKED"},
    )
    monkeypatch.setattr(
        runtime,
        "process_status",
        lambda _state_dir, _service: {"running": False},
    )

    with pytest.raises(runtime.RuntimeBlocked, match="did not establish"):
        runtime.wait_for_okx_runtime(tmp_path)


def test_okx_runtime_startup_propagates_only_safe_failure_stage_and_type(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    moments = iter((0.0, 1.0))
    monkeypatch.setattr(runtime.time, "monotonic", lambda: next(moments))
    monkeypatch.setattr(
        runtime,
        "okx_runtime_readiness",
        lambda _state_dir: {"status": "BLOCKED"},
    )
    monkeypatch.setattr(
        runtime,
        "process_status",
        lambda _state_dir, _service: {"running": False},
    )
    failure_path = tmp_path / runtime.OKX_RUNTIME_FAILURE_FILE
    failure_path.write_text(
        json.dumps(
            {
                "status": "BLOCKED",
                "stage": "read-attestation",
                "category": "ATTESTATION",
                "cause_type": "OkxDemoCredentialsUnavailable",
            }
        ),
        encoding="utf-8",
    )
    failure_path.chmod(0o600)

    with pytest.raises(runtime.RuntimeBlocked) as captured:
        runtime.wait_for_okx_runtime(tmp_path)

    assert captured.value.okx_runtime_failure_stage == "read-attestation"
    assert captured.value.okx_runtime_failure_category == "ATTESTATION"
    assert (
        captured.value.okx_runtime_failure_type
        == "OkxDemoCredentialsUnavailable"
    )


def test_okx_runtime_failure_rejects_unsafe_or_unexpected_evidence(tmp_path):
    runtime = load_runtime_module()
    failure_path = tmp_path / runtime.OKX_RUNTIME_FAILURE_FILE
    failure_path.write_text(
        json.dumps(
            {
                "status": "BLOCKED",
                "stage": "writer-capability",
                "category": "WRITER",
                "cause_type": "IntegrityError",
                "secret": "must-not-be-read",
            }
        ),
        encoding="utf-8",
    )
    failure_path.chmod(0o600)
    assert runtime.okx_runtime_failure(tmp_path) == {}

    failure_path.write_text(
        json.dumps(
            {
                "status": "BLOCKED",
                "stage": "writer-capability",
                "category": "WRITER",
                "cause_type": "IntegrityError",
            }
        ),
        encoding="utf-8",
    )
    failure_path.chmod(0o644)
    assert runtime.okx_runtime_failure(tmp_path) == {}

    failure_path.write_text(
        json.dumps(
            {
                "status": "BLOCKED",
                "stage": "read-attestation",
                "category": "ATTESTATION",
                "cause_type": "SensitiveButValidIdentifier",
            }
        ),
        encoding="utf-8",
    )
    failure_path.chmod(0o600)
    assert runtime.okx_runtime_failure(tmp_path) == {}


def test_okx_runtime_startup_fails_closed_after_bounded_timeout(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    moments = iter((0.0, 300.0))
    monkeypatch.setattr(runtime.time, "monotonic", lambda: next(moments))

    with pytest.raises(runtime.RuntimeBlocked, match="did not establish"):
        runtime.wait_for_okx_runtime(tmp_path)


def test_repeated_start_refuses_without_stopping_healthy_runtime(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    stopped = []
    monkeypatch.setattr(
        runtime,
        "process_status",
        lambda _state_dir, service: {
            "service": service,
            "pid": 9001 if service == "backend" else None,
            "running": service == "backend",
        },
    )
    monkeypatch.setattr(
        runtime,
        "stop_all",
        lambda _state_dir: stopped.append(True),
    )

    with pytest.raises(runtime.RuntimeBlocked, match="repeated up was refused"):
        runtime.start(tmp_path)

    assert stopped == []


def test_orphan_cleanup_signals_only_marker_and_cwd_verified_process(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    signals = []
    process_snapshots = []
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *_args, **_kwargs: (
            process_snapshots.append(True)
            or SimpleNamespace(
                stdout=(
                    "321 python -m app.adapters.okx_demo.runtime_service\n"
                    "654 python unrelated.py\n"
                )
            )
        ),
    )
    monkeypatch.setattr(
        runtime,
        "is_managed_process",
        lambda pid, service: pid == 321 and service == "okx_runtime",
    )
    running = iter((True, False))
    monkeypatch.setattr(
        runtime,
        "process_running",
        lambda _pid: next(running),
    )
    monkeypatch.setattr(
        runtime.os,
        "killpg",
        lambda pid, signum: signals.append((pid, signum)),
    )
    monkeypatch.setattr(runtime.time, "sleep", lambda _seconds: None)

    runtime.cleanup_orphaned_managed_processes(tmp_path)

    assert signals == [(321, runtime.signal.SIGTERM)]
    assert process_snapshots == [True]


def test_stop_service_preserves_pid_when_process_group_cannot_be_signaled(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    pid = 321
    pid_path = tmp_path / runtime.PID_FILES["backend"]
    pid_path.write_text("321\n", encoding="utf-8")
    pid_path.chmod(0o600)
    monkeypatch.setattr(runtime, "process_running", lambda _pid: True)
    monkeypatch.setattr(
        runtime,
        "is_managed_process",
        lambda candidate, service: candidate == pid and service == "backend",
    )
    monkeypatch.setattr(
        runtime.os,
        "killpg",
        lambda *_args: (_ for _ in ()).throw(PermissionError),
    )

    result = runtime.stop_service(tmp_path, "backend")

    assert result == {
        "service": "backend",
        "status": "BLOCKED",
        "pid": pid,
        "reason": "managed process group could not be signaled safely",
    }
    assert pid_path.exists()


def test_credential_loss_freezes_openings_without_stopping_writer(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    stopped = []
    monkeypatch.setattr(
        runtime,
        "process_status",
        lambda _state_dir, _service: {
            "service": "okx_runtime",
            "pid": 123,
            "running": True,
        },
    )
    monkeypatch.setattr(
        runtime,
        "okx_runtime_readiness",
        lambda _state_dir: {"status": "BLOCKED_OPENINGS"},
    )
    monkeypatch.setattr(
        runtime,
        "stop_all",
        lambda _state_dir: stopped.append(True),
    )

    result = runtime.freeze_okx_openings(tmp_path)

    assert result["status"] == "BLOCKED_OPENINGS"
    assert (
        tmp_path / runtime.OPENINGS_FREEZE_FILE
    ).read_text(encoding="utf-8") == "BLOCKED_OPENINGS\n"
    assert stopped == []


def test_readiness_rejects_reused_pid_even_when_writer_lock_is_held(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    pid = 777
    path = tmp_path / runtime.OKX_RUNTIME_READY_FILE
    path.write_text(
        json.dumps(
            {
                "status": "READY",
                "execution_target": "OKX_DEMO",
                "adapter": "ATTESTED",
                "reconciliation": "RECONCILED",
                "writer": "UNIQUE",
                "pid": pid,
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    monkeypatch.setattr(
        runtime,
        "process_status",
        lambda *_args: {
            "service": "okx_runtime",
            "pid": pid,
            "running": True,
        },
    )
    monkeypatch.setattr(
        runtime,
        "is_managed_process",
        lambda *_args: False,
    )
    monkeypatch.setattr(
        runtime,
        "_writer_lock_holder",
        lambda _state_dir: pid,
    )

    assert runtime.okx_runtime_readiness(tmp_path)["status"] == "BLOCKED"


def test_incomplete_startup_cleanup_never_claims_clean(monkeypatch, tmp_path):
    runtime = load_runtime_module()
    monkeypatch.setattr(
        runtime,
        "process_status",
        lambda _state_dir, service: {
            "service": service,
            "pid": 88 if service == "okx_runtime" else None,
            "running": service == "okx_runtime",
        },
    )
    monkeypatch.setattr(
        runtime,
        "orphaned_managed_process_map",
        lambda _state_dir, services: {
            service: [] for service in services
        },
    )
    monkeypatch.setattr(runtime, "_writer_lock_holder", lambda _path: 88)

    with pytest.raises(runtime.RuntimeBlocked, match="cleanup is incomplete"):
        runtime.require_complete_startup_cleanup(
            tmp_path,
            {
                "services": [
                    {"service": "okx_runtime", "status": "BLOCKED"}
                ]
            },
        )


def test_recent_logs_refuses_symlink(monkeypatch, tmp_path):
    runtime = load_runtime_module()
    target = tmp_path / "private.txt"
    target.write_text("password=must-not-appear\n", encoding="utf-8")
    (tmp_path / runtime.LOG_FILES["backend"]).symlink_to(target)

    payload = runtime.recent_logs(tmp_path, 10)

    assert payload["backend"]["status"] == "BLOCKED"
    assert "must-not-appear" not in str(payload)
