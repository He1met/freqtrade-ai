from __future__ import annotations

from sqlalchemy import create_engine

from app.canonical_v13.authority_upgrade import (
    PREVIOUS_CANONICAL_MANIFEST_DIGEST,
    RESEARCH_AUTHORITY_TABLES,
    SPLIT_RESEARCH_WRITER_IDENTITIES,
    render_authority_rollback_acl_sql,
    render_authority_upgrade_acl_sql,
    render_authority_upgrade_plan,
    verify_authority_upgrade_state,
)
from app.canonical_v13.bootstrap import (
    local_legacy_research_writer_role,
    local_role_mapping,
)
from app.canonical_v13.manifest import (
    CANONICAL_MANIFEST_DIGEST,
    CANONICAL_TABLE_NAMES,
)


def test_offline_upgrade_and_rollback_plans_are_exact_and_non_destructive() -> None:
    mapping = local_role_mapping()
    legacy = local_legacy_research_writer_role()
    plan = render_authority_upgrade_plan(
        role_mapping=mapping,
        legacy_research_writer_role=legacy,
    )
    upgrade = render_authority_upgrade_acl_sql(
        role_mapping=mapping,
        legacy_research_writer_role=legacy,
    )
    rollback = render_authority_rollback_acl_sql(
        role_mapping=mapping,
        legacy_research_writer_role=legacy,
    )

    assert plan["previous_manifest_digest"] == PREVIOUS_CANONICAL_MANIFEST_DIGEST
    assert plan["current_manifest_digest"] == CANONICAL_MANIFEST_DIGEST
    assert plan["table_count"] == len(CANONICAL_TABLE_NAMES) == 57
    assert tuple(plan["research_tables"]) == RESEARCH_AUTHORITY_TABLES
    assert plan["destructive_table_operations"] == []
    assert "DROP " not in (upgrade + rollback).upper()
    assert "DELETE " not in (upgrade + rollback).upper()
    assert "TRUNCATE " not in (upgrade + rollback).upper()

    for logical_role in SPLIT_RESEARCH_WRITER_IDENTITIES:
        physical = mapping.physical(logical_role)
        assert f"TO {physical}" in upgrade
        assert f"FROM {physical}" in rollback
    assert (
        "REVOKE ALL PRIVILEGES ON TABLE "
        "strategy_platform_v13.target_scores "
        f"FROM {legacy}"
    ) in upgrade
    assert (
        f"GRANT SELECT, INSERT ON TABLE strategy_platform_v13.target_scores TO {legacy}"
    ) in rollback


def test_upgrade_verifier_requires_postgresql() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        with engine.connect() as connection:
            result = verify_authority_upgrade_state(
                connection,
                role_mapping=local_role_mapping(),
                legacy_research_writer_role=local_legacy_research_writer_role(),
            )
    finally:
        engine.dispose()
    assert result.accepted is False
    assert result.state == "BLOCKED"
    assert result.problems == ("BLOCKED_POSTGRESQL_REQUIRED",)
