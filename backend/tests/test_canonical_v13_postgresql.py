from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from app.canonical_v13.bootstrap import verify_postgresql_bootstrap
from app.canonical_v13.authority_upgrade import (
    PREVIOUS_CANONICAL_MANIFEST_DIGEST,
    apply_authority_upgrade,
    render_authority_rollback_acl_sql,
    rollback_authority_upgrade,
    verify_authority_upgrade_state,
)
from app.canonical_v13.configuration_governance import (
    create_audited_configuration_draft,
)
from app.canonical_v13.genesis import (
    assert_postgresql_acl_sql,
    install_canonical_genesis,
    render_postgresql_acl_sql,
    render_postgresql_owner_sql,
)
from app.canonical_v13.role_mapping import CanonicalRoleMapping
from app.canonical_v13.manifest import CANONICAL_MANIFEST_DIGEST
from app.canonical_v13.models import AUDIT_EVENTS_TABLE, SCHEMA_METADATA_TABLE
from app.canonical_v13.research_authorization import (
    CONSUMED_EVENT,
    REVOKED_EVENT,
    CanonicalResearchAuthorizationBlocked,
    authorize_research_execution,
    consume_research_execution_authorization,
    revoke_research_execution_authorization,
    verify_persisted_research_authorization_consumption,
)
from app.canonical_v13.research_execution import start_consumed_research_attempt
from app.canonical_v13.research_qualification import persist_qualification_receipt
from app.canonical_v13.research_scoring import persist_scoring_receipt
from app.canonical_v13.research_validation import (
    build_ephemeral_attempt_receipt,
    build_ephemeral_launch_spec,
    record_terminal_attempt,
)
from tests.test_canonical_v13_research_validation import (
    EXECUTOR_IMAGE_DIGEST,
    _prepare_ready_plan,
)


DATABASE_URL = os.environ.get("CANONICAL_V13_POSTGRES_URL")
ROLE_PREFIX = os.environ.get(
    "CANONICAL_V13_ROLE_PREFIX", "freqtrade_ai_v13_ci_"
)

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="CANONICAL_V13_POSTGRES_URL is required for the isolated contract",
)


def _statements(sql: str) -> tuple[str, ...]:
    return tuple(statement for statement in sql.split(";\n") if statement.strip())


