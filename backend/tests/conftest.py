import json
from typing import Optional

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app.db.strategy_platform_v13_task1 import (
    configuration_bundle_digest,
    configuration_digest,
)
from app.models import ResearchJob
from app.models.strategy_deployment import StrategyDeployment
from app.models.strategy_validation import StrategyValidationPlan
from app.services.operator_authorization import (
    OPERATOR_TOKEN_ENV,
    operator_request_coordinator,
)


_V13_BUNDLE_FIXTURE_MODULES = {
    "test_full_chain_repository.py",
    "test_okx_demo_writer_postgresql.py",
    "test_research_job_postgresql.py",
    "test_risk_chain_postgresql.py",
    "test_strategy_deployment_repository.py",
}
_V13_BUNDLE_BOUND_MODELS = (
    ResearchJob,
    StrategyDeployment,
    StrategyValidationPlan,
)


def _ensure_v13_test_bundle(connection) -> Optional[int]:
    """Create one safe synthetic frozen bundle for legacy PostgreSQL fixtures.

    The helper is deliberately test-only.  It does not weaken the V1.3 database
    trigger: every new workflow row still carries a real FK to a VALIDATED,
    digest-bound, Demo-only bundle.  Pre-V1.3 migration fixtures have no bundle
    trigger yet and are left unchanged.
    """

    if connection.dialect.name != "postgresql":
        return None

    bundle_guard_installed = connection.execute(
        text(
            "SELECT EXISTS ("
            "SELECT 1 FROM pg_trigger trigger "
            "JOIN pg_class relation ON relation.oid=trigger.tgrelid "
            "JOIN pg_namespace namespace ON namespace.oid=relation.relnamespace "
            "WHERE namespace.nspname=current_schema() "
            "AND relation.relname='research_jobs' "
            "AND trigger.tgname='research_jobs_v13_bundle_required' "
            "AND NOT trigger.tgisinternal)"
        )
    ).scalar_one()
    if bundle_guard_installed is not True:
        return None

    type_key = "test-fixture-profile"
    schema_version = "strategy-platform-v13-test-fixture-v1"
    payload = {
        "fixture_scope": "POSTGRESQL_CONTRACT_ONLY",
        "demo_only": True,
        "allow_real_funds": False,
        "single_writer_required": True,
    }
    config_digest = configuration_digest(
        config_type=type_key,
        schema_version=schema_version,
        payload=payload,
    )
    connection.execute(
        text(
            "INSERT INTO configuration_types "
            "(type_key,name_zh,description_zh,schema_version,handler_key,"
            "editor_capability,enabled) VALUES "
            "(:key,:name,:description,'1','strategy-platform-test-fixture-v1',"
            "CAST(:capability AS json),TRUE) ON CONFLICT (type_key) DO NOTHING"
        ),
        {
            "key": type_key,
            "name": "PostgreSQL contract fixture",
            "description": "Synthetic offline bundle used only by database tests.",
            "capability": json.dumps({"test_only": True}, sort_keys=True),
        },
    )
    connection.execute(
        text(
            "INSERT INTO configuration_versions "
            "(type_key,version_number,lifecycle_status,payload_json,schema_version,"
            "config_digest,change_summary,created_by,validated_at) VALUES "
            "(:key,1,'DRAFT',CAST(:payload AS json),:schema_version,:digest,"
            ":summary,'system:test-fixture',NULL) "
            "ON CONFLICT (type_key,version_number) DO NOTHING"
        ),
        {
            "key": type_key,
            "payload": json.dumps(payload, sort_keys=True),
            "schema_version": schema_version,
            "digest": config_digest,
            "summary": "Offline synthetic fixture; no runtime or exchange capability.",
        },
    )
    connection.execute(
        text(
            "UPDATE configuration_versions SET lifecycle_status='VALIDATED',"
            "validated_at=now() WHERE type_key=:key AND version_number=1 "
            "AND lifecycle_status='DRAFT' AND config_digest=:digest"
        ),
        {"key": type_key, "digest": config_digest},
    )
    version_id = int(
        connection.execute(
            text(
                "SELECT id FROM configuration_versions "
                "WHERE type_key=:key AND version_number=1 "
                "AND lifecycle_status='VALIDATED' AND config_digest=:digest"
            ),
            {"key": type_key, "digest": config_digest},
        ).scalar_one()
    )
    map_key = f"{type_key}:{version_id}"
    resolved_versions = {map_key: version_id}
    resolved_digests = {map_key: config_digest}
    capability = {
        "fixture_scope": "POSTGRESQL_CONTRACT_ONLY",
        "demo_only": True,
        "allow_real_funds": False,
        "single_writer_required": True,
        "exchange_access": "NONE",
        "order_submission": "DISABLED",
    }
    bundle_digest = configuration_bundle_digest(
        workflow_kind="TEST_FIXTURE",
        scope_type="TEST_ONLY",
        scope_key="postgresql-contract",
        aggregate_profile_version_id=version_id,
        resolved_versions_json=resolved_versions,
        resolved_digests_json=resolved_digests,
        capability_snapshot=capability,
    )
    connection.execute(
        text(
            "INSERT INTO configuration_bundle_snapshots "
            "(workflow_kind,scope_type,scope_key,aggregate_profile_version_id,"
            "resolved_versions_json,resolved_digests_json,bundle_digest,"
            "capability_snapshot) VALUES "
            "('TEST_FIXTURE','TEST_ONLY','postgresql-contract',:version_id,"
            "CAST(:versions AS json),CAST(:digests AS json),:bundle_digest,"
            "CAST(:capability AS json)) "
            "ON CONFLICT (workflow_kind,scope_type,scope_key,bundle_digest) "
            "DO NOTHING"
        ),
        {
            "version_id": version_id,
            "versions": json.dumps(resolved_versions, sort_keys=True),
            "digests": json.dumps(resolved_digests, sort_keys=True),
            "bundle_digest": bundle_digest,
            "capability": json.dumps(capability, sort_keys=True),
        },
    )
    return int(
        connection.execute(
            text(
                "SELECT id FROM configuration_bundle_snapshots "
                "WHERE workflow_kind='TEST_FIXTURE' AND scope_type='TEST_ONLY' "
                "AND scope_key='postgresql-contract' AND bundle_digest=:digest"
            ),
            {"digest": bundle_digest},
        ).scalar_one()
    )


