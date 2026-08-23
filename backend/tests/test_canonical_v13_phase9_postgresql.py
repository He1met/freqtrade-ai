from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from uuid import uuid4

import pytest
from app.canonical_v13.acceptance_signal_trigger_upgrade import (
    ACCEPTANCE_CONTROL_WRITER_READ_DELTA,
    ACCEPTANCE_SIGNAL_GUARD_TRIGGER,
    ACCEPTANCE_TRIGGER_GUARD_TRIGGER,
    CanonicalAcceptanceSignalTriggerUpgradeBlocked,
    apply_acceptance_signal_trigger_upgrade,
    rollback_acceptance_signal_trigger_upgrade,
    verify_acceptance_signal_trigger_upgrade,
)
from app.canonical_v13.bootstrap import verify_postgresql_bootstrap
from app.canonical_v13.phase9_schema_upgrade import (
    PHASE9_DATABASE_CONNECT_DELTA,
    PHASE9_EXTENSION_TABLE_NAMES,
    PHASE9_UNIQUE_CONSTRAINTS,
    CanonicalPhase9SchemaUpgradeBlocked,
    apply_phase9_schema_upgrade,
    rollback_phase9_schema_upgrade,
    verify_phase9_schema_upgrade,
)
from app.canonical_v13.role_mapping import CanonicalRoleMapping
from app.canonical_v13.runtime_image_upgrade import (
    CanonicalRuntimeImageUpgradeBlocked,
    apply_runtime_image_upgrade,
    rollback_runtime_image_upgrade,
)
from app.canonical_v13.deployment_rollover_upgrade import (
    CanonicalDeploymentRolloverUpgradeBlocked,
    apply_deployment_rollover_upgrade,
    rollback_deployment_rollover_upgrade,
    verify_deployment_rollover_upgrade,
)
from app.canonical_v13.deployment_approval import approve_demo_deployment
from app.canonical_v13.deployment_control import create_demo_deployment
from app.canonical_v13.runtime_image_authority import (
    RUNTIME_IMAGE_BASE_DIGEST,
    RUNTIME_IMAGE_TITLE,
    RuntimeImageInspection,
    accept_runtime_image,
)
from app.canonical_v13.models import (
    ACCEPTANCE_SIGNAL_TRIGGERS_TABLE,
    DEPLOYMENTS_TABLE,
    OPTIMIZATION_RUNS_TABLE,
    OPTIMIZATION_TRIALS_TABLE,
    QUALIFICATION_DECISIONS_TABLE,
    RUNTIME_IMAGE_ACCEPTANCES_TABLE,
    SCHEMA_METADATA_TABLE,
    SIGNALS_TABLE,
)
from app.canonical_v13.optimization import optimization_selection_digest
from app.canonical_v13.optimization_observability_upgrade import (
    OPTIMIZATION_OBSERVABILITY_COLUMNS,
    OPTIMIZATION_OBSERVABILITY_GUARD_FUNCTION,
    OPTIMIZATION_OBSERVABILITY_GUARD_TRIGGER,
    PREVIOUS_OPTIMIZATION_OBSERVABILITY_MANIFEST_DIGEST,
    apply_optimization_observability_upgrade,
    verify_optimization_observability_upgrade,
)
from app.canonical_v13.runtime_reader_acl_upgrade import (
    PREVIOUS_RUNTIME_READER_ACL_MANIFEST_DIGEST,
    CanonicalRuntimeReaderAclUpgradeBlocked,
    apply_runtime_reader_acl_upgrade,
    rollback_runtime_reader_acl_upgrade,
    verify_runtime_reader_acl_upgrade,
)
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import DBAPIError

DATABASE_URL = os.environ.get("CANONICAL_V13_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="CANONICAL_V13_POSTGRES_URL is required for the isolated contract",
)


