from collections.abc import Generator
from contextlib import contextmanager
from typing import Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.exceptions import ConfigurationError


def redact_database_url(database_url: str) -> str:
    try:
        return make_url(database_url).render_as_string(hide_password=True)
    except ArgumentError:
        return "<invalid database url>"


def create_database_engine(database_url: Optional[str] = None) -> Engine:
    resolved_url = database_url or get_settings().database_url
    if not resolved_url:
        raise ConfigurationError("DATABASE_URL is required to create the database engine.")

    try:
        return create_engine(resolved_url, pool_pre_ping=True)
    except ArgumentError as exc:
        raise ConfigurationError(f"Invalid DATABASE_URL: {redact_database_url(resolved_url)}") from exc


def create_session_factory(bind: Engine) -> sessionmaker:
    return sessionmaker(bind=bind, autoflush=False, autocommit=False)


engine = create_database_engine()
SessionLocal = create_session_factory(engine)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope(session_factory: sessionmaker = SessionLocal) -> Generator[Session, None, None]:
    db = session_factory()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def verify_database_connection(database_engine: Engine = engine) -> None:
    try:
        with database_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        safe_url = database_engine.url.render_as_string(hide_password=True)
        raise ConfigurationError(f"Database connection failed for {safe_url}: {exc.__class__.__name__}") from exc
