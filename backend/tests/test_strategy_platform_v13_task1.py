from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib

import pytest
from sqlalchemy import CheckConstraint, LargeBinary, Table, create_engine, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from app.db import strategy_platform_v13_task1 as task1
from app.db.strategy_platform_v13_task1 import (
    MarketFileEvidence,
    StrategyPlatformTask1Blocked,
    canonical_digest,
    canonical_json,
    install_strategy_platform_v13_task1_schema,
    validate_market_inventory,
)
from app.models import Base


EXPECTED_EXTENSION_TABLES = {
    "adapter_definitions",
    "strategy_source_definitions",
    "strategy_source_definition_versions",
    "trigger_source_definitions",
    "trigger_source_definition_versions",
    "timeframe_definitions",
    "timeframe_definition_versions",
    "research_target_config_sets",
    "research_target_configs",
    "strategy_family_definitions",
    "strategy_family_definition_versions",
    "provider_model_config_versions",
    "generation_profile_versions",
    "generation_profile_families",
    "scoring_profile_versions",
    "scoring_rules",
    "diversity_profile_versions",
    "diversity_rules",
    "worker_execution_profile_versions",
    "scheduler_profile_versions",
    "market_data_policy_versions",
    "evidence_freshness_profile_versions",
    "evidence_freshness_rules",
    "monitoring_profile_versions",
    "promotion_profile_versions",
    "promotion_rules",
    "risk_profile_versions",
    "risk_rules",
    "capacity_profile_versions",
    "runtime_profile_versions",
    "deployment_profile_versions",
    "market_data_profile_versions",
    "optimization_profile_versions",
    "ui_presentation_profile_versions",
    "research_profile_versions",
    "strategy_submissions",
    "strategy_runtime_instances",
    "strategy_position_ledger_entries",
    "strategy_position_reconciliation_items",
    "market_data_file_records",
    "market_data_update_jobs",
    "market_data_update_items",
    "optimization_runs",
    "optimization_trials",
    "strategy_platform_migration_runs",
    "strategy_platform_migration_table_snapshots",
    "strategy_platform_migration_entity_mappings",
    "strategy_platform_migration_conflicts",
}


def _sqlite_engine():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


def _checks(table_name: str) -> dict[str, str]:
    table = Base.metadata.tables[table_name]
    return {
        constraint.name or "": str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }


def _classification_windows(
    file_digest: str, *, pair: str, timeframe: str
) -> dict[str, dict[str, object]]:
    interval_seconds = 300 if timeframe == "5m" else 900

    def evidence(
        actual: str, first: float, last: float, start: str, end: str
    ) -> dict[str, object]:
        parsed_start = datetime.fromisoformat(start.replace("Z", "+00:00"))
        parsed_end = datetime.fromisoformat(end.replace("Z", "+00:00"))
        return {
            "market_data_digest": file_digest,
            "actual_regime": actual,
            "first_close": first,
            "last_close": last,
            "close_return": last / first - 1.0,
            "row_count": int((parsed_end - parsed_start).total_seconds())
            // interval_seconds,
            "start_at": start,
            "end_at": end,
        }

    primary_start = (
        "2023-08-01T00:00:00Z" if pair.startswith("SOL/") else "2023-07-01T00:00:00Z"
    )
    range_end = (
        "2024-05-01T00:00:00Z" if pair.startswith("SOL/") else "2024-06-29T00:00:00Z"
    )
    return {
        "primary_bear": evidence("bear", 100.0, 90.0, primary_start, "2023-10-01T00:00:00Z"),
        "wf_bull": evidence("bull", 100.0, 110.0, "2023-10-01T00:00:00Z", "2024-03-01T00:00:00Z"),
        "wf_range": evidence("range", 100.0, 101.0, "2024-03-01T00:00:00Z", range_end),
        # OOS is intentionally classified from evidence, not forced to a regime.
        "oos": evidence("bull", 100.0, 105.0, "2025-01-01T00:00:00Z", "2025-10-01T00:00:00Z"),
        "wf_bear": evidence("bear", 100.0, 80.0, "2025-10-01T00:00:00Z", "2026-02-01T00:00:00Z"),
    }