def test_acceptance_signal_trigger_is_database_immutable() -> None:
    assert DATABASE_URL is not None
    engine = create_engine(DATABASE_URL)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                mapping = CanonicalRoleMapping.from_prefix(
                    os.environ.get(
                        "CANONICAL_V13_ROLE_PREFIX", "freqtrade_ai_v13_ci_"
                    )
                )
                previous = verify_acceptance_signal_trigger_upgrade(
                    connection, role_mapping=mapping
                )
                assert previous.status == "PREVIOUS_READY"
                upgraded_initial = apply_acceptance_signal_trigger_upgrade(
                    connection, role_mapping=mapping
                )
                assert upgraded_initial.status == "UPGRADED"
                verified = verify_acceptance_signal_trigger_upgrade(
                    connection, role_mapping=mapping
                )
                assert verified.status == "ACCEPTED"
                composed = verify_postgresql_bootstrap(
                    connection,
                    role_mapping=mapping,
                    require_zero_business_rows=False,
                )
                assert composed.explicit_acl_count == 363
                assert not any(
                    "table grants" in problem for problem in composed.problems
                )
                assert verified.immutability_trigger_present is True
                rolled_back = rollback_acceptance_signal_trigger_upgrade(
                    connection, role_mapping=mapping
                )
                assert rolled_back.status == "ROLLED_BACK"
                assert rolled_back.immutability_trigger_present is False
                signal_writer = mapping.physical("canonical_signal_writer")
                control_writer = mapping.physical("canonical_control_writer")
                for table_name in (
                    "deployment_approvals",
                    "qualification_decisions",
                    "research_targets",
                    "runtime_image_acceptances",
                ):
                    assert connection.execute(
                        text(
                            "SELECT has_table_privilege(:role, :table, 'SELECT')"
                        ),
                        {
                            "role": signal_writer,
                            "table": f"strategy_platform_v13.{table_name}",
                        },
                    ).scalar_one() is False
                for table_name in ACCEPTANCE_CONTROL_WRITER_READ_DELTA:
                    assert connection.execute(
                        text("SELECT has_table_privilege(:role, :table, 'SELECT')"),
                        {
                            "role": control_writer,
                            "table": f"strategy_platform_v13.{table_name}",
                        },
                    ).scalar_one() is False
                assert (
                    rollback_acceptance_signal_trigger_upgrade(
                        connection, role_mapping=mapping
                    ).status
                    == "PREVIOUS_READY"
                )
                upgraded = apply_acceptance_signal_trigger_upgrade(
                    connection, role_mapping=mapping
                )
                assert upgraded.status == "UPGRADED"
                assert upgraded.immutability_trigger_present is True
                for table_name in (
                    "deployment_approvals",
                    "qualification_decisions",
                    "research_targets",
                    "runtime_image_acceptances",
                ):
                    assert connection.execute(
                        text(
                            "SELECT has_table_privilege(:role, :table, 'SELECT')"
                        ),
                        {
                            "role": signal_writer,
                            "table": f"strategy_platform_v13.{table_name}",
                        },
                    ).scalar_one() is True
                for table_name in ACCEPTANCE_CONTROL_WRITER_READ_DELTA:
                    assert connection.execute(
                        text("SELECT has_table_privilege(:role, :table, 'SELECT')"),
                        {
                            "role": control_writer,
                            "table": f"strategy_platform_v13.{table_name}",
                        },
                    ).scalar_one() is True
                connection.execute(
                    text(
                        "REVOKE SELECT ON TABLE "
                        "strategy_platform_v13.qualification_decisions "
                        f"FROM {control_writer}"
                    )
                )
                previous = verify_acceptance_signal_trigger_upgrade(
                    connection, role_mapping=mapping
                )
                assert previous.status == "PREVIOUS_READY"
                connection.execute(
                    text(
                        "GRANT INSERT ON TABLE "
                        "strategy_platform_v13.qualification_decisions "
                        f"TO {control_writer}"
                    )
                )
                with pytest.raises(CanonicalAcceptanceSignalTriggerUpgradeBlocked):
                    verify_acceptance_signal_trigger_upgrade(
                        connection, role_mapping=mapping
                    )
                tampered_composition = verify_postgresql_bootstrap(
                    connection,
                    role_mapping=mapping,
                    require_zero_business_rows=False,
                )
                assert tampered_composition.accepted is False
                assert "extra table grants count=1" in tampered_composition.problems
                with pytest.raises(CanonicalAcceptanceSignalTriggerUpgradeBlocked):
                    apply_acceptance_signal_trigger_upgrade(
                        connection, role_mapping=mapping
                    )
                connection.execute(
                    text(
                        "REVOKE INSERT ON TABLE "
                        "strategy_platform_v13.qualification_decisions "
                        f"FROM {control_writer}"
                    )
                )
                upgraded_acl = apply_acceptance_signal_trigger_upgrade(
                    connection, role_mapping=mapping
                )
                assert upgraded_acl.status == "UPGRADED"
                assert upgraded_acl.repeat_noop is False
                for table_name in ACCEPTANCE_CONTROL_WRITER_READ_DELTA:
                    assert connection.execute(
                        text("SELECT has_table_privilege(:role, :table, 'SELECT')"),
                        {
                            "role": control_writer,
                            "table": f"strategy_platform_v13.{table_name}",
                        },
                    ).scalar_one() is True
                assert (
                    apply_acceptance_signal_trigger_upgrade(
                        connection, role_mapping=mapping
                    ).status
                    == "ACCEPTED"
                )
                trigger_id = uuid4()
                scheduled_at = datetime(2026, 8, 23, 14, 0, tzinfo=timezone.utc)
                connection.exec_driver_sql(
                    "SET LOCAL session_replication_role = replica"
                )
                connection.execute(
                    ACCEPTANCE_SIGNAL_TRIGGERS_TABLE.insert().values(
                        id=trigger_id,
                        qualification_decision_id=uuid4(),
                        deployment_approval_id=uuid4(),
                        deployment_id=uuid4(),
                        runtime_instance_id=uuid4(),
                        runtime_image_acceptance_id=uuid4(),
                        strategy_version_id=uuid4(),
                        research_target_id=uuid4(),
                        configuration_bundle_id=uuid4(),
                        configuration_bundle_digest="1" * 64,
                        market_snapshot_id=uuid4(),
                        market_snapshot_digest="2" * 64,
                        source_kind="ACCEPTANCE_SCHEDULED_TEST",
                        execution_target="OKX_DEMO",
                        allow_real_funds=False,
                        acceptance_only=True,
                        position_policy="LONG_ONLY",
                        max_order_count=1,
                        scheduled_at=scheduled_at,
                        expires_at=scheduled_at + timedelta(minutes=2),
                        idempotency_key="postgresql-immutable-trigger",
                        request_digest="3" * 64,
                        receipt_digest="4" * 64,
                        created_at=scheduled_at - timedelta(minutes=1),
                    )
                )
                signal_id = uuid4()
                connection.execute(
                    SIGNALS_TABLE.insert().values(
                        id=signal_id,
                        deployment_id=uuid4(),
                        runtime_instance_id=uuid4(),
                        strategy_version_id=uuid4(),
                        research_target_id=uuid4(),
                        configuration_bundle_id=uuid4(),
                        configuration_bundle_digest="1" * 64,
                        market_snapshot_id=uuid4(),
                        market_snapshot_digest="2" * 64,
                        source_kind="ACCEPTANCE_SCHEDULED_TEST",
                        acceptance_trigger_id=trigger_id,
                        worker_receipt_digest="5" * 64,
                        worker_signer_key_id="isolated-signer",
                        worker_signature_algorithm="HMAC_SHA256_V1",
                        worker_signature="6" * 64,
                        signal_json={
                            "source_kind": "ACCEPTANCE_SCHEDULED_TEST",
                            "natural_signal": False,
                        },
                        signal_digest="7" * 64,
                        created_at=scheduled_at,
                    )
                )
                connection.exec_driver_sql(
                    "SET LOCAL session_replication_role = origin"
                )
                for statement in (
                    ACCEPTANCE_SIGNAL_TRIGGERS_TABLE.update()
                    .where(ACCEPTANCE_SIGNAL_TRIGGERS_TABLE.c.id == trigger_id)
                    .values(expires_at=scheduled_at + timedelta(minutes=3)),
                    ACCEPTANCE_SIGNAL_TRIGGERS_TABLE.delete().where(
                        ACCEPTANCE_SIGNAL_TRIGGERS_TABLE.c.id == trigger_id
                    ),
                    SIGNALS_TABLE.update()
                    .where(SIGNALS_TABLE.c.id == signal_id)
                    .values(worker_signature="8" * 64),
                    SIGNALS_TABLE.delete().where(SIGNALS_TABLE.c.id == signal_id),
                ):
                    with pytest.raises(DBAPIError, match="immutable"):
                        with connection.begin_nested():
                            connection.execute(statement)
                trigger_names = connection.execute(
                    text(
                        "SELECT tgname FROM pg_catalog.pg_trigger trigger "
                        "JOIN pg_catalog.pg_class relation ON relation.oid=trigger.tgrelid "
                        "JOIN pg_catalog.pg_namespace namespace ON namespace.oid=relation.relnamespace "
                        "WHERE namespace.nspname='strategy_platform_v13' "
                        "AND relation.relname IN ('acceptance_signal_triggers', 'signals') "
                        "AND NOT trigger.tgisinternal"
                    )
                ).scalars()
                assert {
                    ACCEPTANCE_TRIGGER_GUARD_TRIGGER,
                    ACCEPTANCE_SIGNAL_GUARD_TRIGGER,
                }.issubset(set(trigger_names))
            finally:
                transaction.rollback()
    finally:
        engine.dispose()


