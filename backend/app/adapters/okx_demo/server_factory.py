from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Mapping

from sqlalchemy.orm import Session
from sqlalchemy.engine import Connection

from app.adapters.okx_demo.credential_preflight import OkxDemoPreflightBlocked
from app.adapters.okx_demo.credentials import OkxDemoCredentialsUnavailable
from app.adapters.okx_demo.order_writer import OkxDemoOrderWriter
from app.adapters.okx_demo.read_adapter import (
    OkxDemoReadClient,
    _AttestedWriterCredentialHandle,
    create_attested_okx_demo_read_adapter,
)
from app.adapters.okx_demo.write_semantics import OkxDemoWriteBlocked
from app.adapters.okx_demo.write_transport import (
    _create_attested_writer_credential_bridge,
    _create_production_write_transport,
)
from app.adapters.okx_demo.writer_lock import OkxDemoWriterProcessLock
from app.adapters.okx_demo.writer_repository import SqlAlchemyOrderWriterStore
from app.db.migrations import verify_connection_schema


_DEFAULT_LOCK_PATH = (
    Path.home() / ".freqtrade-ai" / "run" / "okx-demo-order-writer.lock"
)
_SERVER_SESSION_CAPABILITY = object()
_SAFE_EXCEPTION_TYPE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


class OkxDemoServerSessionBlocked(OkxDemoCredentialsUnavailable):
    """Safe, typed failure from the credential-bearing session factory."""

    def __init__(self, *, stage: str, category: str, cause: BaseException) -> None:
        cause_type = type(cause).__name__
        if _SAFE_EXCEPTION_TYPE.fullmatch(cause_type) is None:
            cause_type = "UnknownError"
        self.stage = stage
        self.category = category
        self.cause_type = cause_type
        super().__init__(
            "OKX_DEMO server session blocked "
            "[stage={}, category={}, cause_type={}]".format(
                stage,
                category,
                cause_type,
            )
        )


def _session_failure(
    *,
    stage: str,
    exc: BaseException,
) -> OkxDemoServerSessionBlocked:
    if isinstance(exc, OkxDemoPreflightBlocked):
        category = "PREFLIGHT"
    elif isinstance(exc, OkxDemoCredentialsUnavailable):
        category = "ATTESTATION"
    else:
        category = "UNEXPECTED"
    return OkxDemoServerSessionBlocked(
        stage=stage,
        category=category,
        cause=exc,
    )


class OkxDemoServerSession:
    """One #446-attested read/write session owning the sole local writer lock."""

    def __init__(
        self,
        *,
        read: OkxDemoReadClient,
        credentials: _AttestedWriterCredentialHandle,
        process_lock: OkxDemoWriterProcessLock,
        _capability: object,
    ) -> None:
        if _capability is not _SERVER_SESSION_CAPABILITY:
            raise OkxDemoWriteBlocked(
                "OKX_DEMO server session requires the production factory"
            )
        self.read = read
        self.__credentials = credentials
        self.__process_lock = process_lock
        self.__closed = False
        self.__writer_created = False

    def create_order_writer(self, db: Session) -> OkxDemoOrderWriter:
        """Build the only writer without exposing its authenticated transport."""

        if self.__closed:
            raise OkxDemoWriteBlocked("OKX_DEMO server session is closed")
        if self.__writer_created:
            raise OkxDemoWriteBlocked(
                "OKX_DEMO server session already owns an order writer"
            )
        if not isinstance(db, Session):
            raise OkxDemoWriteBlocked(
                "OKX_DEMO production writer requires a SQLAlchemy session"
            )
        if db.in_transaction():
            raise OkxDemoWriteBlocked(
                "OKX_DEMO production writer requires a clean database session"
            )
        bind = db.get_bind()
        if not isinstance(bind, Connection) or db.connection() is not bind:
            raise OkxDemoWriteBlocked(
                "OKX_DEMO production writer requires a pinned database connection"
            )
        readiness = verify_connection_schema(bind)
        # ``db.connection()`` above intentionally proves exact connection
        # identity, but it also autobegins a Session transaction.  Approval
        # claims own their transaction, so hand the writer a clean Session.
        db.rollback()
        if not readiness.ready:
            raise OkxDemoWriteBlocked(
                "OKX_DEMO production writer requires the current PostgreSQL schema"
            )
        self.__credentials.bind_database(db)
        transport = _create_production_write_transport(self.__credentials)

        class _ProductionOrderWriter(OkxDemoOrderWriter):
            def _post(
                self,
                *,
                path: str,
                body: Mapping[str, Any],
            ) -> Mapping[str, Any]:
                return transport.post(path=path, body=body)

        writer = _ProductionOrderWriter(
            read_client=self.read,
            write_transport=None,
            store=SqlAlchemyOrderWriterStore(db, pinned_connection=bind),
        )
        self.__writer_created = True
        return writer

    def close(self) -> None:
        if self.__closed:
            return
        self.__closed = True
        self.__credentials.revoke("FACTORY_CLOSE")
        self.__process_lock.release()

    def __enter__(self) -> "OkxDemoServerSession":
        if self.__closed:
            raise OkxDemoWriteBlocked("OKX_DEMO server session is closed")
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


def create_okx_demo_server_session(
    environment: Mapping[str, str],
    *,
    lock_path: Path = _DEFAULT_LOCK_PATH,
) -> OkxDemoServerSession:
    """Attest once through #446, then share that private session with the writer."""

    process_lock = OkxDemoWriterProcessLock(lock_path)
    credentials = None
    stage = "writer-lock"
    try:
        process_lock.acquire()
        stage = "read-attestation"
        read = create_attested_okx_demo_read_adapter(environment)
        stage = "writer-credential-bridge"
        credentials = _create_attested_writer_credential_bridge(read)
        return OkxDemoServerSession(
            read=read,
            credentials=credentials,
            process_lock=process_lock,
            _capability=_SERVER_SESSION_CAPABILITY,
        )
    except OkxDemoWriteBlocked:
        if credentials is not None:
            credentials.revoke("FACTORY_FAILURE")
        process_lock.release()
        raise
    except (OkxDemoPreflightBlocked, OkxDemoCredentialsUnavailable) as exc:
        if credentials is not None:
            credentials.revoke("FACTORY_FAILURE")
        process_lock.release()
        raise _session_failure(stage=stage, exc=exc) from None
    except Exception as exc:
        if credentials is not None:
            credentials.revoke("FACTORY_FAILURE")
        process_lock.release()
        raise _session_failure(stage=stage, exc=exc) from None
