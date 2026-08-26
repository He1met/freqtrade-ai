from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.canonical_v13.continuous_demo_soak import ContinuousDemoSoakOperator
import app.canonical_v13.continuous_demo_soak as soak_module
from app.canonical_v13.continuous_demo_order_writer import (
    dispatch_continuous_demo_order,
    prepare_continuous_demo_order,
)
from app.canonical_v13.phase9_production_composition import CanonicalFillWriterOperator
from tests.test_canonical_v13_continuous_demo_order_writer import (
    FakeContinuousExitTransport,
    _continuous_exit_grant,
)
from tests.test_canonical_v13_phase9_execution_authority import (
    _production_chain,
    canonical_connection,  # noqa: F401, F811 - shared fixture
)
from tests.test_canonical_v13_phase9_order_writer import FakeFillSession, _factory
from tests.test_canonical_v13_research_evaluation import NOW


class ForbiddenSessionFactory:
    def __call__(self):
        raise AssertionError("private OKX session must not be opened while drained and flat")


def test_openings_disabled_and_flat_is_drained_without_private_okx(  # noqa: F811
    canonical_connection,  # noqa: F811
):
    with canonical_connection.begin():
        _production_chain(canonical_connection, create_intent=False)
    factory = _factory(canonical_connection.engine)
    operator = ContinuousDemoSoakOperator(
        reader_factory=factory,
        deployment_factory=factory,
        approval_factory=factory,
        risk_factory=factory,
        order_factory=factory,
        fill_factory=factory,
        ledger_factory=factory,
        reconciliation_factory=factory,
        session_factory=ForbiddenSessionFactory(),
        holder_token_digest="e" * 64,
    )

    result = operator.tick(openings_enabled=False, evaluated_at=NOW)

    assert result.status == "DRAINED"
    assert result.reason_code == "OPENINGS_DISABLED_AND_FLAT"
    assert result.allow_real_funds is False


def test_prepared_order_releases_lease_when_private_session_cannot_open(
    monkeypatch,
) -> None:
    @contextmanager
    def connection_factory():
        yield object()

    class FailedSessionFactory:
        def __call__(self):
            raise RuntimeError("private session unavailable")

    operator = ContinuousDemoSoakOperator(
        reader_factory=connection_factory,
        deployment_factory=connection_factory,
        approval_factory=connection_factory,
        risk_factory=connection_factory,
        order_factory=connection_factory,
        fill_factory=connection_factory,
        ledger_factory=connection_factory,
        reconciliation_factory=connection_factory,
        session_factory=FailedSessionFactory(),
        holder_token_digest="e" * 64,
    )
    decision_id = uuid4()
    attestation_id = uuid4()
    released = []
    monkeypatch.setattr(
        soak_module,
        "prepare_continuous_demo_order",
        lambda *_args, **_kwargs: SimpleNamespace(
            order_id=uuid4(), lease_generation=1
        ),
    )
    monkeypatch.setattr(operator, "_release_lease", lambda *, now: released.append(now))

    with pytest.raises(RuntimeError, match="private session unavailable"):
        operator._prepare_and_dispatch(
            decision_id,
            attestation_id,
            signal_id=uuid4(),
            now=NOW,
        )

    assert released == [NOW]


def test_partial_market_fill_waits_before_ledger_or_reconciliation(
    canonical_connection,  # noqa: F811
) -> None:
    with canonical_connection.begin():
        decision_id, attestation_id = _continuous_exit_grant(canonical_connection)
        prepared = prepare_continuous_demo_order(
            canonical_connection,
            risk_decision_id=decision_id,
            attestation_id=attestation_id,
            writer_identity="canonical_order_writer",
            holder_identity="canonical-v13-order-writer-v1",
            holder_token_digest="e" * 64,
            idempotency_key="continuous-partial-fill-fixture",
            evaluated_at=NOW + timedelta(seconds=2),
        )
    factory = _factory(canonical_connection.engine)
    dispatch_continuous_demo_order(
        factory,
        order_id=prepared.order_id,
        transport=FakeContinuousExitTransport(),
        holder_identity="canonical-v13-order-writer-v1",
        holder_token_digest="e" * 64,
        lease_generation=prepared.lease_generation,
        evaluated_at=NOW + timedelta(seconds=2),
    )
    def partial_session():
        return FakeFillSession(
            fills=(
                {
                    "fill_id": "partial-fill-1",
                    "bill_id": "partial-bill-1",
                    "order_id": "demo-exchange-order-1",
                    "inst_id": "BTC-USDT-SWAP",
                    "price": "10000",
                    "size": "0.4",
                    "fee": "-0.004",
                    "timestamp": "1787292000000",
                },
            )
        )
    CanonicalFillWriterOperator(
        connection_factory=factory,
        session_factory=partial_session,
    ).collect(order_id=prepared.order_id)
    operator = ContinuousDemoSoakOperator(
        reader_factory=factory,
        deployment_factory=factory,
        approval_factory=factory,
        risk_factory=factory,
        order_factory=factory,
        fill_factory=factory,
        ledger_factory=factory,
        reconciliation_factory=factory,
        session_factory=partial_session,
        holder_token_digest="e" * 64,
    )

    order, step = operator._execution_state()
    assert order is not None
    assert step == "FILL"
    result = operator._fill(order, now=NOW + timedelta(seconds=5))
    assert result.status == "WAITING"
    assert result.reason_code == "MARKET_FILL_PARTIAL"