def test_acceptance_signal_trigger_postgresql_concurrent_single_winner() -> None:
    assert DATABASE_URL is not None
    engine = create_engine(DATABASE_URL)
    deployment_id = uuid4()
    scheduled_at = datetime(2026, 8, 23, 14, 0, tzinfo=timezone.utc)
    barrier = Barrier(2)

    def insert_once(index: int) -> str:
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    "SET LOCAL session_replication_role = replica"
                )
                barrier.wait(timeout=5)
                connection.execute(
                    ACCEPTANCE_SIGNAL_TRIGGERS_TABLE.insert().values(
                        id=uuid4(),
                        qualification_decision_id=uuid4(),
                        deployment_approval_id=uuid4(),
                        deployment_id=deployment_id,
                        runtime_instance_id=uuid4(),
                        runtime_image_acceptance_id=uuid4(),
                        strategy_version_id=uuid4(),
                        research_target_id=uuid4(),
                        configuration_bundle_id=uuid4(),
                        configuration_bundle_digest="1" * 64,
                        market_snapshot_id=uuid4(),
                        market_snapshot_digest="2" * 64,
                        source_kind="ACCEPTANCE_SCHEDULED_TEST",
                        execution_target="OKX_DEMO",
                        allow_real_funds=False,
                        acceptance_only=True,
                        position_policy="LONG_ONLY",
                        max_order_count=1,
                        scheduled_at=scheduled_at,
                        expires_at=scheduled_at + timedelta(minutes=2),
                        idempotency_key=f"postgresql-concurrent-trigger-{index}",
                        request_digest=str(index + 3) * 64,
                        receipt_digest=str(index + 5) * 64,
                        created_at=scheduled_at - timedelta(minutes=1),
                    )
                )
            return "INSERTED"
        except DBAPIError as exc:
            assert exc.orig.sqlstate == "23505"
            return "REJECTED_UNIQUE"

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = tuple(executor.map(insert_once, range(2)))
        assert sorted(outcomes) == ["INSERTED", "REJECTED_UNIQUE"]
        with engine.begin() as connection:
            assert connection.execute(
                select(func.count())
                .select_from(ACCEPTANCE_SIGNAL_TRIGGERS_TABLE)
                .where(ACCEPTANCE_SIGNAL_TRIGGERS_TABLE.c.deployment_id == deployment_id)
            ).scalar_one() == 1
    finally:
        with engine.begin() as connection:
            connection.exec_driver_sql("SET LOCAL session_replication_role = replica")
            connection.execute(
                ACCEPTANCE_SIGNAL_TRIGGERS_TABLE.delete().where(
                    ACCEPTANCE_SIGNAL_TRIGGERS_TABLE.c.deployment_id == deployment_id
                )
            )
        engine.dispose()


