import pytest

from app.core.exceptions import ConfigurationError
from app.db.migrations import (
    PREVIOUS_SCHEMA_VERSION,
    SCHEMA_VERSION,
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
    assert PREVIOUS_SCHEMA_VERSION == "20260712_01"
    assert SCHEMA_VERSION == "20260722_01"