def _market_inventory() -> tuple[MarketFileEvidence, ...]:
    first_open = datetime(2023, 7, 1, tzinfo=timezone.utc)
    last_close = datetime(2026, 2, 1, tzinfo=timezone.utc)
    observed_at = datetime(2026, 8, 13, tzinfo=timezone.utc)
    records: list[MarketFileEvidence] = []
    for index, pair in enumerate(
        ("BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"), start=1
    ):
        for timeframe in ("5m", "15m"):
            digest = hashlib.sha256(f"{pair}:{timeframe}".encode()).hexdigest()
            interval_seconds = 300 if timeframe == "5m" else 900
            records.append(
                MarketFileEvidence(
                    exchange="okx",
                    market_type="futures",
                    pair=pair,
                    instrument_id=f"{pair.split('/')[0]}-USDT-SWAP",
                    timeframe=timeframe,
                    data_kind="futures",
                    absolute_path=f"/design-lab/{index}-{timeframe}.feather",
                    relative_path=f"futures/{index}-{timeframe}.feather",
                    file_format="feather",
                    size_bytes=4096,
                    sha256=digest,
                    row_count=100,
                    first_open_at=first_open,
                    last_open_at=last_close - timedelta(seconds=interval_seconds),
                    last_close_at=last_close,
                    expected_interval_seconds=interval_seconds,
                    gap_count=0,
                    duplicate_count=0,
                    null_count=0,
                    freshness_status="PASSED",
                    observed_at=observed_at,
                    source_receipt_digest=hashlib.sha256(
                        f"source:{pair}:{timeframe}".encode()
                    ).hexdigest(),
                    classification_windows=_classification_windows(
                        digest, pair=pair, timeframe=timeframe
                    ),
                )
            )
    return tuple(records)


def test_task1_extension_table_contract_is_complete_and_restrictive() -> None:
    declared = task1.STRATEGY_PLATFORM_V13_EXTENSION_TABLES

    assert len(declared) == len(set(declared)) == 48
    assert set(declared) == EXPECTED_EXTENSION_TABLES
    assert EXPECTED_EXTENSION_TABLES.issubset(Base.metadata.tables)
    for table_name in declared:
        table = Base.metadata.tables[table_name]
        assert all(foreign_key.ondelete == "RESTRICT" for foreign_key in table.foreign_keys)

    assert {
        "strategy_target_id",
        "configuration_bundle_snapshot_id",
    }.issubset(Base.metadata.tables["strategy_deployments"].c.keys())
    assert {
        "deployment_id",
        "signal_evaluation_id",
        "runtime_instance_row_id",
    }.issubset(Base.metadata.tables["trade_intents"].c.keys())


def test_extension_metadata_has_no_secret_or_executable_storage_contract() -> None:
    allowed_safety_or_reference_columns = {
        "contains_secret_material",
        "contains_executable_payload",
        "secret_material_present",
        "executable_payload_present",
        "executable_payload_allowed",
        "credential_reference_kind",
        "credential_reference_name",
    }
    forbidden_storage_names = {
        "secret",
        "password",
        "api_key",
        "access_key",
        "private_key",
        "credential_value",
        "executable",
        "command",
        "source_code",
        "code_payload",
    }

    for table_name in task1.STRATEGY_PLATFORM_V13_EXTENSION_TABLES:
        table = Base.metadata.tables[table_name]
        assert not any(isinstance(column.type, LargeBinary) for column in table.c)
        for column in table.c:
            if column.name in allowed_safety_or_reference_columns:
                continue
            assert column.name not in forbidden_storage_names

    safety_checks = {
        **_checks("adapter_definitions"),
        **_checks("strategy_source_definition_versions"),
        **_checks("trigger_source_definition_versions"),
        **_checks("provider_model_config_versions"),
        **_checks("strategy_submissions"),
        **_checks("runtime_profile_versions"),
        **_checks("optimization_profile_versions"),
        **_checks("ui_presentation_profile_versions"),
    }
    check_sql = " ".join(safety_checks.values())
    for fragment in (
        "contains_secret_material = FALSE",
        "contains_executable_payload = FALSE",
        "secret_material_present = FALSE",
        "executable_payload_present = FALSE",
        "executable_payload_allowed = FALSE",
        "payload_redacted = TRUE",
        "execution_requested = FALSE",
    ):
        assert fragment in check_sql