def test_optimization_observability_upgrade_backfills_only_canonical_trials() -> None:
    assert DATABASE_URL is not None
    mapping = CanonicalRoleMapping.from_prefix(
        os.environ.get("CANONICAL_V13_ROLE_PREFIX", "freqtrade_ai_v13_ci_")
    )
    engine = create_engine(DATABASE_URL)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                is_superuser = bool(
                    connection.execute(
                        text(
                            "SELECT rolsuper FROM pg_catalog.pg_roles "
                            "WHERE rolname=current_user"
                        )
                    ).scalar_one()
                )
                if not is_superuser:
                    pytest.skip(
                        "isolated optimization upgrade regression requires superuser"
                    )
                run_id = uuid4()
                request_digest = "1" * 64
                actor = "canonical-upgrade-regression"
                base_trial = {
                    "trial_number": 1,
                    "parameters_json": {"period": 12},
                    "metrics_json": {"eligible": False},
                }
                selection_digest = optimization_selection_digest(
                    optimization_run_id=run_id,
                    run_request_digest=request_digest,
                    actor_identity=actor,
                    selected_trial_numbers=(),
                    trials=(base_trial,),
                )
                connection.exec_driver_sql(
                    "SET LOCAL session_replication_role=replica"
                )
                now = datetime(2026, 8, 24, tzinfo=timezone.utc)
                connection.execute(
                    OPTIMIZATION_RUNS_TABLE.insert().values(
                        id=run_id,
                        baseline_qualification_decision_id=uuid4(),
                        status="BLOCKED",
                        actor_identity=actor,
                        objective_json={"trial_budget": 1},
                        request_digest=request_digest,
                        receipt_digest="2" * 64,
                        terminal_reason_codes=[
                            "ZERO_TRAIN_VALIDATION_ELIGIBLE_FINALISTS"
                        ],
                        trial_count=1,
                        result_count=1,
                        submitted_strategy_count=0,
                        result_digest=selection_digest,
                        created_at=now,
                        completed_at=now,
                    )
                )
                connection.execute(
                    OPTIMIZATION_TRIALS_TABLE.insert().values(
                        id=uuid4(),
                        optimization_run_id=run_id,
                        trial_number=1,
                        actor_identity=actor,
                        environment_class="ISOLATED_TEST",
                        parameters_json=base_trial["parameters_json"],
                        metrics_json={
                            "eligible": False,
                            "selected_finalist": False,
                            "selection_digest": selection_digest,
                        },
                        request_digest="3" * 64,
                        result_digest="4" * 64,
                        submitted_strategy_version_id=None,
                        submission_link_digest=None,
                        created_at=now,
                    )
                )
                connection.exec_driver_sql(
                    f"DROP TRIGGER {OPTIMIZATION_OBSERVABILITY_GUARD_TRIGGER} "
                    "ON strategy_platform_v13.optimization_runs"
                )
                connection.exec_driver_sql(
                    "DROP FUNCTION strategy_platform_v13."
                    f"{OPTIMIZATION_OBSERVABILITY_GUARD_FUNCTION}()"
                )
                for column in OPTIMIZATION_OBSERVABILITY_COLUMNS:
                    connection.exec_driver_sql(
                        "ALTER TABLE strategy_platform_v13.optimization_runs "
                        f"DROP COLUMN {column} CASCADE"
                    )
                connection.execute(
                    SCHEMA_METADATA_TABLE.update()
                    .where(
                        SCHEMA_METADATA_TABLE.c.metadata_key
                        == "canonical-v13-genesis"
                    )
                    .values(
                        manifest_digest=(
                            PREVIOUS_OPTIMIZATION_OBSERVABILITY_MANIFEST_DIGEST
                        )
                    )
                )
                assert (
                    verify_optimization_observability_upgrade(connection).status
                    == "PREVIOUS_READY"
                )
                upgraded = apply_optimization_observability_upgrade(
                    connection, role_mapping=mapping
                )
                connection.exec_driver_sql(
                    "SET LOCAL session_replication_role=origin"
                )
                assert upgraded.status == "UPGRADED"
                assert upgraded.terminal_run_count >= 1
                assert upgraded.trial_count >= 1
                assert upgraded.result_count >= 1
                assert upgraded.submitted_strategy_count == 0
                replay = apply_optimization_observability_upgrade(
                    connection, role_mapping=mapping
                )
                assert replay.status == "ACCEPTED"
                assert replay.repeat_noop is True
                row = connection.execute(
                    select(OPTIMIZATION_RUNS_TABLE).where(
                        OPTIMIZATION_RUNS_TABLE.c.id == run_id
                    )
                ).mappings().one()
                assert row["terminal_reason_codes"] == [
                    "ZERO_TRAIN_VALIDATION_ELIGIBLE_FINALISTS"
                ]
                assert row["trial_count"] == row["result_count"] == 1
                savepoint = connection.begin_nested()
                with pytest.raises(DBAPIError):
                    connection.execute(
                        OPTIMIZATION_RUNS_TABLE.update()
                        .where(OPTIMIZATION_RUNS_TABLE.c.id == run_id)
                        .values(terminal_reason_codes=["TAMPER"])
                    )
                savepoint.rollback()
            finally:
                transaction.rollback()
    finally:
        engine.dispose()


def test_deployment_rollover_upgrade_trigger_replay_and_rollback_guard() -> None:
    assert DATABASE_URL is not None
    mapping = CanonicalRoleMapping.from_prefix(
        os.environ.get("CANONICAL_V13_ROLE_PREFIX", "freqtrade_ai_v13_ci_")
    )
    engine = create_engine(DATABASE_URL)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                assert (
                    verify_deployment_rollover_upgrade(connection).status == "ACCEPTED"
                )
                accepted = verify_deployment_rollover_upgrade(connection)
                assert accepted.runtime_identity_global_constraint_present is False
                assert accepted.runtime_identity_active_index_present is True
                assert (
                    rollback_acceptance_signal_trigger_upgrade(
                        connection, role_mapping=mapping
                    ).status
                    == "ROLLED_BACK"
                )
                connection.exec_driver_sql(
                    "ALTER TABLE strategy_platform_v13.runtime_instances "
                    "ADD CONSTRAINT uq_runtime_instances_runtime_identity "
                    "UNIQUE (runtime_identity)"
                )
                with pytest.raises(
                    CanonicalDeploymentRolloverUpgradeBlocked,
                    match="BLOCKED_PARTIAL_DEPLOYMENT_ROLLOVER_UPGRADE",
                ):
                    verify_deployment_rollover_upgrade(connection)
                connection.exec_driver_sql(
                    "ALTER TABLE strategy_platform_v13.runtime_instances "
                    "DROP CONSTRAINT uq_runtime_instances_runtime_identity"
                )
                rolled_back = rollback_deployment_rollover_upgrade(
                    connection, role_mapping=mapping
                )
                assert rolled_back.status == "ROLLED_BACK"
                assert rolled_back.runtime_identity_global_constraint_present is True
                assert rolled_back.runtime_identity_active_index_present is False
                assert (
                    rollback_deployment_rollover_upgrade(
                        connection, role_mapping=mapping
                    ).status
                    == "PREVIOUS_READY"
                )
                upgraded = apply_deployment_rollover_upgrade(
                    connection, role_mapping=mapping
                )
                assert upgraded.status == "UPGRADED"
                assert upgraded.runtime_identity_global_constraint_present is False
                assert upgraded.runtime_identity_active_index_present is True
                assert (
                    apply_deployment_rollover_upgrade(
                        connection, role_mapping=mapping
                    ).status
                    == "ACCEPTED"
                )
                assert (
                    apply_acceptance_signal_trigger_upgrade(
                        connection, role_mapping=mapping
                    ).status
                    == "UPGRADED"
                )

            finally:
                transaction.rollback()
    finally:
        engine.dispose()