def test_empty_postgresql_genesis_mapping_acl_and_repeat_noop() -> None:
    assert DATABASE_URL is not None
    mapping = CanonicalRoleMapping.from_prefix(ROLE_PREFIX)
    acl = render_postgresql_acl_sql(mapping)
    assert_postgresql_acl_sql(acl, mapping)
    engine = create_engine(DATABASE_URL)
    try:
        with engine.begin() as connection:
            first = install_canonical_genesis(
                connection, installer_identity="canonical-v13-ci"
            )
            if first.created:
                for statement in _statements(acl):
                    connection.exec_driver_sql(statement)
                for statement in _statements(render_postgresql_owner_sql(mapping)):
                    connection.exec_driver_sql(statement)
            accepted = verify_postgresql_bootstrap(
                connection,
                role_mapping=mapping,
                require_zero_business_rows=True,
            )
        assert first.created is True or first.repeat_noop is True
        assert accepted.accepted is True
        assert accepted.problems == ()
        assert accepted.table_count == 46
        assert accepted.business_row_count == 0

        with engine.begin() as connection:
            repeat = install_canonical_genesis(
                connection, installer_identity="canonical-v13-ci-repeat"
            )
        assert repeat.created is False
        assert repeat.repeat_noop is True

        with engine.connect() as connection:
            transaction = connection.begin()
            connection.exec_driver_sql(
                f"SET LOCAL ROLE {mapping.physical('canonical_api_reader')}"
            )
            savepoint = connection.begin_nested()
            with pytest.raises(DBAPIError) as denied:
                connection.exec_driver_sql(
                    "INSERT INTO strategy_platform_v13.audit_events "
                    "DEFAULT VALUES"
                )
            assert denied.value.orig.sqlstate == "42501"
            savepoint.rollback()
            transaction.rollback()

        with engine.connect() as connection:
            transaction = connection.begin()
            connection.exec_driver_sql(
                f"SET LOCAL ROLE {mapping.physical('canonical_control_writer')}"
            )
            result = create_audited_configuration_draft(
                connection,
                actor_identity="canonical-v13-ci-control",
                idempotency_key="configuration-draft-acl-v1",
                profile_key="configuration-draft-acl-v1",
                configuration_kind="DIVERSITY",
                scope_key="ci",
                workflow_key="research",
                schema_json={"type": "object", "additionalProperties": False},
                payload_json={
                    "rules": [
                        {
                            "rule_key": "exact",
                            "algorithm": "exact-v1",
                            "metric": "duplicate_count",
                            "operator": "==",
                            "threshold": 0,
                        }
                    ]
                },
                adapter_identity="canonical-v13-ci-adapter",
                adapter_digest="a" * 64,
                dependencies=(),
            )
            assert result["idempotent_replay"] is False
            transaction.rollback()

        legacy_role = ROLE_PREFIX + "research_writer"
        with engine.connect() as connection:
            transaction = connection.begin()
            current = verify_authority_upgrade_state(
                connection,
                role_mapping=mapping,
                legacy_research_writer_role=legacy_role,
            )
            assert current.accepted is True
            assert current.state == "CURRENT"

            research_memberships = {
                f"{ROLE_PREFIX}validation_writer": f"{ROLE_PREFIX}validation_login",
                f"{ROLE_PREFIX}scoring_writer": f"{ROLE_PREFIX}scoring_login",
                f"{ROLE_PREFIX}qualification_writer": f"{ROLE_PREFIX}qualification_login",
                f"{ROLE_PREFIX}optimization_writer": f"{ROLE_PREFIX}optimization_login",
            }
            for capability_role, service_principal in research_memberships.items():
                connection.exec_driver_sql(
                    f"GRANT {capability_role} TO {service_principal}"
                )
            default_membership_gate = verify_authority_upgrade_state(
                connection,
                role_mapping=mapping,
                legacy_research_writer_role=legacy_role,
            )
            assert default_membership_gate.accepted is False
            provisioned_membership_gate = verify_authority_upgrade_state(
                connection,
                role_mapping=mapping,
                legacy_research_writer_role=legacy_role,
                allowed_isolated_memberships=frozenset(
                    research_memberships.items()
                ),
            )
            assert provisioned_membership_gate.accepted is True
            assert provisioned_membership_gate.state == "CURRENT"
            for capability_role, service_principal in research_memberships.items():
                connection.exec_driver_sql(
                    f"REVOKE {capability_role} FROM {service_principal}"
                )

            for statement in _statements(
                render_authority_rollback_acl_sql(
                    role_mapping=mapping,
                    legacy_research_writer_role=legacy_role,
                )
            ):
                connection.exec_driver_sql(statement)
            connection.execute(
                SCHEMA_METADATA_TABLE.update()
                .where(
                    SCHEMA_METADATA_TABLE.c.metadata_key
                    == "canonical-v13-genesis",
                    SCHEMA_METADATA_TABLE.c.manifest_digest
                    == CANONICAL_MANIFEST_DIGEST,
                )
                .values(manifest_digest=PREVIOUS_CANONICAL_MANIFEST_DIGEST)
            )
            previous = verify_authority_upgrade_state(
                connection,
                role_mapping=mapping,
                legacy_research_writer_role=legacy_role,
            )
            assert previous.accepted is True
            assert previous.state == "PREVIOUS_READY"

            upgraded = apply_authority_upgrade(
                connection,
                role_mapping=mapping,
                legacy_research_writer_role=legacy_role,
                actor_identity="canonical-v13-ci-authority-upgrade",
            )
            assert upgraded.status == "UPGRADED"
            assert upgraded.generation == 1
            assert upgraded.research_row_count == 0

            event = connection.execute(
                AUDIT_EVENTS_TABLE.select().where(
                    AUDIT_EVENTS_TABLE.c.event_type
                    == "CANONICAL_RESEARCH_AUTHORITY_UPGRADED"
                )
            ).mappings().one()
            connection.execute(
                AUDIT_EVENTS_TABLE.update()
                .where(AUDIT_EVENTS_TABLE.c.id == event["id"])
                .values(receipt_digest="0" * 64)
            )
            drifted = verify_authority_upgrade_state(
                connection,
                role_mapping=mapping,
                legacy_research_writer_role=legacy_role,
            )
            assert drifted.accepted is False
            assert any("receipt drift" in problem for problem in drifted.problems)
            connection.execute(
                AUDIT_EVENTS_TABLE.update()
                .where(AUDIT_EVENTS_TABLE.c.id == event["id"])
                .values(receipt_digest=event["receipt_digest"])
            )

            rolled_back = rollback_authority_upgrade(
                connection,
                role_mapping=mapping,
                legacy_research_writer_role=legacy_role,
                actor_identity="canonical-v13-ci-authority-rollback",
            )
            assert rolled_back.status == "ROLLED_BACK"
            assert rolled_back.generation == 1

            reapplied = apply_authority_upgrade(
                connection,
                role_mapping=mapping,
                legacy_research_writer_role=legacy_role,
                actor_identity="canonical-v13-ci-authority-reapply",
            )
            assert reapplied.status == "UPGRADED"
            assert reapplied.generation == 2
            transaction.rollback()
    finally:
        engine.dispose()


