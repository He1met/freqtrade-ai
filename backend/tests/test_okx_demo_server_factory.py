from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.adapters.okx_demo import credential_preflight as preflight
from app.adapters.okx_demo import server_factory
from app.adapters.okx_demo.credentials import OkxDemoCredentialsUnavailable
from app.adapters.okx_demo.read_adapter import _AttestedWriterCredentialHandle
from app.adapters.okx_demo.write_semantics import OkxDemoWriteBlocked
from app.adapters.okx_demo.write_transport import UrllibOkxDemoWriteTransport


NOW = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)


def account() -> dict:
    return {
        "uid": "demo-account",
        "mainUid": "demo-main",
        "acctLv": "2",
        "posMode": "net_mode",
        "perm": "read_only,trade",
    }


def environment() -> dict:
    return {
        preflight.EXECUTION_TARGET_ENV: "OKX_DEMO",
        preflight.ALLOW_REAL_FUNDS_ENV: "false",
        preflight.REST_URL_ENV: preflight.OKX_DEMO_REST_URL,
        "OKX_DEMO_API_KEY": "ephemeral-api-key",
        "OKX_DEMO_API_SECRET": "ephemeral-api-secret",
        "OKX_DEMO_API_PASSPHRASE": "ephemeral-passphrase",
        preflight.OKX_DEMO_ACCOUNT_FINGERPRINT_ENV: preflight.account_fingerprint(
            account()
        ),
    }


class FakeProcessLock:
    events = []

    def __init__(self, path: Path) -> None:
        self.path = path
        self.held = False

    def acquire(self) -> None:
        self.events.append(("acquire", self.path))
        self.held = True

    def release(self) -> None:
        if self.held:
            self.events.append(("release", self.path))
            self.held = False


class WriteResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return b'{"code":"0","data":[{"ordId":"1","clOrdId":"client-1"}]}'


class WriteOpener:
    def __init__(self) -> None:
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        return WriteResponse()


class FakeCredentialProvider:
    def __init__(self) -> None:
        self.revoked = False
        self.bound_bind = None

    def bind_database(self, db) -> None:
        self.bound_bind = db.get_bind()

    def revoke(self, _reason: str) -> None:
        self.revoked = True

    def authorization_headers(self, **_kwargs):
        if self.revoked:
            raise OkxDemoCredentialsUnavailable("revoked")
        return {
            "OK-ACCESS-KEY": "ephemeral-api-key",
            "OK-ACCESS-SIGN": "signature",
            "OK-ACCESS-TIMESTAMP": "timestamp",
            "OK-ACCESS-PASSPHRASE": "ephemeral-passphrase",
        }


def fake_attested_read_client(
    provider: FakeCredentialProvider,
) -> object:
    return SimpleNamespace(
        _engine=SimpleNamespace(_credential_provider=provider),
        _writer_credential_handle=(
            _AttestedWriterCredentialHandle._from_attested_session(provider)
        ),
    )


def install_offline_production_boundary(monkeypatch):
    calls = []
    providers = []
    write_opener = WriteOpener()
    FakeProcessLock.events = []

    def attest(snapshot):
        calls.append(dict(snapshot))
        assert snapshot[preflight.EXECUTION_TARGET_ENV] == "OKX_DEMO"
        assert snapshot[preflight.ALLOW_REAL_FUNDS_ENV] == "false"
        assert snapshot[preflight.REST_URL_ENV] == preflight.OKX_DEMO_REST_URL
        provider = FakeCredentialProvider()
        providers.append(provider)
        return fake_attested_read_client(provider)

    monkeypatch.setattr(
        server_factory,
        "create_attested_okx_demo_read_adapter",
        attest,
    )
    monkeypatch.setattr(
        server_factory,
        "OkxDemoWriterProcessLock",
        FakeProcessLock,
    )
    monkeypatch.setattr(
        "app.adapters.okx_demo.write_transport.build_direct_no_redirect_opener",
        lambda: write_opener,
    )
    return calls, write_opener, providers