def test_deployment_rollover_postgresql_preserves_disabled_evidence() -> None:
    assert DATABASE_URL is not None
    mapping = CanonicalRoleMapping.from_prefix(
        os.environ.get("CANONICAL_V13_ROLE_PREFIX", "freqtrade_ai_v13_ci_")
    )
    engine = create_engine(DATABASE_URL)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                qualification_id = connection.execute(
                    select(QUALIFICATION_DECISIONS_TABLE.c.id).where(
                        QUALIFICATION_DECISIONS_TABLE.c.status == "QUALIFIED"
                    )
                ).scalar_one()
                approval = approve_demo_deployment(
                    connection,
                    qualification_decision_id=qualification_id,
                    actor_identity="operator:isolated-postgresql-rollover",
                    reason="exercise database rollover guard",
                )
                deployment_id = create_demo_deployment(
                    connection,
                    deployment_approval_id=approval.deployment_approval_id,
                ).deployment_id
                connection.execute(
                    DEPLOYMENTS_TABLE.update()
                    .where(DEPLOYMENTS_TABLE.c.id == deployment_id)
                    .values(status="ACTIVE")
                )
                connection.execute(
                    DEPLOYMENTS_TABLE.update()
                    .where(DEPLOYMENTS_TABLE.c.id == deployment_id)
                    .values(
                        status="DISABLED",
                        disabled_at=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc),
                        disabled_by="operator:isolated-postgresql-rollover",
                        disable_reason="preserve exact disabled deployment evidence",
                        superseded_by_qualification_decision_id=qualification_id,
                        disable_request_digest="1" * 64,
                        disable_receipt_digest="2" * 64,
                    )
                )
                with pytest.raises(DBAPIError, match="immutable"):
                    with connection.begin_nested():
                        connection.execute(
                            DEPLOYMENTS_TABLE.update()
                            .where(DEPLOYMENTS_TABLE.c.id == deployment_id)
                            .values(capability_digest="0" * 64)
                        )
                with pytest.raises(
                    CanonicalDeploymentRolloverUpgradeBlocked,
                    match="BLOCKED_DISABLED_DEPLOYMENT_EVIDENCE_NONZERO",
                ):
                    rollback_deployment_rollover_upgrade(
                        connection, role_mapping=mapping
                    )
            finally:
                transaction.rollback()
    finally:
        engine.dispose()


def test_phase9_postgresql_upgrade_rollback_and_exact_replay() -> None:
    assert DATABASE_URL is not None
    mapping = CanonicalRoleMapping.from_prefix(
        os.environ.get("CANONICAL_V13_ROLE_PREFIX", "freqtrade_ai_v13_ci_")
    )
    engine = create_engine(DATABASE_URL)
    try:
        with engine.begin() as connection:
            initial = verify_phase9_schema_upgrade(connection)
            assert initial.status == "ACCEPTED"
            assert initial.present_constraints == tuple(
                sorted(PHASE9_UNIQUE_CONSTRAINTS)
            )
            assert initial.present_extension_tables == tuple(
                sorted(PHASE9_EXTENSION_TABLE_NAMES)
            )
            assert set(initial.affected_row_counts.values()) == {0}

            database_name = str(
                connection.execute(text("SELECT current_database()")).scalar_one()
            )
            for role in PHASE9_DATABASE_CONNECT_DELTA:
                connection.exec_driver_sql(
                    f'GRANT CONNECT ON DATABASE "{database_name}" '
                    f"TO {mapping.physical(role)}"
                )

            acceptance_rolled_back = rollback_acceptance_signal_trigger_upgrade(
                connection, role_mapping=mapping
            )
            assert acceptance_rolled_back.status == "ROLLED_BACK"
            rollover_rolled_back = rollback_deployment_rollover_upgrade(
                connection, role_mapping=mapping
            )
            assert rollover_rolled_back.status == "ROLLED_BACK"
            reader_rolled_back = rollback_runtime_reader_acl_upgrade(
                connection,
                role_mapping=mapping,
                actor_identity="isolated-postgresql-upgrade-chain",
            )
            assert reader_rolled_back.status == "ROLLED_BACK"
            image_rolled_back = rollback_runtime_image_upgrade(
                connection, role_mapping=mapping
            )
            assert image_rolled_back.status == "ROLLED_BACK"
            assert (
                rollback_runtime_image_upgrade(connection, role_mapping=mapping).status
                == "PREVIOUS_READY"
            )

            rolled_back = rollback_phase9_schema_upgrade(
                connection, role_mapping=mapping
            )
            assert rolled_back.status == "ROLLED_BACK"
            assert rolled_back.present_constraints == ()
            assert rolled_back.present_extension_tables == ()
            assert rolled_back.destructive_row_operations == 0

            rollback_replay = rollback_phase9_schema_upgrade(
                connection, role_mapping=mapping
            )
            assert rollback_replay.status == "PREVIOUS_READY"
            assert rollback_replay.repeat_noop is True

            upgraded = apply_phase9_schema_upgrade(
                connection,
                role_mapping=mapping,
            )
            assert upgraded.status == "UPGRADED"
            assert upgraded.present_constraints == tuple(
                sorted(PHASE9_UNIQUE_CONSTRAINTS)
            )
            assert upgraded.present_extension_tables == tuple(
                sorted(PHASE9_EXTENSION_TABLE_NAMES)
            )
            assert upgraded.destructive_row_operations == 0

            repeated = apply_phase9_schema_upgrade(
                connection,
                role_mapping=mapping,
            )
            assert repeated.status == "ACCEPTED"
            assert repeated.repeat_noop is True
            assert (
                repeated.receipt_digest
                == verify_phase9_schema_upgrade(connection).receipt_digest
            )
            image_upgraded = apply_runtime_image_upgrade(
                connection, role_mapping=mapping
            )
            assert image_upgraded.status == "UPGRADED"
            assert (
                apply_runtime_image_upgrade(connection, role_mapping=mapping).status
                == "ACCEPTED"
            )
            reader_upgraded = apply_runtime_reader_acl_upgrade(
                connection,
                role_mapping=mapping,
                actor_identity="isolated-postgresql-upgrade-chain",
            )
            assert reader_upgraded.status == "UPGRADED"
            rollover_upgraded = apply_deployment_rollover_upgrade(
                connection, role_mapping=mapping
            )
            assert rollover_upgraded.status == "UPGRADED"
            assert (
                apply_deployment_rollover_upgrade(
                    connection, role_mapping=mapping
                ).status
                == "ACCEPTED"
            )
            assert (
                apply_acceptance_signal_trigger_upgrade(
                    connection, role_mapping=mapping
                ).status
                == "UPGRADED"
            )
    finally:
        engine.dispose()