def test_postgresql_control_role_serializes_consume_against_revoke() -> None:
    assert DATABASE_URL is not None
    mapping = CanonicalRoleMapping.from_prefix(ROLE_PREFIX)
    engine = create_engine(DATABASE_URL)
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    try:
        with engine.begin() as connection:
            prepared = _prepare_ready_plan(connection)
            attempt_id = uuid4()
            connection.exec_driver_sql(
                f"SET LOCAL ROLE {mapping.physical('canonical_control_writer')}"
            )
            authorization = authorize_research_execution(
                connection,
                lineage=prepared.lineage,
                attempt_id=attempt_id,
                validation_plan_id=prepared.plan_id,
                validation_plan_digest=prepared.plan_digest,
                actor_identity="canonical-v13-ci-race-control",
                purpose="ONE_NO_TRADE_RESEARCH_ATTEMPT",
                authorized_at=now,
                expires_at=now + timedelta(minutes=5),
                environment_class="PRODUCTION_RESEARCH",
            )

        barrier = Barrier(2)

        def consume() -> str:
            try:
                with engine.begin() as connection:
                    connection.exec_driver_sql(
                        f"SET LOCAL ROLE {mapping.physical('canonical_control_writer')}"
                    )
                    barrier.wait(timeout=5)
                    consume_research_execution_authorization(
                        connection,
                        authorization_id=authorization.authorization_id,
                        expected_lineage=prepared.lineage,
                        validation_plan_id=prepared.plan_id,
                        validation_plan_digest=prepared.plan_digest,
                        attempt_id=attempt_id,
                        actor_identity="canonical-v13-ci-race-consumer",
                        consumed_at=now + timedelta(seconds=1),
                    )
                return "CONSUMED"
            except CanonicalResearchAuthorizationBlocked:
                return "BLOCKED"

        def revoke() -> str:
            try:
                with engine.begin() as connection:
                    connection.exec_driver_sql(
                        f"SET LOCAL ROLE {mapping.physical('canonical_control_writer')}"
                    )
                    barrier.wait(timeout=5)
                    revoke_research_execution_authorization(
                        connection,
                        authorization_id=authorization.authorization_id,
                        actor_identity="canonical-v13-ci-race-revoker",
                        reason="CI_RACE_PROBE",
                        revoked_at=now + timedelta(seconds=1),
                    )
                return "REVOKED"
            except CanonicalResearchAuthorizationBlocked:
                return "BLOCKED"

        with ThreadPoolExecutor(max_workers=2) as executor:
            consume_future = executor.submit(consume)
            revoke_future = executor.submit(revoke)
            outcomes = {
                consume_future.result(timeout=10),
                revoke_future.result(timeout=10),
            }
        assert outcomes in ({"CONSUMED", "BLOCKED"}, {"REVOKED", "BLOCKED"})
        with engine.connect() as connection:
            terminal_count = connection.execute(
                select(func.count())
                .select_from(AUDIT_EVENTS_TABLE)
                .where(
                    AUDIT_EVENTS_TABLE.c.aggregate_id
                    == str(authorization.authorization_id),
                    AUDIT_EVENTS_TABLE.c.event_type.in_((CONSUMED_EVENT, REVOKED_EVENT)),
                )
            ).scalar_one()
        assert terminal_count == 1
    finally:
        engine.dispose()