def test_server_factory_attests_once_then_builds_one_read_write_session(
    monkeypatch,
) -> None:
    attestations, write_opener, providers = install_offline_production_boundary(
        monkeypatch
    )
    active_environment = environment()

    session = server_factory.create_okx_demo_server_session(active_environment)
    active_environment["OKX_DEMO_API_SECRET"] = "mutated-after-attestation"
    engine = create_engine("sqlite+pysqlite:///:memory:")
    connection = engine.connect()
    db = Session(bind=connection)
    monkeypatch.setattr(
        server_factory,
        "verify_connection_schema",
        lambda _connection: SimpleNamespace(ready=True),
    )
    writer = session.create_order_writer(db)
    assert db.in_transaction() is False
    assert providers[0].bound_bind is engine
    assert len(attestations) == 1
    assert not hasattr(session, "write")
    assert not hasattr(writer, "_write")
    assert not hasattr(writer, "_OkxDemoOrderWriter__offline_write_transport") or (
        writer._OkxDemoOrderWriter__offline_write_transport is None
    )
    assert "_WRITE_TRANSPORTS" not in vars(
        __import__(
            "app.adapters.okx_demo.order_writer",
            fromlist=["_WRITE_TRANSPORTS"],
        )
    )
    assert not any(
        hasattr(value, "post")
        for value in vars(writer).values()
        if value is not None
    )
    assert write_opener.calls == []
    assert [event[0] for event in FakeProcessLock.events] == ["acquire"]
    with pytest.raises(OkxDemoWriteBlocked, match="already owns"):
        session.create_order_writer(db)

    session.close()
    assert [event[0] for event in FakeProcessLock.events] == [
        "acquire",
        "release",
    ]
    with pytest.raises(OkxDemoWriteBlocked, match="closed"):
        session.create_order_writer(db)
    assert len(write_opener.calls) == 0
    db.close()
    connection.close()


def test_production_writer_rejects_sqlite_or_unready_schema(monkeypatch) -> None:
    install_offline_production_boundary(monkeypatch)
    session = server_factory.create_okx_demo_server_session(environment())
    engine = create_engine("sqlite+pysqlite:///:memory:")
    connection = engine.connect()
    db = Session(bind=connection)

    with pytest.raises(OkxDemoWriteBlocked, match="PostgreSQL schema"):
        session.create_order_writer(db)

    assert not hasattr(session, "write")
    session.close()
    db.close()
    connection.close()


def test_production_writer_rejects_engine_bound_session(monkeypatch) -> None:
    install_offline_production_boundary(monkeypatch)
    session = server_factory.create_okx_demo_server_session(environment())
    db = Session(create_engine("sqlite+pysqlite:///:memory:"))

    with pytest.raises(OkxDemoWriteBlocked, match="pinned database connection"):
        session.create_order_writer(db)

    session.close()
    db.close()


def test_production_writer_rejects_dirty_pinned_session(monkeypatch) -> None:
    install_offline_production_boundary(monkeypatch)
    server_session = server_factory.create_okx_demo_server_session(environment())
    engine = create_engine("sqlite+pysqlite:///:memory:")
    connection = engine.connect()
    db = Session(bind=connection)
    db.begin()

    with pytest.raises(OkxDemoWriteBlocked, match="clean database session"):
        server_session.create_order_writer(db)

    assert db.in_transaction() is True
    db.rollback()
    server_session.close()
    db.close()
    connection.close()


def test_process_lock_is_acquired_before_preflight_and_released_on_failure(
    monkeypatch,
) -> None:
    FakeProcessLock.events = []
    monkeypatch.setattr(
        server_factory,
        "OkxDemoWriterProcessLock",
        FakeProcessLock,
    )

    def blocked(_snapshot):
        assert [event[0] for event in FakeProcessLock.events] == ["acquire"]
        raise OkxDemoCredentialsUnavailable("blocked")

    monkeypatch.setattr(
        server_factory,
        "create_attested_okx_demo_read_adapter",
        blocked,
    )

    with pytest.raises(
        server_factory.OkxDemoServerSessionBlocked,
        match=(
            r"stage=read-attestation, category=ATTESTATION, "
            r"cause_type=OkxDemoCredentialsUnavailable"
        ),
    ) as captured:
        server_factory.create_okx_demo_server_session(environment())

    assert captured.value.stage == "read-attestation"
    assert captured.value.category == "ATTESTATION"
    assert [event[0] for event in FakeProcessLock.events] == [
        "acquire",
        "release",
    ]


