from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from app.canonical_v13.bootstrap import (
    LOCAL_DATABASE_NAME,
    LOCAL_RESEARCH_SERVICE_PRINCIPALS,
    LOCAL_ROLE_PREFIX,
    local_role_mapping,
)
from app.canonical_v13.genesis import (
    assert_postgresql_acl_sql,
    render_postgresql_acl_sql,
    render_postgresql_genesis_ddl,
    render_postgresql_owner_sql,
    postgresql_owner_table_grant_statements,
)
from app.canonical_v13.role_mapping import (
    LOGICAL_ROLE_IDENTITIES,
    CanonicalRoleMapping,
    CanonicalRoleMappingBlocked,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP_SCRIPT = REPOSITORY_ROOT / "backend/scripts/canonical_v13_bootstrap.py"


def _load_bootstrap_script():
    spec = importlib.util.spec_from_file_location(
        "canonical_v13_bootstrap_restore_test", BOOTSTRAP_SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_local_mapping_is_total_unique_and_digest_stable() -> None:
    mapping = local_role_mapping()
    assert tuple(mapping.roles) == LOGICAL_ROLE_IDENTITIES
    assert len(set(mapping.roles.values())) == len(LOGICAL_ROLE_IDENTITIES)
    assert all(role.startswith(LOCAL_ROLE_PREFIX) for role in mapping.roles.values())
    assert len(mapping.mapping_digest) == 64
    assert mapping == CanonicalRoleMapping.from_prefix(LOCAL_ROLE_PREFIX)
    assert tuple(LOCAL_RESEARCH_SERVICE_PRINCIPALS.values()) == (
        "canonical_validation_writer",
        "canonical_scoring_writer",
        "canonical_qualification_writer",
        "canonical_optimization_writer",
    )


@pytest.mark.parametrize(
    "roles,code",
    [
        ({}, "BLOCKED_ROLE_MAPPING_KEYS"),
        (
            {role: "same_role" for role in LOGICAL_ROLE_IDENTITIES},
            "BLOCKED_ROLE_MAPPING_DUPLICATE",
        ),
        (
            {
                role: ("invalid-role" if index == 0 else f"valid_{index}")
                for index, role in enumerate(LOGICAL_ROLE_IDENTITIES)
            },
            "BLOCKED_ROLE_MAPPING_IDENTIFIER",
        ),
    ],
)
def test_mapping_rejects_incomplete_duplicate_and_invalid_roles(
    roles: dict[str, str], code: str
) -> None:
    with pytest.raises(CanonicalRoleMappingBlocked, match=code):
        CanonicalRoleMapping.exact(roles)


def test_mapped_owner_and_acl_are_exact_and_contain_no_logical_role_targets() -> None:
    mapping = local_role_mapping()
    owner = render_postgresql_owner_sql(mapping)
    acl = render_postgresql_acl_sql(mapping)
    ddl = render_postgresql_genesis_ddl(mapping)
    assert_postgresql_acl_sql(acl, mapping)
    owner_grants = postgresql_owner_table_grant_statements(mapping)
    assert len(owner_grants) == 48
    assert (
        "GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER "
        "ON TABLE "
        "strategy_platform_v13.configuration_profiles TO "
        f"{mapping.physical('canonical_schema_owner')}"
    ) in owner_grants
    assert f"OWNER TO {mapping.physical('canonical_schema_owner')}" in owner
    for statement in owner.rstrip(";\n").split(";\n"):
        assert statement in ddl
    for logical, physical in mapping.roles.items():
        assert physical in acl or physical in owner
        assert f" TO {logical}" not in acl
        assert f" TO {logical}" not in owner


def test_bootstrap_render_cli_is_offline_and_never_requires_a_database_url() -> None:
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(REPOSITORY_ROOT / "backend"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "FREQTRADE_AI_DISABLE_ENV_FILE": "1",
        "FREQTRADE_AI_TEST_DISABLE_ENV_FILE": "1",
    }
    completed = subprocess.run(
        [
            sys.executable,
            str(BOOTSTRAP_SCRIPT),
            "render",
        ],
        cwd=REPOSITORY_ROOT / "backend",
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    payload = json.loads(completed.stdout)
    assert payload["status"] == "READY"
    assert payload["database_name"] == LOCAL_DATABASE_NAME
    assert payload["role_prefix"] == LOCAL_ROLE_PREFIX
    assert payload["capability_role_count"] == 18
    assert payload["acl_statement_count"] == 1257
    assert "DATABASE_URL" not in completed.stdout


def test_owner_table_acl_plan_is_offline_exact_and_non_destructive() -> None:
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(REPOSITORY_ROOT / "backend"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "FREQTRADE_AI_DISABLE_ENV_FILE": "1",
        "FREQTRADE_AI_TEST_DISABLE_ENV_FILE": "1",
    }
    completed = subprocess.run(
        [sys.executable, str(BOOTSTRAP_SCRIPT), "owner-table-acl-plan"],
        cwd=REPOSITORY_ROOT / "backend",
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    payload = json.loads(completed.stdout)
    assert payload["status"] == "READY"
    assert payload["contract"] == "canonical-v13-owner-table-acl-v1"
    assert payload["table_statement_count"] == 48
    assert payload["target_privilege_fact_count"] == 336
    assert payload["legacy_privilege_fact_count"] == 2
    assert len(payload["owner_acl_digest"]) == 64
    assert payload["destructive_table_operations"] == []
    assert payload["requires_zero_research_rows"] is True


def test_restore_verifier_requires_an_explicit_strict_restore_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap = _load_bootstrap_script()
    monkeypatch.delenv(bootstrap.RESTORE_DATABASE_NAME_ENV, raising=False)
    with pytest.raises(bootstrap.BootstrapBlocked, match="RESTORE_DATABASE_NAME_UNSET"):
        bootstrap._restore_database_name()

    for invalid_name in (
        LOCAL_DATABASE_NAME,
        "freqtrade_ai_v13_restore",
        "freqtrade_ai_v13_restore_Upper",
        "freqtrade_ai_v13_restore_bad-name",
        "another_database_restore_20260815",
    ):
        monkeypatch.setenv(bootstrap.RESTORE_DATABASE_NAME_ENV, invalid_name)
        with pytest.raises(
            bootstrap.BootstrapBlocked, match="RESTORE_DATABASE_NAME_INVALID"
        ):
            bootstrap._restore_database_name()

    restore_database = "freqtrade_ai_v13_restore_20260815_d465e031"
    monkeypatch.setenv(bootstrap.RESTORE_DATABASE_NAME_ENV, restore_database)
    assert bootstrap._restore_database_name() == restore_database


def test_restore_database_url_is_exact_and_does_not_relax_production_verifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap = _load_bootstrap_script()
    restore_database = "freqtrade_ai_v13_restore_20260815_d465e031"
    restore_url = f"postgresql+psycopg:///{restore_database}"
    monkeypatch.setenv(bootstrap.DATABASE_URL_ENV, restore_url)

    with pytest.raises(
        bootstrap.BootstrapBlocked, match="CANONICAL_DATABASE_NAME_MISMATCH"
    ):
        bootstrap._database_url()
    assert (
        bootstrap._database_url(expected_database_name=restore_database)
        == restore_url
    )

    monkeypatch.setenv(
        bootstrap.DATABASE_URL_ENV,
        "postgresql+psycopg:///freqtrade_ai_v13_restore_another",
    )
    with pytest.raises(
        bootstrap.BootstrapBlocked, match="CANONICAL_DATABASE_NAME_MISMATCH"
    ):
        bootstrap._database_url(expected_database_name=restore_database)