def test_demo_only_fail_closed_constraints_cover_config_and_runtime_facts() -> None:
    checks = " ".join(
        sql
        for table_name in (
            "execution_target_definition_versions",
            "risk_profile_versions",
            "runtime_profile_versions",
            "deployment_profile_versions",
            "strategy_runtime_instances",
        )
        for sql in _checks(table_name).values()
    )
    assert checks.count("allow_real_funds = FALSE") >= 5
    assert checks.count("demo_only = TRUE") >= 4
    assert checks.count("single_writer_required = TRUE") >= 4
    assert "fail_closed = TRUE" in checks

    engine = _sqlite_engine()
    runtime_table = Base.metadata.tables["strategy_runtime_instances"]
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                runtime_table.insert().values(
                    deployment_id=1,
                    strategy_target_id=1,
                    configuration_bundle_snapshot_id=1,
                    runtime_adapter_key="runtime-metadata-v1",
                    runtime_instance_id="must-not-be-live",
                    config_digest="a" * 64,
                    status="UNKNOWN",
                    demo_only=True,
                    allow_real_funds=True,
                    single_writer_required=True,
                )
            )


def test_pair_timeframe_and_active_slot_are_data_driven_not_fixed_enums() -> None:
    candidate_checks = " ".join(
        _checks("strategy_research_candidates").values()
    )
    active_slot_check = _checks("strategy_deployments")[
        "strategy_deployments_active_slot_check"
    ]
    for fixed_value in (
        "BTC/USDT:USDT",
        "ETH/USDT:USDT",
        "SOL/USDT:USDT",
        "5m",
        "15m",
    ):
        assert fixed_value not in candidate_checks
    assert "active_slot > 0" in active_slot_check
    assert "active_slot <= 9" not in active_slot_check
    assert "BETWEEN 1 AND 9" not in active_slot_check

    engine = _sqlite_engine()
    batches = Base.metadata.tables["strategy_research_batches"]
    candidates = Base.metadata.tables["strategy_research_candidates"]
    deployments = Base.metadata.tables["strategy_deployments"]
    with engine.begin() as connection:
        batch_id = connection.execute(
            batches.insert()
            .values(
                run_id="dynamic-target-contract",
                source_type="test",
                repository_commit="a" * 40,
                report_schema_version="v1",
                report_path="reports/test.json",
                report_digest="b" * 64,
                status="GENERATED",
                requested_count=1,
                generated_count=1,
                persisted_count=1,
                qualified_count=0,
                rejected_count=1,
                safety_snapshot={},
                selection_policy={},
                window_evidence=[],
            )
            .returning(batches.c.id)
        ).scalar_one()
        connection.execute(
            candidates.insert().values(
                batch_id=batch_id,
                candidate_name="dynamic-target",
                source_path="strategies/dynamic.py",
                code_digest="c" * 64,
                pair="DOGE/USDT:USDT",
                timeframe="1h",
                unit_slot=1,
                similarity_evidence={},
                correlation_evidence={},
                status="REJECTED",
                loadable=True,
                static_check="PASSED",
                lookahead_status="UNKNOWN",
                validation_passed=False,
                deployable_candidate=False,
                rejection_reasons=[],
                evidence_snapshot={},
            )
        )
        connection.execute(
            deployments.insert().values(
                execution_target_id="OKX_DEMO",
                candidate_approval_id=101,
                strategy_id=201,
                strategy_version_id=301,
                candidate_digest="d" * 64,
                promotion_policy_version="v1",
                deployment_policy_digest="e" * 64,
                real_orders=False,
                instrument_id="DOGE-USDT-SWAP",
                timeframe="1h",
                status="ACTIVE",
                active_slot=27,
                evidence_snapshot={},
            )
        )

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                deployments.insert().values(
                    execution_target_id="OKX_DEMO",
                    candidate_approval_id=102,
                    strategy_id=202,
                    strategy_version_id=302,
                    candidate_digest="f" * 64,
                    promotion_policy_version="v1",
                    deployment_policy_digest="1" * 64,
                    real_orders=False,
                    instrument_id="XRP-USDT-SWAP",
                    timeframe="1h",
                    status="ACTIVE",
                    active_slot=0,
                    evidence_snapshot={},
                )
            )