def test_safe_preflight_reason_survives_factory_boundary(monkeypatch) -> None:
    FakeProcessLock.events = []
    monkeypatch.setattr(
        server_factory,
        "OkxDemoWriterProcessLock",
        FakeProcessLock,
    )
    monkeypatch.setattr(
        server_factory,
        "create_attested_okx_demo_read_adapter",
        lambda _snapshot: (_ for _ in ()).throw(
            preflight.OkxDemoPreflightBlocked(
                preflight.IP_WHITELIST_REJECTED_REASON
            )
        ),
    )

    with pytest.raises(server_factory.OkxDemoServerSessionBlocked) as captured:
        server_factory.create_okx_demo_server_session(environment())

    assert captured.value.stage == "read-attestation"
    assert captured.value.category == "PREFLIGHT"
    assert captured.value.cause_type == "OkxDemoPreflightBlocked"
    assert preflight.IP_WHITELIST_REJECTED_REASON not in str(captured.value)
    assert [event[0] for event in FakeProcessLock.events] == [
        "acquire",
        "release",
    ]


def test_server_session_and_production_transport_are_factory_gated() -> None:
    class ForgedProvider:
        def _assert_attested_session(self, _capability):
            return False

        def authorization_headers(self, **_kwargs):
            return {
                "OK-ACCESS-KEY": "key",
                "OK-ACCESS-SIGN": "signature",
                "OK-ACCESS-TIMESTAMP": "timestamp",
                "OK-ACCESS-PASSPHRASE": "passphrase",
            }

    with pytest.raises(OkxDemoWriteBlocked, match="server factory"):
        UrllibOkxDemoWriteTransport(
            ForgedProvider(),
            _capability=object(),
        )
    with pytest.raises(OkxDemoWriteBlocked, match="production factory"):
        server_factory.OkxDemoServerSession(
            read=object(),
            credentials=object(),
            process_lock=object(),
            _capability=object(),
        )


def test_factory_failure_never_renders_or_serializes_credentials(
    monkeypatch,
) -> None:
    FakeProcessLock.events = []
    monkeypatch.setattr(
        server_factory,
        "OkxDemoWriterProcessLock",
        FakeProcessLock,
    )
    monkeypatch.setattr(
        server_factory,
        "create_attested_okx_demo_read_adapter",
        lambda _snapshot: (_ for _ in ()).throw(
            OkxDemoCredentialsUnavailable("unsafe")
        ),
    )

    with pytest.raises(OkxDemoCredentialsUnavailable) as captured:
        server_factory.create_okx_demo_server_session(environment())

    rendered = json.dumps({"error": str(captured.value)})
    assert "ephemeral-api-key" not in rendered
    assert "ephemeral-api-secret" not in rendered
    assert "ephemeral-passphrase" not in rendered
    assert "unsafe" not in rendered


def test_factory_unknown_failure_preserves_safe_stage_and_type_only(
    monkeypatch,
) -> None:
    FakeProcessLock.events = []
    monkeypatch.setattr(
        server_factory,
        "OkxDemoWriterProcessLock",
        FakeProcessLock,
    )

    class SensitiveFailure(RuntimeError):
        pass

    monkeypatch.setattr(
        server_factory,
        "create_attested_okx_demo_read_adapter",
        lambda _snapshot: (_ for _ in ()).throw(
            SensitiveFailure(
                "api-key=secret signature=private "
                "postgresql://operator:password@localhost/db"
            )
        ),
    )

    with pytest.raises(server_factory.OkxDemoServerSessionBlocked) as captured:
        server_factory.create_okx_demo_server_session(environment())

    assert captured.value.stage == "read-attestation"
    assert captured.value.category == "UNEXPECTED"
    assert captured.value.cause_type == "UnexpectedError"
    rendered = str(captured.value)
    assert "secret" not in rendered
    assert "signature" not in rendered
    assert "password" not in rendered
    assert "SensitiveFailure" not in rendered
