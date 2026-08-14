from __future__ import annotations

import inspect
import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import create_engine

from app.canonical_v13.api import API_PREFIX
from app.canonical_v13 import production
from app.canonical_v13.research_persistence import research_service_principal


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"


def test_source_layout_imports_from_an_unrelated_working_directory(tmp_path) -> None:
    modules = sorted(
        f"app.canonical_v13.{path.stem}"
        for path in (BACKEND_ROOT / "app" / "canonical_v13").glob("*.py")
        if path.name != "__init__.py"
    )
    command = (
        "import importlib; "
        f"modules={modules!r}; "
        "[importlib.import_module(name) for name in modules]; "
        "print(len(modules))"
    )
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(BACKEND_ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        "FREQTRADE_AI_DISABLE_ENV_FILE": "1",
        "FREQTRADE_AI_TEST_DISABLE_ENV_FILE": "1",
    }
    result = subprocess.run(
        [sys.executable, "-c", command],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.stdout.strip() == str(len(modules))


def test_dependencies_and_legacy_bootstrap_boundaries_are_explicit() -> None:
    requirements = (BACKEND_ROOT / "requirements.txt").read_text()
    for dependency in ("fastapi==", "sqlalchemy==", "psycopg[binary]==", "pydantic=="):
        assert dependency in requirements

    legacy_main = (BACKEND_ROOT / "app" / "main.py").read_text()
    production_source = inspect.getsource(production)
    assert "canonical_v13" not in legacy_main
    for forbidden in (
        "app.main",
        "app.db",
        "app.models",
        "Base.metadata",
        'os.environ["DATABASE_URL"]',
        'environment.get("DATABASE_URL")',
    ):
        assert forbidden not in production_source
    assert "install_canonical_genesis" not in production_source
    assert "activate_research_bundle" not in production_source

    runbook = (
        REPOSITORY_ROOT
        / "docs"
        / "runbooks"
        / "strategy_platform_v13_canonical_genesis.md"
    ).read_text()
    for required in (
        "BLOCKED_NON_EMPTY_CANONICAL_DATABASE",
        "render_postgresql_acl_sql(mapping)",
        "render_postgresql_owner_sql(mapping)",
        "require_zero_business_rows=True",
        production.READER_DATABASE_URL_ENV,
        production.CONTROL_DATABASE_URL_ENV,
        "不会安装 genesis",
        "ROLLBACK",
    ):
        assert required in runbook


def test_frontend_and_backend_share_one_canonical_prefix_and_split_dev_proxy() -> None:
    client_source = (
        REPOSITORY_ROOT / "frontend" / "src" / "api" / "canonicalV13Client.ts"
    ).read_text()
    vite_source = (REPOSITORY_ROOT / "frontend" / "vite.config.ts").read_text()
    assert f'CANONICAL_V13_API_ROOT = "{API_PREFIX}"' in client_source
    assert f'"{API_PREFIX}"' in vite_source
    assert vite_source.index(f'"{API_PREFIX}"') < vite_source.index('"/api"')
    assert "127.0.0.1:8011" in vite_source
    assert "127.0.0.1:8000" in vite_source


def test_production_composition_requires_two_roles_on_one_postgresql_database(
    monkeypatch,
) -> None:
    with pytest.raises(production.CanonicalProductionConfigurationBlocked) as missing:
        production.create_app({"DATABASE_URL": "sqlite:///legacy.db"})
    assert "BLOCKED_CANONICAL_DATABASE_URL_UNSET" in str(missing.value)

    base = {
        production.READER_DATABASE_URL_ENV:
            "postgresql+psycopg://freqtrade_ai_v13_api_login@127.0.0.1/canonical_v13",
        production.CONTROL_DATABASE_URL_ENV:
            "postgresql+psycopg://freqtrade_ai_v13_control_login@127.0.0.1/canonical_v13",
        **{
            environment_name: (
                "postgresql+psycopg://"
                f"{research_service_principal(production.local_role_mapping(), logical_role)}"
                "@127.0.0.1/canonical_v13"
            )
            for logical_role, environment_name in (
                production.RESEARCH_PERSISTENCE_ENV_BY_CAPABILITY.items()
            )
        },
    }
    without_validation = dict(base)
    without_validation.pop(
        production.RESEARCH_PERSISTENCE_ENV_BY_CAPABILITY[
            "canonical_validation_writer"
        ]
    )
    with pytest.raises(production.CanonicalProductionConfigurationBlocked) as missing_research:
        production.create_app(without_validation)
    assert "BLOCKED_RESEARCH_DATABASE_URL_UNSET" in str(missing_research.value)

    with pytest.raises(production.CanonicalProductionConfigurationBlocked) as invalid:
        production.create_app(
            {
                **base,
                production.READER_DATABASE_URL_ENV: "sqlite:///canonical.db",
            }
        )
    assert "BLOCKED_CANONICAL_DATABASE_URL_INVALID" in str(invalid.value)

    with pytest.raises(production.CanonicalProductionConfigurationBlocked) as split:
        production.create_app(
            {
                **base,
                production.CONTROL_DATABASE_URL_ENV:
                    "postgresql+psycopg://freqtrade_ai_v13_control_login@127.0.0.1/other_database",
            }
        )
    assert "BLOCKED_CANONICAL_DATABASE_SPLIT" in str(split.value)

    engines = [create_engine("sqlite+pysqlite:///:memory:") for _ in range(6)]
    calls = []

    def fake_create_engine(url, **kwargs):
        calls.append((url, kwargs))
        return engines[len(calls) - 1]

    monkeypatch.setattr(production, "create_engine", fake_create_engine)
    app = production.create_app(base)
    try:
        assert len(calls) == 6
        assert all(call[1] == {"pool_pre_ping": True} for call in calls)
        assert {route.path for route in app.routes if route.path.startswith(API_PREFIX)}
        assert app.state.canonical_reader_engine is engines[0]
        assert app.state.canonical_control_engine is engines[1]
        assert set(app.state.canonical_research_engines) == set(
            production.RESEARCH_PERSISTENCE_ENV_BY_CAPABILITY
        )
        routes = {route.path for route in app.routes}
        assert "/healthz" in routes
        assert "/readyz" in routes
    finally:
        for engine in engines:
            engine.dispose()