def test_phase9_previous_acl_and_connect_drift_fail_closed_atomically() -> None:
    assert DATABASE_URL is not None
    mapping = CanonicalRoleMapping.from_prefix(
        os.environ.get("CANONICAL_V13_ROLE_PREFIX", "freqtrade_ai_v13_ci_")
    )
    engine = create_engine(DATABASE_URL)
    extra_role = mapping.physical("canonical_approval_writer")
    try:
        with engine.begin() as connection:
            assert (
                rollback_acceptance_signal_trigger_upgrade(
                    connection, role_mapping=mapping
                ).status
                == "ROLLED_BACK"
            )
            assert (
                rollback_deployment_rollover_upgrade(
                    connection, role_mapping=mapping
                ).status
                == "ROLLED_BACK"
            )
            assert (
                rollback_runtime_reader_acl_upgrade(
                    connection,
                    role_mapping=mapping,
                    actor_identity="isolated-postgresql-acl-drift-chain",
                ).status
                == "ROLLED_BACK"
            )
            rollback_runtime_image_upgrade(connection, role_mapping=mapping)
            connection.exec_driver_sql(
                "GRANT DELETE ON TABLE strategy_platform_v13.signals "
                f"TO {extra_role}"
            )

        with pytest.raises(
            CanonicalPhase9SchemaUpgradeBlocked,
            match="BLOCKED_PREVIOUS_ACL_DRIFT",
        ):
            with engine.begin() as connection:
                rollback_phase9_schema_upgrade(connection, role_mapping=mapping)

        with engine.connect() as connection:
            current = verify_phase9_schema_upgrade(connection, role_mapping=mapping)
            assert current.status == "ACCEPTED"

        with engine.begin() as connection:
            connection.exec_driver_sql(
                "REVOKE DELETE ON TABLE strategy_platform_v13.signals "
                f"FROM {extra_role}"
            )
            previous = rollback_phase9_schema_upgrade(connection, role_mapping=mapping)
            assert previous.status == "ROLLED_BACK"
            database_name = str(
                connection.execute(text("SELECT current_database()")).scalar_one()
            )
            connection.exec_driver_sql(
                f'GRANT CONNECT ON DATABASE "{database_name}" TO {extra_role}'
            )
            with pytest.raises(
                CanonicalPhase9SchemaUpgradeBlocked,
                match="BLOCKED_PREVIOUS_DATABASE_CONNECT_DRIFT",
            ):
                verify_phase9_schema_upgrade(connection, role_mapping=mapping)
            connection.exec_driver_sql(
                f'REVOKE CONNECT ON DATABASE "{database_name}" FROM {extra_role}'
            )
            assert (
                verify_phase9_schema_upgrade(connection, role_mapping=mapping).status
                == "PREVIOUS_READY"
            )
            assert (
                apply_phase9_schema_upgrade(connection, role_mapping=mapping).status
                == "UPGRADED"
            )
            assert (
                apply_runtime_image_upgrade(connection, role_mapping=mapping).status
                == "UPGRADED"
            )
            assert (
                apply_runtime_reader_acl_upgrade(
                    connection,
                    role_mapping=mapping,
                    actor_identity="isolated-postgresql-acl-drift-chain",
                ).status
                == "UPGRADED"
            )
            assert (
                apply_deployment_rollover_upgrade(
                    connection, role_mapping=mapping
                ).status
                == "UPGRADED"
            )
            assert (
                apply_acceptance_signal_trigger_upgrade(
                    connection, role_mapping=mapping
                ).status
                == "UPGRADED"
            )
    finally:
        engine.dispose()


