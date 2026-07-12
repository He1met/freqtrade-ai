import importlib.util
from pathlib import Path

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
