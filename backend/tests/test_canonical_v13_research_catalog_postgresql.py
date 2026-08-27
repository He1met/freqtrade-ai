from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select, text

from app.canonical_v13.genesis import verify_canonical_genesis
from app.canonical_v13.models import (
    QUALIFICATION_DECISIONS_TABLE,
    RESEARCH_RUN_CATALOG_TABLE,
    STRATEGIES_TABLE,
    VALIDATION_PLANS_TABLE,
)
from app.canonical_v13.research_catalog import (
    ResearchResult,
    apply_research_import,
    list_research_results,
    load_research_result,
    plan_research_import,
)
from app.canonical_v13.research_catalog_upgrade import (
    PREVIOUS_RESEARCH_CATALOG_MANIFEST_DIGEST,
    apply_research_catalog_upgrade,
    verify_research_catalog_upgrade,
)
from app.canonical_v13.role_mapping import CanonicalRoleMapping


DATABASE_URL = os.environ.get("CANONICAL_V13_POSTGRES_URL")
ROLE_PREFIX = os.environ.get("CANONICAL_V13_ROLE_PREFIX", "freqtrade_ai_v13_ci_")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="CANONICAL_V13_POSTGRES_URL is required for the isolated contract",
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "research_catalog"
FIXTURE_CASES = (
    "derivatives_crowding",
    "acquisition_blocked",
    "finalists_frozen",
)


def _result(index: int) -> ResearchResult:
    return load_research_result(
        FIXTURE_ROOT / FIXTURE_CASES[index] / "research_result.json"
    )


def test_predecessor_upgrade_and_three_idempotent_imports_leave_core_rows_unchanged() -> None:
    assert DATABASE_URL is not None
    mapping = CanonicalRoleMapping.from_prefix(ROLE_PREFIX)
    engine = create_engine(DATABASE_URL)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("DROP TABLE strategy_platform_v13.research_run_catalog")
            )
            connection.execute(
                text(
                    "UPDATE strategy_platform_v13.schema_metadata "
                    "SET manifest_digest=:digest "
                    "WHERE metadata_key='canonical-v13-genesis'"
                ),
                {"digest": PREVIOUS_RESEARCH_CATALOG_MANIFEST_DIGEST},
            )
            predecessor = verify_research_catalog_upgrade(
                connection, role_mapping=mapping
            )
            assert predecessor.status == "PREVIOUS_READY"
            upgraded = apply_research_catalog_upgrade(
                connection, role_mapping=mapping
            )
            assert upgraded.status == "UPGRADED"

            core_before = tuple(
                int(
                    connection.execute(
                        select(func.count()).select_from(table)
                    ).scalar_one()
                )
                for table in (
                    STRATEGIES_TABLE,
                    VALIDATION_PLANS_TABLE,
                    QUALIFICATION_DECISIONS_TABLE,
                )
            )
            connection.exec_driver_sql(
                f"SET LOCAL ROLE {mapping.physical('canonical_control_writer')}"
            )
            for index in range(3):
                result = _result(index)
                assert plan_research_import(connection, result).action == "INSERT"
                assert apply_research_import(connection, result).action == "INSERTED"
                assert plan_research_import(connection, result).action == "NO_OP"
                assert apply_research_import(connection, result).action == "NO_OP"
            connection.exec_driver_sql("RESET ROLE")

            assert tuple(
                int(
                    connection.execute(
                        select(func.count()).select_from(table)
                    ).scalar_one()
                )
                for table in (
                    STRATEGIES_TABLE,
                    VALIDATION_PLANS_TABLE,
                    QUALIFICATION_DECISIONS_TABLE,
                )
            ) == core_before
            assert connection.execute(
                select(func.count()).select_from(RESEARCH_RUN_CATALOG_TABLE)
            ).scalar_one() == 3

            connection.exec_driver_sql(
                f"SET LOCAL ROLE {mapping.physical('canonical_api_reader')}"
            )
            assert len(list_research_results(connection)) == 3
            connection.exec_driver_sql("RESET ROLE")
            assert verify_canonical_genesis(connection).accepted
            assert verify_research_catalog_upgrade(
                connection, role_mapping=mapping
            ).status == "ACCEPTED"
    finally:
        engine.dispose()