def test_runtime_reader_qualification_acl_rollover_is_exact_and_replayable() -> None:
    assert DATABASE_URL is not None
    mapping = CanonicalRoleMapping.from_prefix(
        os.environ.get("CANONICAL_V13_ROLE_PREFIX", "freqtrade_ai_v13_ci_")
    )
    engine = create_engine(DATABASE_URL)
    reader = mapping.physical("canonical_runtime_reader")
    table_name = "strategy_platform_v13.qualification_decisions"
    actor = "isolated-postgresql-runtime-reader-acl-test"
    try:
        with engine.begin() as connection:
            with pytest.raises(
                CanonicalRuntimeReaderAclUpgradeBlocked,
                match="BLOCKED_DEPLOYMENT_ROLLOVER_ROLLBACK_REQUIRED",
            ):
                rollback_runtime_reader_acl_upgrade(
                    connection, role_mapping=mapping, actor_identity=actor
                )
            assert (
                rollback_acceptance_signal_trigger_upgrade(
                    connection, role_mapping=mapping
                ).status
                == "ROLLED_BACK"
            )
            assert (
                rollback_deployment_rollover_upgrade(
                    connection, role_mapping=mapping
                ).status
                == "ROLLED_BACK"
            )
            qualification_count = connection.execute(
                text(f"SELECT count(*) FROM {table_name}")
            ).scalar_one()
            connection.exec_driver_sql(
                f"REVOKE SELECT ON TABLE {table_name} FROM {reader}"
            )
            connection.execute(
                SCHEMA_METADATA_TABLE.update()
                .where(SCHEMA_METADATA_TABLE.c.metadata_key == "canonical-v13-genesis")
                .values(manifest_digest=PREVIOUS_RUNTIME_READER_ACL_MANIFEST_DIGEST)
            )
            previous = verify_runtime_reader_acl_upgrade(
                connection, role_mapping=mapping
            )
            assert previous.status == "PREVIOUS_READY"
            assert previous.qualification_decision_count == qualification_count

            upgraded = apply_runtime_reader_acl_upgrade(
                connection, role_mapping=mapping, actor_identity=actor
            )
            assert upgraded.status == "UPGRADED"
            assert upgraded.qualification_decision_count == qualification_count
            assert upgraded.privileges == {
                "SELECT": True,
                "INSERT": False,
                "UPDATE": False,
                "DELETE": False,
                "TRUNCATE": False,
                "REFERENCES": False,
                "TRIGGER": False,
            }
            replay = apply_runtime_reader_acl_upgrade(
                connection, role_mapping=mapping, actor_identity=actor
            )
            assert replay.status == "ACCEPTED"
            assert replay.repeat_noop is True
            assert replay.receipt_digest == upgraded.receipt_digest

            connection.exec_driver_sql(
                f"GRANT INSERT ON TABLE {table_name} TO {reader}"
            )
            with pytest.raises(
                CanonicalRuntimeReaderAclUpgradeBlocked,
                match="BLOCKED_PARTIAL_RUNTIME_READER_ACL_UPGRADE",
            ):
                verify_runtime_reader_acl_upgrade(connection, role_mapping=mapping)
            connection.exec_driver_sql(
                f"REVOKE INSERT ON TABLE {table_name} FROM {reader}"
            )

            rolled_back = rollback_runtime_reader_acl_upgrade(
                connection, role_mapping=mapping, actor_identity=actor
            )
            assert rolled_back.status == "ROLLED_BACK"
            assert rolled_back.qualification_decision_count == qualification_count
            assert (
                rollback_runtime_reader_acl_upgrade(
                    connection, role_mapping=mapping, actor_identity=actor
                ).status
                == "PREVIOUS_READY"
            )
            reapplied = apply_runtime_reader_acl_upgrade(
                connection, role_mapping=mapping, actor_identity=actor
            )
            assert reapplied.status == "UPGRADED"
            assert (
                apply_runtime_reader_acl_upgrade(
                    connection, role_mapping=mapping, actor_identity=actor
                ).receipt_digest
                == reapplied.receipt_digest
            )
            assert (
                apply_deployment_rollover_upgrade(
                    connection, role_mapping=mapping
                ).status
                == "UPGRADED"
            )
            assert (
                apply_acceptance_signal_trigger_upgrade(
                    connection, role_mapping=mapping
                ).status
                == "UPGRADED"
            )
    finally:
        engine.dispose()


def test_runtime_reader_acl_failed_transaction_restores_predecessor_state() -> None:
    assert DATABASE_URL is not None
    mapping = CanonicalRoleMapping.from_prefix(
        os.environ.get("CANONICAL_V13_ROLE_PREFIX", "freqtrade_ai_v13_ci_")
    )
    engine = create_engine(DATABASE_URL)
    reader = mapping.physical("canonical_runtime_reader")
    table_name = "strategy_platform_v13.qualification_decisions"
    actor = "isolated-postgresql-runtime-reader-acl-failure-test"
    try:
        with engine.begin() as connection:
            assert (
                rollback_acceptance_signal_trigger_upgrade(
                    connection, role_mapping=mapping
                ).status
                == "ROLLED_BACK"
            )
            assert (
                rollback_deployment_rollover_upgrade(
                    connection, role_mapping=mapping
                ).status
                == "ROLLED_BACK"
            )
            connection.exec_driver_sql(
                f"REVOKE SELECT ON TABLE {table_name} FROM {reader}"
            )
            connection.execute(
                SCHEMA_METADATA_TABLE.update()
                .where(SCHEMA_METADATA_TABLE.c.metadata_key == "canonical-v13-genesis")
                .values(manifest_digest=PREVIOUS_RUNTIME_READER_ACL_MANIFEST_DIGEST)
            )

        with pytest.raises(RuntimeError, match="injected failure"):
            with engine.begin() as connection:
                apply_runtime_reader_acl_upgrade(
                    connection, role_mapping=mapping, actor_identity=actor
                )
                raise RuntimeError("injected failure")

        with engine.begin() as connection:
            restored = verify_runtime_reader_acl_upgrade(
                connection, role_mapping=mapping
            )
            assert restored.status == "PREVIOUS_READY"
            assert not any(restored.privileges.values())
            assert (
                apply_runtime_reader_acl_upgrade(
                    connection, role_mapping=mapping, actor_identity=actor
                ).status
                == "UPGRADED"
            )
            assert (
                apply_deployment_rollover_upgrade(
                    connection, role_mapping=mapping
                ).status
                == "UPGRADED"
            )
            assert (
                apply_acceptance_signal_trigger_upgrade(
                    connection, role_mapping=mapping
                ).status
                == "UPGRADED"
            )
    finally:
        engine.dispose()