def test_postgresql_research_roles_enforce_independent_receipt_writers() -> None:
    assert DATABASE_URL is not None
    mapping = CanonicalRoleMapping.from_prefix(ROLE_PREFIX)
    owner_engine = create_engine(DATABASE_URL)
    base_url = make_url(DATABASE_URL)
    service_engines = {
        capability: create_engine(
            base_url.set(username=ROLE_PREFIX + principal_suffix)
        )
        for capability, principal_suffix in {
            "reader": "api_login",
            "control": "control_login",
            "validation": "validation_login",
            "scoring": "scoring_login",
            "qualification": "qualification_login",
        }.items()
    }
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    try:
        with owner_engine.begin() as connection:
            first = install_canonical_genesis(
                connection, installer_identity="canonical-v13-ci-role-chain"
            )
            if first.created:
                for statement in _statements(render_postgresql_acl_sql(mapping)):
                    connection.exec_driver_sql(statement)
                for statement in _statements(render_postgresql_owner_sql(mapping)):
                    connection.exec_driver_sql(statement)
            service_principals = {
                ROLE_PREFIX + "api_login": "canonical_api_reader",
                ROLE_PREFIX + "control_login": "canonical_control_writer",
                ROLE_PREFIX + "validation_login": "canonical_validation_writer",
                ROLE_PREFIX + "scoring_login": "canonical_scoring_writer",
                ROLE_PREFIX + "qualification_login": "canonical_qualification_writer",
                ROLE_PREFIX + "optimization_login": "canonical_optimization_writer",
            }
            for principal, logical_role in service_principals.items():
                connection.exec_driver_sql(
                    f"GRANT {mapping.physical(logical_role)} TO {principal}"
                )
            connection.exec_driver_sql(
                "REVOKE CONNECT ON DATABASE freqtrade_ai_v13_ci_test FROM PUBLIC"
            )
            missing_connect = verify_postgresql_bootstrap(
                connection,
                role_mapping=mapping,
                require_zero_business_rows=False,
                service_principals=service_principals,
            )
            assert missing_connect.accepted is False
            assert "missing service database CONNECT count=6" in (
                missing_connect.problems
            )
            for logical_role in service_principals.values():
                connection.exec_driver_sql(
                    "GRANT CONNECT ON DATABASE freqtrade_ai_v13_ci_test "
                    f"TO {mapping.physical(logical_role)}"
                )
            provisioned = verify_postgresql_bootstrap(
                connection,
                role_mapping=mapping,
                require_zero_business_rows=False,
                service_principals=service_principals,
            )
            assert provisioned.accepted is True
            prepared = _prepare_ready_plan(connection)
            attempt_id = uuid4()
            spec = build_ephemeral_launch_spec(
                connection,
                validation_plan_id=prepared.plan_id,
                expected_plan_digest=prepared.plan_digest,
                executor_identity="canonical-v13-ci-freqtrade-worker",
                executor_image_digest=EXECUTOR_IMAGE_DIGEST,
            )

        with service_engines["control"].begin() as connection:
            assert connection.execute(text("SELECT current_user")).scalar_one() == (
                ROLE_PREFIX + "control_login"
            )
            authorization = authorize_research_execution(
                connection,
                lineage=prepared.lineage,
                attempt_id=attempt_id,
                validation_plan_id=prepared.plan_id,
                validation_plan_digest=prepared.plan_digest,
                actor_identity="canonical-v13-ci-control",
                purpose="ONE_NO_TRADE_RESEARCH_ATTEMPT",
                authorized_at=now,
                expires_at=now + timedelta(minutes=5),
                environment_class="PRODUCTION_RESEARCH",
            )
            consumption = consume_research_execution_authorization(
                connection,
                authorization_id=authorization.authorization_id,
                expected_lineage=prepared.lineage,
                validation_plan_id=prepared.plan_id,
                validation_plan_digest=prepared.plan_digest,
                attempt_id=attempt_id,
                actor_identity="canonical-v13-ci-orchestrator",
                consumed_at=now + timedelta(seconds=1),
            )

        with service_engines["reader"].begin() as connection:
            assert connection.execute(text("SELECT current_user")).scalar_one() == (
                ROLE_PREFIX + "api_login"
            )
            verify_persisted_research_authorization_consumption(
                connection,
                consumption=consumption,
            )

        with service_engines["validation"].begin() as connection:
            assert connection.execute(text("SELECT current_user")).scalar_one() == (
                ROLE_PREFIX + "validation_login"
            )
            running = start_consumed_research_attempt(
                connection,
                launch_spec=spec,
                authorization_consumption=consumption,
            )
            receipt = build_ephemeral_attempt_receipt(
                running,
                metrics_by_window_key={
                    "required-a": {"trade_count": 2, "profit_factor": 2.0},
                    "required-b": {"trade_count": 3, "profit_factor": 1.8},
                },
            )
            record_terminal_attempt(connection, receipt=receipt)

        with service_engines["scoring"].begin() as connection:
            assert connection.execute(text("SELECT current_user")).scalar_one() == (
                ROLE_PREFIX + "scoring_login"
            )
            scoring = persist_scoring_receipt(
                connection,
                validation_plan_id=prepared.plan_id,
                validation_attempt_id=attempt_id,
                scorer_identity="canonical-v13-ci-scorer",
            )

        with pytest.raises(DBAPIError) as denied:
            with service_engines["scoring"].begin() as connection:
                persist_qualification_receipt(
                    connection,
                    validation_plan_id=prepared.plan_id,
                    validation_attempt_id=attempt_id,
                    qualifier_identity="canonical-v13-ci-forbidden-qualifier",
                )
        assert denied.value.orig.sqlstate == "42501"

        with service_engines["qualification"].begin() as connection:
            assert connection.execute(text("SELECT current_user")).scalar_one() == (
                ROLE_PREFIX + "qualification_login"
            )
            qualification = persist_qualification_receipt(
                connection,
                validation_plan_id=prepared.plan_id,
                validation_attempt_id=attempt_id,
                qualifier_identity="canonical-v13-ci-qualifier",
            )
            assert scoring.validation_attempt_id == attempt_id
            assert qualification.target_score_id == scoring.target_score_id
            assert qualification.status == "QUALIFIED"
    finally:
        for engine in service_engines.values():
            engine.dispose()
        owner_engine.dispose()
