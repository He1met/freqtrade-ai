from threading import Event, Thread

import pytest
from fastapi import HTTPException

from app.services.operator_authorization import (
    OPERATOR_TOKEN_ENV,
    OperatorRequestCoordinator,
    OperatorRequestHeaders,
)


def headers(key: str, *, token: str = "synthetic-test-operator-token", provider: bool = False):
    return OperatorRequestHeaders(
        operator_token=token,
        idempotency_key=key,
        provider_authorization="once" if provider else None,
    )


def test_replays_completed_idempotent_request_without_running_handler_twice() -> None:
    coordinator = OperatorRequestCoordinator()
    calls = []

    def handler():
        calls.append("called")
        return {"status": "SUCCESS", "database_ids": {"run_id": 42}}

    first = coordinator.execute(
        headers("same-request-key"),
        operation="test.write",
        provider_call=False,
        request_payload={"value": 1},
        handler=handler,
    )
    replay = coordinator.execute(
        headers("same-request-key"),
        operation="test.write",
        provider_call=False,
        request_payload={"value": 1},
        handler=handler,
    )

    assert replay == first
    assert calls == ["called"]


def test_rejects_idempotency_key_reuse_with_different_payload() -> None:
    coordinator = OperatorRequestCoordinator()
    coordinator.execute(
        headers("payload-bound-key"),
        operation="test.write",
        provider_call=False,
        request_payload={"value": 1},
        handler=lambda: {"status": "SUCCESS"},
    )

    with pytest.raises(HTTPException) as exc_info:
        coordinator.execute(
            headers("payload-bound-key"),
            operation="test.write",
            provider_call=False,
            request_payload={"value": 2},
            handler=lambda: {"status": "SUCCESS"},
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["operation_status"] == "BLOCKED"


def test_rejects_unauthorized_request_without_running_handler() -> None:
    coordinator = OperatorRequestCoordinator()
    called = False

    def handler():
        nonlocal called
        called = True

    with pytest.raises(HTTPException) as exc_info:
        coordinator.execute(
            headers("unauthorized-key", token="wrong-synthetic-token"),
            operation="test.write",
            provider_call=False,
            request_payload={"value": 1},
            handler=handler,
        )

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["operation_status"] == "UNAUTHORIZED"
    assert called is False
    assert "wrong-synthetic-token" not in str(exc_info.value.detail)


def test_blocks_when_operator_token_is_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(OPERATOR_TOKEN_ENV, raising=False)
    coordinator = OperatorRequestCoordinator()

    with pytest.raises(HTTPException) as exc_info:
        coordinator.execute(
            headers("missing-config-key"),
            operation="test.write",
            provider_call=False,
            request_payload={"value": 1},
            handler=lambda: {"status": "SUCCESS"},
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["operation_status"] == "BLOCKED"


def test_real_provider_requires_one_time_authorization() -> None:
    coordinator = OperatorRequestCoordinator()

    with pytest.raises(HTTPException) as exc_info:
        coordinator.execute(
            headers("provider-auth-key"),
            operation="test.provider",
            provider_call=True,
            request_payload={"value": 1},
            handler=lambda: None,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["operation_status"] == "BLOCKED"


def test_allows_only_one_concurrent_real_provider_request() -> None:
    coordinator = OperatorRequestCoordinator()
    entered = Event()
    release = Event()

    def slow_handler():
        entered.set()
        release.wait(timeout=5)
        return {"status": "SUCCESS"}

    thread = Thread(
        target=lambda: coordinator.execute(
            headers("provider-first-key", provider=True),
            operation="test.provider.first",
            provider_call=True,
            request_payload={"value": 1},
            handler=slow_handler,
        )
    )
    thread.start()
    assert entered.wait(timeout=5)

    try:
        with pytest.raises(HTTPException) as exc_info:
            coordinator.execute(
                headers("provider-second-key", provider=True),
                operation="test.provider.second",
                provider_call=True,
                request_payload={"value": 2},
                handler=lambda: {"status": "SUCCESS"},
            )
        assert exc_info.value.status_code == 409
        assert exc_info.value.detail["operation_status"] == "BLOCKED"
    finally:
        release.set()
        thread.join(timeout=5)

    assert thread.is_alive() is False