def test_canonical_json_and_digest_are_order_and_timezone_stable() -> None:
    instant = datetime(
        2026, 8, 13, 12, 34, 56, tzinfo=timezone(timedelta(hours=8))
    )
    first = {"z": 1, "at": instant, "a": "策略"}
    second = {"a": "策略", "z": 1, "at": instant}
    expected_json = '{"a":"\\u7b56\\u7565","at":"2026-08-13T04:34:56Z","z":1}'

    assert canonical_json(first) == expected_json
    assert canonical_json(second) == expected_json
    assert canonical_digest(first) == canonical_digest(second)
    assert canonical_digest(first) == hashlib.sha256(expected_json.encode()).hexdigest()
    with pytest.raises(TypeError, match="unsupported canonical JSON value"):
        canonical_json({"unsupported": object()})


def test_market_inventory_accepts_only_complete_six_target_real_evidence() -> None:
    inventory = _market_inventory()

    assert validate_market_inventory(inventory) == inventory
    with pytest.raises(StrategyPlatformTask1Blocked, match="exactly"):
        validate_market_inventory(inventory[:-1])
    with pytest.raises(StrategyPlatformTask1Blocked, match="acceptance-grade"):
        validate_market_inventory(
            (replace(inventory[0], gap_count=1), *inventory[1:])
        )
    with pytest.raises(StrategyPlatformTask1Blocked, match="acceptance-grade"):
        validate_market_inventory(
            (replace(inventory[0], sha256="not-a-digest"), *inventory[1:])
        )
    with pytest.raises(StrategyPlatformTask1Blocked, match="acceptance-grade"):
        validate_market_inventory(
            (
                replace(inventory[0], expected_interval_seconds=600),
                *inventory[1:],
            )
        )


def test_classification_requires_file_bound_complete_and_matching_evidence() -> None:
    inventory = _market_inventory()
    first = inventory[0]
    assert first.classification_windows is not None

    missing = dict(first.classification_windows)
    missing.pop("wf_bear")
    with pytest.raises(StrategyPlatformTask1Blocked, match="incomplete"):
        validate_market_inventory(
            (replace(first, classification_windows=missing), *inventory[1:])
        )

    wrong_digest = {
        key: dict(value) for key, value in first.classification_windows.items()
    }
    wrong_digest["wf_bull"]["market_data_digest"] = "f" * 64
    with pytest.raises(StrategyPlatformTask1Blocked, match="invalid"):
        validate_market_inventory(
            (replace(first, classification_windows=wrong_digest), *inventory[1:])
        )

    wrong_regime = {
        key: dict(value) for key, value in first.classification_windows.items()
    }
    wrong_regime["wf_bull"]["actual_regime"] = "bear"
    with pytest.raises(StrategyPlatformTask1Blocked, match="invalid"):
        validate_market_inventory(
            (replace(first, classification_windows=wrong_regime), *inventory[1:])
        )


class _FakeResult:
    def scalar_one(self) -> str:
        return "public"


class _FakePostgresqlConnection:
    def __init__(self) -> None:
        self.dialect = postgresql.dialect()
        self.statements: list[str] = []

    def execute(self, statement):
        self.statements.append(str(statement))
        return _FakeResult()


