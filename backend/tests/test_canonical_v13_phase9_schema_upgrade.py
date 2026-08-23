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
from app.canonical_v13.deployment_rollover_upgrade import (
    DEPLOYMENT_ROLLOVER_COLUMNS,
    RUNTIME_IDENTITY_ACTIVE_INDEX,
    deployment_rollover_trigger_statements,
)
from app.canonical_v13.genesis import render_postgresql_genesis_ddl
from app.canonical_v13.optimization_observability_upgrade import (
    OPTIMIZATION_OBSERVABILITY_COLUMNS,
    optimization_observability_trigger_statements,
)


def test_deployment_rollover_guard_is_exact_and_fail_closed() -> None:
    sql = ";\n".join(deployment_rollover_trigger_statements())
    assert DEPLOYMENT_ROLLOVER_COLUMNS == (
        "disable_reason",
        "disable_receipt_digest",
        "disable_request_digest",
        "disabled_at",
        "disabled_by",
        "superseded_by_qualification_decision_id",
    )
    assert "OLD.status = 'DISABLED'" in sql
    assert "OLD.status <> 'ACTIVE'" in sql
    assert "disable evidence is incomplete" in sql
    assert "deployment lineage is immutable" in sql
    assert "BEFORE UPDATE" in sql


def test_runtime_identity_rollover_is_unique_only_for_nonstopped_rows() -> None:
    ddl = render_postgresql_genesis_ddl()
    assert RUNTIME_IDENTITY_ACTIVE_INDEX in ddl
    assert "CREATE UNIQUE INDEX" in ddl
    assert "runtime_instances (runtime_identity)" in ddl
    assert "WHERE status <> 'STOPPED'" in ddl
    assert "UNIQUE (runtime_identity)" not in ddl


def test_optimization_observability_guard_is_exact_and_fail_closed() -> None:
    sql = ";\n".join(optimization_observability_trigger_statements())
    assert OPTIMIZATION_OBSERVABILITY_COLUMNS == (
        "result_count",
        "result_digest",
        "submitted_strategy_count",
        "terminal_reason_codes",
        "trial_count",
    )
    assert "BEFORE INSERT OR UPDATE" in sql
    assert "terminal rows require lifecycle completion" in sql
    assert "canonical optimization terminal evidence is immutable" in sql
    assert "canonical optimization submission count drift" in sql
    assert "canonical optimization terminal summary does not match trials" in sql
    assert "jsonb_array_length(NEW.terminal_reason_codes::jsonb)" in sql
    assert "guard_optimization_runs_terminal_observability" in render_postgresql_genesis_ddl()


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
    assert not any(table_name in sql for table_name in PHASE9_EXTENSION_TABLE_NAMES)
    assert (
        "REVOKE SELECT ON TABLE strategy_platform_v13.deployment_approvals "
        "FROM phase9_test_control_writer"
    ) in statements
    assert (
        "REVOKE SELECT ON TABLE strategy_platform_v13.qualification_decisions "
        "FROM phase9_test_runtime_reader"
    ) in statements
    assert (
        "REVOKE SELECT ON TABLE strategy_platform_v13.research_targets "
        "FROM phase9_test_risk_writer"
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
    assert "runtime-reader-acl-apply" in runbook
    assert "deployment-rollover-apply" in runbook
    assert (
        runbook.index("canonical_v13_runtime_image.py schema-apply")
        < runbook.index("runtime-reader-acl-apply")
        < runbook.index("deployment-rollover-apply")
    )
    assert "必须严格反向执行" in runbook
    assert "/phase9/deployments/{old_id}/disable" in runbook
    assert "禁止直接 SQL 改状态" in runbook
    assert "repeat_noop=true" in runbook
    assert "该表只属于" not in runbook
    assert "PostgreSQL ACL 下该命令不可达" not in runbook
