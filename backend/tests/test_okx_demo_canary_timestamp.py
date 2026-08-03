from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services.okx_demo_canary_preparation import (
    OkxDemoCanaryPreparationBlocked,
    OkxDemoCanaryPreparationService,
    _postgres_timestamptz,
)
from app.services.operator_authorization import OPERATOR_TOKEN_ENV


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value


class _ConsentSession:
    def __init__(self, consent_deadline_at):
        self.consent_deadline_at = consent_deadline_at
        self.events = []
        self.execute_count = 0

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    def execute(self, _statement, _parameters=None):
        self.events.append("execute")
        self.execute_count += 1
        if self.execute_count == 1:
            return _ScalarResult(
                {"eligibility_state": "PRISTINE", "predecessor": None}
            )
        return _ScalarResult(
            {
                "status": "CONSENT_CAPTURED",
                "handoff_id": "handoff-1",
                "source_job_id": 22,
                "consent_deadline_at": self.consent_deadline_at,
            }
        )

    def commit(self):
        self.events.append("commit")

    def rollback(self):
        self.events.append("rollback")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            datetime(2026, 8, 4, 12, 34, 56),
            datetime(2026, 8, 4, 12, 34, 56, tzinfo=timezone.utc),
        ),
        (
            datetime(
                2026,
                8,
                4,
                20,
                34,
                56,
                123456,
                tzinfo=timezone(timedelta(hours=8)),
            ),
            datetime(2026, 8, 4, 12, 34, 56, 123456, tzinfo=timezone.utc),
        ),
        (
            "2026-08-04T12:34:56Z",
            datetime(2026, 8, 4, 12, 34, 56, tzinfo=timezone.utc),
        ),
        (
            "2026-08-04 20:34:56.1+08:00",
            datetime(2026, 8, 4, 12, 34, 56, 100000, tzinfo=timezone.utc),
        ),
        (
            "2026-08-04T12:34:56.12+00",
            datetime(2026, 8, 4, 12, 34, 56, 120000, tzinfo=timezone.utc),
        ),
        (
            "2026-08-04T12:34:56.123+0000",
            datetime(2026, 8, 4, 12, 34, 56, 123000, tzinfo=timezone.utc),
        ),
        (
            "2026-08-04T12:34:56.1234+00:00",
            datetime(2026, 8, 4, 12, 34, 56, 123400, tzinfo=timezone.utc),
        ),
        (
            "2026-08-04T12:34:56.12345Z",
            datetime(2026, 8, 4, 12, 34, 56, 123450, tzinfo=timezone.utc),
        ),
        (
            "2026-08-04T12:34:56.123456+00:00",
            datetime(2026, 8, 4, 12, 34, 56, 123456, tzinfo=timezone.utc),
        ),
    ],
)
def test_postgres_timestamptz_normalizes_to_aware_utc(value, expected) -> None:
    assert _postgres_timestamptz(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "2026-08-04T12:34:56.1234567+00:00",
        "2026-08-04T12:34:56",
        "2026-08-04T12:34:56.12345",
        "2026-08-04T12:34:56+24:00",
        "not-a-timestamp",
        123,
    ],
)
def test_postgres_timestamptz_rejects_precision_loss_and_malformed_values(
    value,
) -> None:
    with pytest.raises(
        OkxDemoCanaryPreparationBlocked,
        match="PostgreSQL timestamptz is malformed",
    ):
        _postgres_timestamptz(value)


def test_consent_service_parses_five_digits_before_commit(monkeypatch) -> None:
    monkeypatch.setenv(OPERATOR_TOKEN_ENV, "operator-test-token")
    session = _ConsentSession("2026-08-04T12:34:56.12345+00:00")

    result = OkxDemoCanaryPreparationService(
        session
    ).request_final_attestation_consent(
        idempotency_key="timestamp-test",
        operator_token="operator-test-token",
    )

    assert result.consent_deadline_at == datetime(
        2026, 8, 4, 12, 34, 56, 123450, tzinfo=timezone.utc
    )
    assert session.events == ["execute", "execute", "commit"]


@pytest.mark.parametrize(
    "consent_deadline_at",
    [
        "2026-08-04T12:34:56.1234567+00:00",
        "not-a-timestamp",
    ],
)
def test_consent_service_rolls_back_malformed_timestamp_before_commit(
    monkeypatch,
    consent_deadline_at,
) -> None:
    monkeypatch.setenv(OPERATOR_TOKEN_ENV, "operator-test-token")
    session = _ConsentSession(consent_deadline_at)

    with pytest.raises(
        OkxDemoCanaryPreparationBlocked,
        match="PostgreSQL timestamptz is malformed",
    ):
        OkxDemoCanaryPreparationService(
            session
        ).request_final_attestation_consent(
            idempotency_key="timestamp-test",
            operator_token="operator-test-token",
        )

    assert session.events == ["execute", "execute", "rollback"]
