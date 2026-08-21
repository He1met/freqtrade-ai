from __future__ import annotations

from pathlib import Path

from app.canonical_v13.phase9_schema_upgrade import (
    PHASE9_DATABASE_CONNECT_DELTA,
    PHASE9_EXTENSION_TABLE_NAMES,
    PHASE9_SURVIVING_TABLE_GRANT_DELTA,
    PREVIOUS_ACL_CONTRACT_DIGEST,
    render_phase9_acl_rollback_sql,
)
from app.canonical_v13.role_mapping import CanonicalRoleMapping


def test_phase9_acl_rollback_is_the_frozen_predecessor_delta_only() -> None:
    mapping = CanonicalRoleMapping.from_prefix("phase9_test_")
    sql = render_phase9_acl_rollback_sql(
        mapping, database_name="phase9_predecessor_test"
    )
    statements = tuple(item for item in sql.split(";\n") if item)

    assert len(statements) == len(PHASE9_SURVIVING_TABLE_GRANT_DELTA) + len(
        PHASE9_DATABASE_CONNECT_DELTA
    )
    assert PREVIOUS_ACL_CONTRACT_DIGEST == (
        "af302c492883a901798b7c86f4f0c9d457bd942037498571e0f8b962e1948263"
    )
    assert all("REVOKE" in statement for statement in statements)
    assert not any("GRANT" in statement for statement in statements)
    assert not any(
        table_name in sql for table_name in PHASE9_EXTENSION_TABLE_NAMES
    )
    assert (
        "REVOKE SELECT ON TABLE strategy_platform_v13.deployment_approvals "
        "FROM phase9_test_control_writer"
    ) in statements
    assert (
        'REVOKE CONNECT ON DATABASE "phase9_predecessor_test" '
        "FROM phase9_test_runtime_reader"
    ) in statements
    assert "phase9_test_api_reader" not in sql
    assert "phase9_test_validation_writer" not in sql


def test_phase9_runbook_matches_current_probe_and_cleanup_contract() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    runbook = (
        repository_root
        / "docs/runbooks/strategy_platform_v13_phase9_no_order_readiness.md"
    ).read_text(encoding="utf-8")

    assert "13 个是 writer LOGIN" in runbook
    assert "canonical_deployment_writer` transaction" in runbook
    assert "canonical_approval_writer` transaction" in runbook
    assert "cleanup-phase9-provisioning" in runbook
    assert "repeat_noop=true" in runbook
    assert "该表只属于" not in runbook
    assert "PostgreSQL ACL 下该命令不可达" not in runbook
