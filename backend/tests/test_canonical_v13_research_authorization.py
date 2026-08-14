from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.canonical_v13.models import (
    AUDIT_EVENTS_TABLE,
    DEPLOYMENTS_TABLE,
    ORDERS_TABLE,
    QUALIFICATION_DECISIONS_TABLE,
    TARGET_SCORES_TABLE,
    VALIDATION_WINDOW_RESULTS_TABLE,
)
from app.canonical_v13.research_authorization import (
    CanonicalResearchAuthorizationBlocked,
    authorize_research_execution,
    consume_research_execution_authorization,
)
from app.canonical_v13.research_execution import (
    CanonicalResearchExecutionBlocked,
    SimulatedResearchExecutor,
    execute_consumed_research_attempt,
    start_consumed_research_attempt,
)
from app.canonical_v13.research_validation import (
    build_ephemeral_attempt_receipt,
    build_ephemeral_launch_spec,
    start_validation_attempt,
)
from tests.test_canonical_v13_research_validation import (
    EXECUTOR_IMAGE_DIGEST,
    _metrics,
    _prepare_ready_plan,
    canonical_connection,
)


NOW = datetime(2026, 8, 14, 12, tzinfo=timezone.utc)


def _count(connection, table) -> int:
    return int(connection.execute(select(func.count()).select_from(table)).scalar_one())


def _running(connection):
    prepared = _prepare_ready_plan(connection)
    spec = build_ephemeral_launch_spec(
        connection,
        validation_plan_id=prepared.plan_id,
        expected_plan_digest=prepared.plan_digest,
        executor_identity="canonical-ephemeral-simulator-v1",
        executor_image_digest=EXECUTOR_IMAGE_DIGEST,
    )
    return prepared, start_validation_attempt(connection, launch_spec=spec)


def _authorize(connection, prepared, attempt_id, **changes):
    values = {
        "lineage": prepared.lineage,
        "attempt_id": attempt_id,
        "validation_plan_id": prepared.plan_id,
        "validation_plan_digest": prepared.plan_digest,
        "actor_identity": "explicit-isolated-test-authority",
        "purpose": "ONE_ISOLATED_TEST_ATTEMPT",
        "authorized_at": NOW,
        "expires_at": NOW + timedelta(minutes=5),
    }
    values.update(changes)
    return authorize_research_execution(connection, **values)


def _authorized_running(connection, *, environment_class="ISOLATED_TEST"):
    prepared = _prepare_ready_plan(connection)
    attempt_id = uuid4()
    authorization = _authorize(
        connection,
        prepared,
        attempt_id,
        environment_class=environment_class,
    )
    consumption = consume_research_execution_authorization(
        connection,
        authorization_id=authorization.authorization_id,
        expected_lineage=prepared.lineage,
        validation_plan_id=prepared.plan_id,
        validation_plan_digest=prepared.plan_digest,
        attempt_id=attempt_id,
        actor_identity="isolated-executor",
        consumed_at=NOW + timedelta(seconds=1),
    )
    spec = build_ephemeral_launch_spec(
        connection,
        validation_plan_id=prepared.plan_id,
        expected_plan_digest=prepared.plan_digest,
        executor_identity="canonical-ephemeral-simulator-v1",
        executor_image_digest=EXECUTOR_IMAGE_DIGEST,
    )
    running = start_consumed_research_attempt(
        connection,
        launch_spec=spec,
        authorization_consumption=consumption,
    )
    return prepared, running, authorization, consumption


def test_missing_authority_is_blocked_before_executor_or_results(canonical_connection):
    with canonical_connection.begin():
        prepared, running = _running(canonical_connection)
        with pytest.raises(CanonicalResearchExecutionBlocked) as raised:
            execute_consumed_research_attempt(
                canonical_connection,
                running_attempt=running,
                authorization_consumption=None,
                executor=SimulatedResearchExecutor(_metrics()),
            )
    assert raised.value.code == "BLOCKED_EXPLICIT_AUTHORITY_REQUIRED"
    assert _count(canonical_connection, VALIDATION_WINDOW_RESULTS_TABLE) == 0
    assert _count(canonical_connection, TARGET_SCORES_TABLE) == 0
    assert _count(canonical_connection, QUALIFICATION_DECISIONS_TABLE) == 0