def test_runtime_image_acceptance_concurrency_append_only_and_rollback_cleanup() -> (
    None
):
    assert DATABASE_URL is not None
    mapping = CanonicalRoleMapping.from_prefix(
        os.environ.get("CANONICAL_V13_ROLE_PREFIX", "freqtrade_ai_v13_ci_")
    )
    engine = create_engine(DATABASE_URL)
    source_commit = "a" * 40
    source_tree_digest = "b" * 64
    recipe_digest = "c" * 64
    sbom_digest = "d" * 64
    image_manifest_digest = "e" * 64
    image_config_digest = "f" * 64

    class Inspector:
        def inspect(self, _reference: str) -> RuntimeImageInspection:
            return RuntimeImageInspection(
                image_manifest_digest=image_manifest_digest,
                image_config_digest=image_config_digest,
                platform="linux",
                architecture="arm64",
                labels={
                    "org.opencontainers.image.title": RUNTIME_IMAGE_TITLE,
                    "org.opencontainers.image.revision": source_commit,
                    "org.opencontainers.image.base.digest": f"sha256:{RUNTIME_IMAGE_BASE_DIGEST}",
                    "io.freqtrade-ai.source-tree-digest": source_tree_digest,
                    "io.freqtrade-ai.build-recipe-digest": recipe_digest,
                    "io.freqtrade-ai.sbom-digest": sbom_digest,
                    "io.freqtrade-ai.demo-only": "true",
                    "io.freqtrade-ai.allow-real-funds": "false",
                },
                entrypoint=("/opt/freqtrade-ai/bin/canonical-v13-runtime",),
                user="65532:65532",
                stop_signal="SIGTERM",
                builder_identity="isolated-postgresql-test",
            )

    def accept_once():
        with engine.begin() as connection:
            return accept_runtime_image(
                connection,
                inspector=Inspector(),
                immutable_reference=f"sha256:{image_config_digest}",
                source_commit=source_commit,
                source_tree_digest=source_tree_digest,
                build_recipe_digest=recipe_digest,
                sbom_digest=sbom_digest,
                accepted_by="canonical-runtime-image-postgresql-test",
                accepted_at=datetime(2026, 8, 21, 9, 0, tzinfo=timezone.utc),
            )

    try:
        with engine.begin() as connection:
            assert (
                rollback_acceptance_signal_trigger_upgrade(
                    connection, role_mapping=mapping
                ).status
                == "ROLLED_BACK"
            )
            assert (
                rollback_deployment_rollover_upgrade(
                    connection, role_mapping=mapping
                ).status
                == "ROLLED_BACK"
            )
            assert (
                rollback_runtime_reader_acl_upgrade(
                    connection,
                    role_mapping=mapping,
                    actor_identity="isolated-postgresql-runtime-image-chain",
                ).status
                == "ROLLED_BACK"
            )
        with ThreadPoolExecutor(max_workers=2) as executor:
            accepted = tuple(executor.map(lambda _index: accept_once(), range(2)))
        assert accepted[0] == accepted[1]
        with engine.begin() as connection:
            with pytest.raises(DBAPIError, match="append-only"):
                connection.execute(
                    RUNTIME_IMAGE_ACCEPTANCES_TABLE.update().values(
                        accepted_by="tampered"
                    )
                )
        with pytest.raises(
            CanonicalRuntimeImageUpgradeBlocked,
            match="BLOCKED_RUNTIME_IMAGE_ACCEPTANCES_NONZERO",
        ):
            with engine.begin() as connection:
                rollback_runtime_image_upgrade(connection, role_mapping=mapping)
    finally:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE strategy_platform_v13.runtime_image_acceptances "
                "DISABLE TRIGGER runtime_image_acceptances_append_only"
            )
            connection.execute(RUNTIME_IMAGE_ACCEPTANCES_TABLE.delete())
            connection.exec_driver_sql(
                "ALTER TABLE strategy_platform_v13.runtime_image_acceptances "
                "ENABLE TRIGGER runtime_image_acceptances_append_only"
            )
            assert (
                rollback_runtime_image_upgrade(connection, role_mapping=mapping).status
                == "ROLLED_BACK"
            )
            assert (
                apply_runtime_image_upgrade(connection, role_mapping=mapping).status
                == "UPGRADED"
            )
            assert (
                apply_runtime_reader_acl_upgrade(
                    connection,
                    role_mapping=mapping,
                    actor_identity="isolated-postgresql-runtime-image-chain",
                ).status
                == "UPGRADED"
            )
            assert (
                apply_deployment_rollover_upgrade(
                    connection, role_mapping=mapping
                ).status
                == "UPGRADED"
            )
            assert (
                apply_acceptance_signal_trigger_upgrade(
                    connection, role_mapping=mapping
                ).status
                == "UPGRADED"
            )
        engine.dispose()
