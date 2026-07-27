import pytest

from app.core.exceptions import ConfigurationError
from app.db.migrations import (
    ATTESTATION_ACL_BASE_VERSION,
    ATTESTED_SESSION_BASE_VERSION,
    EARLY_TARGET_LINEAGE_VERSION,
    FULL_CHAIN_BASE_VERSION,
    HMAC_ATTESTATION_BASE_VERSION,
    LEGACY_SCHEMA_VERSION,
    ORDER_WRITER_BASE_VERSION,
    PREVIOUS_SCHEMA_VERSION,
    RECONCILIATION_BASE_VERSION,
    RISK_CHAIN_BASE_VERSION,
    RISK_CHAIN_HARDENING_BASE_VERSION,
    RUNTIME_APP_ACL_BASE_VERSION,
    FILL_SNAPSHOT_REPEAT_BASE_VERSION,
    RUNTIME_RECOVERY_BASE_VERSION,
    SCHEMA_VERSION,
    SOAK_BASE_VERSION,
    TARGET_LINEAGE_BASE_VERSION,
    TRUSTED_SNAPSHOT_BASE_VERSION,
    psql_database_url,
    schema_problems,
    verify_schema,
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
    assert SCHEMA_VERSION == "20260728_15"