def test_one_shot_authority_executes_only_isolated_fixture_and_never_deploys(
    canonical_connection,
):
    with canonical_connection.begin():
        prepared, running, authorization, consumption = _authorized_running(
            canonical_connection
        )
        result = execute_consumed_research_attempt(
            canonical_connection,
            running_attempt=running,
            authorization_consumption=consumption,
            executor=SimulatedResearchExecutor(_metrics()),
        )
    assert result.attempt_status == "SUCCEEDED"
    assert result.plan_status == "COMPLETE"
    assert result.environment_class == "ISOLATED_TEST"
    assert _count(canonical_connection, VALIDATION_WINDOW_RESULTS_TABLE) == 2
    assert _count(canonical_connection, TARGET_SCORES_TABLE) == 0
    assert _count(canonical_connection, QUALIFICATION_DECISIONS_TABLE) == 0
    assert _count(canonical_connection, DEPLOYMENTS_TABLE) == 0
    assert _count(canonical_connection, ORDERS_TABLE) == 0

    with pytest.raises(CanonicalResearchAuthorizationBlocked) as reused:
        consume_research_execution_authorization(
            canonical_connection,
            authorization_id=authorization.authorization_id,
            expected_lineage=prepared.lineage,
            validation_plan_id=prepared.plan_id,
            validation_plan_digest=prepared.plan_digest,
            attempt_id=running.validation_attempt_id,
            actor_identity="isolated-executor",
            consumed_at=NOW + timedelta(seconds=2),
        )
    assert reused.value.code == "BLOCKED_EXECUTION_AUTHORIZATION_ALREADY_CONSUMED"


def test_expired_or_mixed_lineage_authority_writes_no_consumption(canonical_connection):
    with canonical_connection.begin():
        prepared = _prepare_ready_plan(canonical_connection)
        attempt_id = uuid4()
        authorization = _authorize(
            canonical_connection,
            prepared,
            attempt_id,
            expires_at=NOW + timedelta(seconds=1),
        )
        before = _count(canonical_connection, AUDIT_EVENTS_TABLE)
        with pytest.raises(CanonicalResearchAuthorizationBlocked) as expired:
            consume_research_execution_authorization(
                canonical_connection,
                authorization_id=authorization.authorization_id,
                expected_lineage=prepared.lineage,
                validation_plan_id=prepared.plan_id,
                validation_plan_digest=prepared.plan_digest,
                attempt_id=attempt_id,
                actor_identity="isolated-executor",
                consumed_at=NOW + timedelta(seconds=2),
            )
        after = _count(canonical_connection, AUDIT_EVENTS_TABLE)
    assert expired.value.code == "BLOCKED_EXECUTION_AUTHORIZATION_EXPIRED"
    assert before == after


def test_production_environment_accepts_exact_networkless_writerless_adapter(
    canonical_connection,
):
    unsafe_type = type(
        "RealExecutor",
        (),
        {
            "environment_class": "PRODUCTION_RESEARCH",
            "network_mode": "none",
            "credential_mounts": (),
            "exchange_capabilities": (),
            "order_capabilities": (),
            "writer_capabilities": (),
            "execute": lambda self, attempt: build_ephemeral_attempt_receipt(
                attempt, metrics_by_window_key=_metrics()
            ),
        },
    )
    with canonical_connection.begin():
        prepared, running, _authorization, consumption = _authorized_running(
            canonical_connection,
            environment_class="PRODUCTION_RESEARCH",
        )
        result = execute_consumed_research_attempt(
            canonical_connection,
            running_attempt=running,
            authorization_consumption=consumption,
            executor=unsafe_type(),
        )
    assert result.environment_class == "PRODUCTION_RESEARCH"
    assert result.attempt_status == "SUCCEEDED"
    assert _count(canonical_connection, VALIDATION_WINDOW_RESULTS_TABLE) == 2


def test_authorization_requires_short_lifetime(
    canonical_connection,
) -> None:
    with canonical_connection.begin():
        prepared = _prepare_ready_plan(canonical_connection)
        with pytest.raises(CanonicalResearchAuthorizationBlocked) as lifetime:
            _authorize(
                canonical_connection,
                prepared,
                uuid4(),
                expires_at=NOW + timedelta(minutes=16),
            )
    assert lifetime.value.code == "BLOCKED_AUTHORIZATION_LIFETIME"
