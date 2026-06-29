from pathlib import Path

import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.core.exceptions import ConfigurationError
from app.db.session import (
    create_database_engine,
    create_session_factory,
    redact_database_url,
    verify_database_connection,
)


def test_settings_reads_database_url_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    get_settings.cache_clear()

    try:
        assert get_settings().database_url == "sqlite+pysqlite:///:memory:"
    finally:
        get_settings.cache_clear()


def test_session_factory_can_create_session() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    session_factory = create_session_factory(engine)

    with session_factory() as session:
        result = session.execute(text("SELECT 1")).scalar_one()

    assert result == 1


def test_verify_database_connection_reports_clear_failure(tmp_path: Path) -> None:
    missing_parent_url = f"sqlite+pysqlite:///{tmp_path / 'missing' / 'db.sqlite'}"
    engine = create_database_engine(missing_parent_url)

    with pytest.raises(ConfigurationError) as exc_info:
        verify_database_connection(engine)

    message = str(exc_info.value)
    assert "Database connection failed" in message
    assert "OperationalError" in message


def test_redact_database_url_hides_password() -> None:
    safe_url = redact_database_url(
        "postgresql+psycopg://freqtrade:placeholder@localhost:5432/freqtrade_ai"
    )

    assert "placeholder" not in safe_url
    assert "***" in safe_url