def test_schema_installer_is_additive_and_reentrant_by_contract(monkeypatch) -> None:
    class FakeInspector:
        @staticmethod
        def get_table_names() -> list[str]:
            return [
                "configuration_versions",
                "configuration_bundle_snapshots",
                "strategy_deployments",
                "strategy_research_candidates",
                "strategy_targets",
                "trade_intents",
            ]

    created: list[tuple[str, bool]] = []

    def record_create(table, *, bind, checkfirst):
        assert bind is connection
        created.append((table.name, checkfirst))

    connection = _FakePostgresqlConnection()
    monkeypatch.setattr(task1, "inspect", lambda _: FakeInspector())
    monkeypatch.setattr(Table, "create", record_create)

    install_strategy_platform_v13_task1_schema(connection)  # type: ignore[arg-type]
    install_strategy_platform_v13_task1_schema(connection)  # type: ignore[arg-type]

    assert Counter(name for name, _ in created) == Counter(
        {name: 2 for name in EXPECTED_EXTENSION_TABLES}
    )
    assert all(checkfirst for _, checkfirst in created)
    ddl = "\n".join(connection.statements)
    for fragment in (
        "ADD COLUMN IF NOT EXISTS strategy_target_id",
        "ADD COLUMN IF NOT EXISTS deployment_id",
        "pair IS NULL OR length(pair) > 0",
        "timeframe IS NULL OR length(timeframe) > 0",
        "active_slot > 0",
        "CREATE INDEX IF NOT EXISTS",
        "guard_strategy_platform_migration_audit",
    ):
        assert fragment in ddl
    for destructive_fragment in (
        "DROP TABLE",
        "DROP COLUMN",
        "TRUNCATE",
        "DELETE FROM",
    ):
        assert destructive_fragment not in ddl.upper()


def test_schema_installer_fails_closed_when_foundation_is_missing() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        with pytest.raises(StrategyPlatformTask1Blocked, match="prerequisites"):
            install_strategy_platform_v13_task1_schema(connection)


def test_migration_audit_constraints_make_retries_idempotent_and_preserve_passed() -> None:
    engine = _sqlite_engine()
    runs = Base.metadata.tables["strategy_platform_migration_runs"]
    mappings = Base.metadata.tables["strategy_platform_migration_entity_mappings"]
    run_values = {
        "migration_key": task1.TASK1_MIGRATION_KEY,
        "execution_scope": "DESIGN_LAB",
        "source_schema_version": "20260811_45",
        "target_schema_version": task1.TASK1_SCHEMA_VERSION,
        "source_snapshot_digest": "a" * 64,
        "status": "RUNNING",
        "operator_identity": "test",
        "request_id": "task1-contract",
        "destructive_write_count": 0,
        "overwritten_row_count": 0,
        "deleted_row_count": 0,
        "unknown_dimensions": ["credential_attestation"],
    }
    with engine.begin() as connection:
        run_id = connection.execute(
            runs.insert().values(**run_values).returning(runs.c.id)
        ).scalar_one()
        connection.execute(
            mappings.insert().values(
                migration_run_id=run_id,
                source_table="strategy_versions",
                source_primary_key="491",
                mapping_kind="legacy-quality-preservation",
                mapping_status="PRESERVED",
                mapping_reason="static PASSED is not dynamic qualification evidence",
                quality_status_asserted="UNKNOWN",
                dynamic_quality_evidence_id=None,
                evidence_snapshot={
                    "legacy_validation_status": "PASSED",
                    "qualified": False,
                },
            )
        )
        preserved = connection.execute(
            select(
                mappings.c.quality_status_asserted,
                mappings.c.dynamic_quality_evidence_id,
            )
        ).one()
        assert tuple(preserved) == ("UNKNOWN", None)

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(runs.insert().values(**run_values))

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                mappings.insert().values(
                    migration_run_id=run_id,
                    source_table="strategy_versions",
                    source_primary_key="491",
                    mapping_kind="legacy-quality-preservation",
                    mapping_status="PRESERVED",
                    mapping_reason="retry must not duplicate mapping",
                    quality_status_asserted="UNKNOWN",
                    evidence_snapshot={},
                )
            )

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                mappings.insert().values(
                    migration_run_id=run_id,
                    source_table="strategy_scores",
                    source_primary_key="430",
                    mapping_kind="legacy-quality-preservation",
                    mapping_status="PRESERVED",
                    mapping_reason="legacy PASSED has no dynamic quality evidence",
                    quality_status_asserted="QUALIFIED",
                    dynamic_quality_evidence_id=None,
                    evidence_snapshot={"legacy_status": "PASSED"},
                )
            )

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                runs.insert().values(
                    **{
                        **run_values,
                        "source_snapshot_digest": "b" * 64,
                        "overwritten_row_count": 1,
                    }
                )
            )
