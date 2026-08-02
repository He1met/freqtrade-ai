import pytest

from app.core.exceptions import ConfigurationError
from app.db.migrations import (
    ATTESTATION_ACL_BASE_VERSION,
    ATTESTED_SESSION_BASE_VERSION,
    CANARY_LINEAGE_WRITE_BASE_VERSION,
    CANARY_FINAL_EXPIRY_BASE_VERSION,
    CANARY_LIFECYCLE_BASE_VERSION,
    EARLY_TARGET_LINEAGE_VERSION,
    DUAL_SIDE_BASE_VERSION,
    FULL_CHAIN_BASE_VERSION,
    HMAC_ATTESTATION_BASE_VERSION,
    LEGACY_SCHEMA_VERSION,
    ORDER_WRITER_BASE_VERSION,
    PREVIOUS_SCHEMA_VERSION,
    RECONCILIATION_INDEX_BASE_VERSION,
    RECONCILIATION_BATCH_FRESHNESS_BASE_VERSION,
    RECONCILIATION_BASE_VERSION,
    RECOVERY_WALL_CLOCK_BASE_VERSION,
    RISK_CHAIN_BASE_VERSION,
    RISK_CHAIN_HARDENING_BASE_VERSION,
    RUNTIME_APP_ACL_BASE_VERSION,
    FILL_SNAPSHOT_REPEAT_BASE_VERSION,
    RUNTIME_RECOVERY_BASE_VERSION,
    STRATEGY_PROMOTION_BASE_VERSION,
    STRATEGY_DEPLOYMENT_BASE_VERSION,
    SINGLE_ACTIVE_DEPLOYMENT_BASE_VERSION,
    STRATEGY_VALIDATION_BASE_VERSION,
    SCHEMA_VERSION,
    SOAK_BASE_VERSION,
    TARGET_LINEAGE_BASE_VERSION,
    TRUSTED_SNAPSHOT_BASE_VERSION,
    psql_database_url,
    schema_problems,
    verify_schema,
    _add_controlled_canary_lifecycle_boundary,
    _add_canary_consent_handoff_boundary,
)
from app.db.session import create_database_engine


def test_psql_url_strips_sqlalchemy_driver_and_password() -> None:
    url = psql_database_url(
        "postgresql+psycopg://freqtrade:secret-value@localhost:5432/freqtrade_ai"
    )

    assert url == "postgresql://freqtrade@localhost:5432/freqtrade_ai"
    assert "secret-value" not in url


def test_psql_url_rejects_non_postgresql_database() -> None:
    with pytest.raises(ConfigurationError, match="PostgreSQL"):
        psql_database_url("sqlite+pysqlite:///:memory:")


def test_schema_verification_fails_closed_for_sqlite() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")

    result = verify_schema(engine)

    assert result.ready is False
    assert result.schema_version is None
    assert result.problems == ("database dialect is not PostgreSQL",)
    assert schema_problems(engine) == ["database dialect is not PostgreSQL"]


def test_schema_version_is_explicit_and_stable() -> None:
    assert LEGACY_SCHEMA_VERSION == "20260712_01"
    assert PREVIOUS_SCHEMA_VERSION == "20260722_01"
    assert TARGET_LINEAGE_BASE_VERSION == "20260723_01"
    assert EARLY_TARGET_LINEAGE_VERSION == "20260727_01"
    assert RISK_CHAIN_BASE_VERSION == "20260727_02"
    assert RISK_CHAIN_HARDENING_BASE_VERSION == "20260727_03"
    assert TRUSTED_SNAPSHOT_BASE_VERSION == "20260727_04"
    assert ATTESTED_SESSION_BASE_VERSION == "20260727_05"
    assert HMAC_ATTESTATION_BASE_VERSION == "20260727_06"
    assert ATTESTATION_ACL_BASE_VERSION == "20260727_07"
    assert ORDER_WRITER_BASE_VERSION == "20260727_08"
    assert RECONCILIATION_BASE_VERSION == "20260727_09"
    assert RUNTIME_RECOVERY_BASE_VERSION == "20260727_10"
    assert FULL_CHAIN_BASE_VERSION == "20260727_11"
    assert SOAK_BASE_VERSION == "20260727_12"
    assert RUNTIME_APP_ACL_BASE_VERSION == "20260727_13"
    assert FILL_SNAPSHOT_REPEAT_BASE_VERSION == "20260727_14"
    assert RECONCILIATION_BATCH_FRESHNESS_BASE_VERSION == "20260728_15"
    assert RECOVERY_WALL_CLOCK_BASE_VERSION == "20260728_16"
    assert DUAL_SIDE_BASE_VERSION == "20260728_17"
    assert STRATEGY_PROMOTION_BASE_VERSION == "20260728_18"
    assert STRATEGY_DEPLOYMENT_BASE_VERSION == "20260729_19"
    assert RECONCILIATION_INDEX_BASE_VERSION == "20260729_21"
    assert SINGLE_ACTIVE_DEPLOYMENT_BASE_VERSION == "20260730_22"
    assert STRATEGY_VALIDATION_BASE_VERSION == "20260801_24"
    assert CANARY_LINEAGE_WRITE_BASE_VERSION == "20260801_25"
    assert CANARY_FINAL_EXPIRY_BASE_VERSION == "20260802_26"
    assert CANARY_LIFECYCLE_BASE_VERSION == "20260802_27"
    assert SCHEMA_VERSION == "20260802_28"


def test_v28_consent_handoff_sql_is_owner_managed_and_exact() -> None:
    import inspect as pyinspect

    source = pyinspect.getsource(_add_canary_consent_handoff_boundary)
    for fragment in (
        "source.id=22",
        "'[15,16,17,18,19,20,21,22]'::jsonb",
        "consent_deadline_at<=statement_timestamp()",
        "exact fresh attested snapshot binding failed",
        "instrument.database_id",
        "instrument.attested_session_id IS DISTINCT FROM market.attested_session_id",
        "REVOKE INSERT ON TABLE",
        "RUNTIME_RESTART_BEFORE_PREPARED",
        "SECURITY DEFINER SET search_path=pg_catalog",
        "okx_demo_operator_consent_secrets",
        "public.hmac(",
        "p_payload||'|'||p_nonce",
    ):
        assert fragment in source


def test_v27_canary_lifecycle_sql_is_fail_closed() -> None:
    import inspect as pyinspect

    source = pyinspect.getsource(_add_controlled_canary_lifecycle_boundary)
    for fragment in (
        "p_expected_version IS NULL",
        "IS DISTINCT FROM p_expected_version",
        "require_current_okx_demo_canary_recovery_run",
        "opening_state IN('live','partially_filled') THEN 'CANCEL_PENDING'",
        "a.operation='PLACE' AND a.attempt_count=1",
        "g.lifecycle_id=l.lifecycle_id AND g.reconciliation_run_id=p_run_id",
        "outcome='FAILED' THEN 'FAILED'",
        "UPDATE SCHEMA_TOKEN.okx_demo_submission_grants SET status='FAILED'",
        "UPDATE SCHEMA_TOKEN.okx_demo_recovery_grants SET status='EXPIRED'",
        "current_user IS DISTINCT FROM 'freqtrade_ai_attestor'",
        "okx_demo_recovery_grants_one_active_lifecycle_action_idx",
        "a.state IN('PREPARED','ACKNOWLEDGED','RECOVERY_REQUIRED','RESIDUAL_CLOSE_REQUIRED')",
    ):
        assert fragment in source