@pytest.fixture(autouse=True)
def bind_legacy_postgresql_fixtures_to_v13_bundle(request: pytest.FixtureRequest):
    """Keep legacy contract fixtures legal after the V1.3 bundle boundary."""

    if request.path.name not in _V13_BUNDLE_FIXTURE_MODULES:
        yield
        return

    def bind_bundle(session: Session, _flush_context, _instances) -> None:
        pending = [
            row
            for row in session.new
            if isinstance(row, _V13_BUNDLE_BOUND_MODELS)
            and row.configuration_bundle_snapshot_id is None
        ]
        if not pending:
            return
        connection = session.connection()
        if connection.dialect.name != "postgresql":
            return
        current_user, session_user = connection.execute(
            text("SELECT current_user, session_user")
        ).one()
        reset_test_role = current_user == "freqtrade" and session_user != current_user
        if reset_test_role:
            connection.exec_driver_sql("RESET ROLE")
        try:
            bundle_id = _ensure_v13_test_bundle(connection)
        finally:
            if reset_test_role:
                connection.exec_driver_sql("SET LOCAL ROLE freqtrade")
        if bundle_id is None:
            return
        for row in pending:
            row.configuration_bundle_snapshot_id = bundle_id

    event.listen(Session, "before_flush", bind_bundle)
    try:
        yield
    finally:
        event.remove(Session, "before_flush", bind_bundle)


@pytest.fixture(autouse=True)
def reset_operator_request_boundary(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(OPERATOR_TOKEN_ENV, "synthetic-test-operator-token")
    monkeypatch.setenv(
        "FREQTRADE_AI_OKX_DEMO_ATTESTATION_PROOF_KEY",
        "74" * 32,
    )
    operator_request_coordinator.reset_for_tests()
    yield
    operator_request_coordinator.reset_for_tests()
