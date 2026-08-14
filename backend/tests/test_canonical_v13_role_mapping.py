from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from app.canonical_v13.bootstrap import (
    LOCAL_DATABASE_NAME,
    LOCAL_ROLE_PREFIX,
    local_role_mapping,
)
from app.canonical_v13.genesis import (
    assert_postgresql_acl_sql,
    render_postgresql_acl_sql,
    render_postgresql_genesis_ddl,
    render_postgresql_owner_sql,
)
from app.canonical_v13.role_mapping import (
    LOGICAL_ROLE_IDENTITIES,
    CanonicalRoleMapping,
    CanonicalRoleMappingBlocked,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_local_mapping_is_total_unique_and_digest_stable() -> None:
    mapping = local_role_mapping()
    assert tuple(mapping.roles) == LOGICAL_ROLE_IDENTITIES
    assert len(set(mapping.roles.values())) == len(LOGICAL_ROLE_IDENTITIES)
    assert all(role.startswith(LOCAL_ROLE_PREFIX) for role in mapping.roles.values())
    assert len(mapping.mapping_digest) == 64
    assert mapping == CanonicalRoleMapping.from_prefix(LOCAL_ROLE_PREFIX)


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
            str(REPOSITORY_ROOT / "backend/scripts/canonical_v13_bootstrap.py"),
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
    assert payload["capability_role_count"] == 15
    assert payload["acl_statement_count"] == 914
    assert "DATABASE_URL" not in completed.stdout
