from __future__ import annotations

from datetime import datetime, timezone
import os

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


def _result(index: int) -> ResearchResult:
    cases = (
        (
            "derivatives-crowding-15m",
            "BOUNDED_NEGATIVE",
            "TRAIN_NO_EDGE_STOP_WITHOUT_VALIDATION_PNL",
            "research/results/derivatives-crowding/validation.json",
        ),
        (
            "public-acquisition-pre-value",
            "DATA_ACQUISITION_BLOCKED_PRE_VALUE",
            "BLOCKED_LIVE_CONTENT_LENGTH_NONE",
            "research/results/public-acquisition/blocked.json",
        ),
        (
            "multi-asset-frozen-finalists",
            "FINALISTS_FROZEN",
            "AWAITING_FORMAL_VALIDATION",
            "research/results/multi-asset/finalists.json",
        ),
    )
    run_id, status, reason, artifact_path = cases[index]
    return ResearchResult(
        run_id=run_id,
        name=run_id.replace("-", " "),
        hypothesis=f"Representative historical import case {index + 1}.",
        universe=["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"],
        timeframe="15m",
        status=status,
        reason_code=reason,
        dataset_digest=f"{index + 1:x}" * 64,
        artifact_path=artifact_path,
        result_digest=f"{index + 4:x}" * 64,
        train_validation_holdout_summary={
            "train": "OBSERVED",
            "validation": "NOT_EVALUATED",
            "holdout": "SEALED_UNREAD",
        },
        metrics_summary={"fixture_case": index + 1},
        created_at=datetime(2026, 8, 26, 17, 40 + index, tzinfo=timezone.utc),
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
