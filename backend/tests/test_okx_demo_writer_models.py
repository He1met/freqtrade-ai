from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.adapters.okx_demo.models import InstrumentSpec
from app.adapters.okx_demo.write_semantics import OkxDemoWriteBlocked
from app.adapters.okx_demo.writer_models import (
    ApprovedExecutionView,
    OrderSubmissionAuthorization,
    approved_execution_view,
    normalize_order_command,
)
from app.adapters.okx_demo.writer_state import (
    WriteEvent,
    WriteState,
    transition_write_state,
)


NOW = datetime(2026, 7, 27, tzinfo=timezone.utc)


def instrument(**overrides):
    values = {
        "inst_id": "BTC-USDT-SWAP",
        "inst_type": "SWAP",
        "base_ccy": "BTC",
        "quote_ccy": "USDT",
        "settle_ccy": "USDT",
        "contract_type": "linear",
        "contract_value": Decimal("0.01"),
        "contract_value_ccy": "BTC",
        "lot_size": Decimal("0.01"),
        "min_size": Decimal("0.01"),
        "tick_size": Decimal("0.1"),
        "state": "live",
    }
    values.update(overrides)
    return InstrumentSpec(**values)


def approved(**overrides):
    values = {
        "approval_id": 3,
        "trade_intent_id": 7,
        "risk_decision_id": 9,
        "execution_target_id": "OKX_DEMO",
        "authorization_schema_version": "RISK_V1",
        "canonical_hash": "b" * 64,
        "policy_digest": "c" * 64,
        "approved_payload_hash": "d" * 64,
        "client_order_id": "WriterOrder001",
        "instrument_id": "BTC-USDT-SWAP",
        "side": "buy",
        "position_side": "long",
        "order_type": "limit",
        "contracts": Decimal("0.02"),
        "limit_price": Decimal("57000.1"),
        "reduce_only": False,
        "margin_mode": "isolated",
        "leverage": Decimal("3"),
        "approved_at": NOW - timedelta(seconds=10),
        "expires_at": NOW + timedelta(seconds=50),
        "policy_version": "risk-v1",
        "idempotency_digest": "a" * 64,
        "take_profit_trigger_price": None,
        "take_profit_order_price": None,
        "stop_loss_trigger_price": None,
        "stop_loss_order_price": None,
    }
    values.update(overrides)
    return ApprovedExecutionView(**values)


def submission_grant(**overrides):
    values = {
        "execution_target_id": "OKX_DEMO",
        "authorization_schema_version": "RISK_V1",
        "canonical_hash": "b" * 64,
        "policy_digest": "c" * 64,
        "approved_payload_hash": "d" * 64,
        "allow_real_funds": False,
        "simulated_trading": True,
        "order_submission_enabled": True,
        "writer_instance_id": "WriterInstance01",
        "approval_id": 3,
        "expires_at": NOW + timedelta(seconds=30),
    }
    values.update(overrides)
    return OrderSubmissionAuthorization(**values)


def test_normalize_limit_with_attached_take_profit_and_stop_loss() -> None:
    execution = approved(
        take_profit_trigger_price=Decimal("60000.0"),
        take_profit_order_price=Decimal("-1"),
        stop_loss_trigger_price=Decimal("55000.0"),
        stop_loss_order_price=Decimal("-1"),
    )

    command = normalize_order_command(
        execution,
        submission_grant=submission_grant(),
        instrument=instrument(),
        now=NOW,
    )

    assert command.normalized_units == Decimal("0.0002")
    assert command.request_body == {
        "instId": "BTC-USDT-SWAP",
        "tdMode": "isolated",
        "side": "buy",
        "posSide": "long",
        "ordType": "limit",
        "sz": "0.02",
        "clOrdId": "WriterOrder001",
        "px": "57000.1",
        "attachAlgoOrds": [
            {
                "attachAlgoClOrdId": "WriterOrder001TP",
                "tpTriggerPx": "60000",
                "tpOrdPx": "-1",
                "tpTriggerPxType": "mark",
                "slTriggerPx": "55000",
                "slOrdPx": "-1",
                "slTriggerPxType": "mark",
            }
        ],
    }


def test_market_reduce_only_close_shape_has_no_price() -> None:
    command = normalize_order_command(
        approved(
            side="sell",
            order_type="market",
            limit_price=None,
            reduce_only=True,
        ),
        submission_grant=submission_grant(),
        instrument=instrument(),
        now=NOW,
    )

    assert command.request_body["ordType"] == "market"
    assert command.request_body["reduceOnly"] is True
    assert "px" not in command.request_body


@pytest.mark.parametrize(
    ("side", "position_side", "reduce_only"),
    [
        ("buy", "short", False),
        ("sell", "long", False),
        ("buy", "long", True),
        ("sell", "short", True),
    ],
)
def test_long_short_order_direction_rejects_ambiguous_position_side(
    side, position_side, reduce_only
) -> None:
    with pytest.raises(ValidationError, match="long/short action"):
        approved(
            side=side,
            position_side=position_side,
            reduce_only=reduce_only,
            order_type="market" if reduce_only else "limit",
            limit_price=None if reduce_only else Decimal("57000.1"),
        )


def test_reduce_only_close_rejects_limit_or_attached_exit_shape() -> None:
    with pytest.raises(ValidationError, match="reduce-only close"):
        approved(reduce_only=True)
    with pytest.raises(ValidationError, match="reduce-only close"):
        approved(
            reduce_only=True,
            order_type="market",
            limit_price=None,
            stop_loss_trigger_price=Decimal("55000.0"),
        )


@pytest.mark.parametrize(
    ("execution", "spec", "reason"),
    [
        (approved(contracts=Decimal("0.001")), instrument(), "minSz/lotSz"),
        (approved(contracts=Decimal("0.015")), instrument(), "minSz/lotSz"),
        (
            approved(limit_price=Decimal("57000.15")),
            instrument(),
            "tickSz",
        ),
        (approved(), instrument(state="suspend"), "live SWAP"),
        (
            approved(instrument_id="ETH-USDT-SWAP"),
            instrument(),
            "does not match",
        ),
    ],
)
def test_precision_and_instrument_mismatch_fail_closed(execution, spec, reason) -> None:
    with pytest.raises(OkxDemoWriteBlocked, match=reason):
        normalize_order_command(
            execution,
            submission_grant=submission_grant(),
            instrument=spec,
            now=NOW,
        )


def test_authorization_is_explicit_approval_bound_and_expiring() -> None:
    with pytest.raises(ValidationError):
        submission_grant(order_submission_enabled=False)
    with pytest.raises(ValidationError):
        submission_grant(execution_target_id="OKX_LIVE")
    with pytest.raises(ValidationError, match="timezone-aware"):
        submission_grant(expires_at=datetime(2026, 7, 27))
    with pytest.raises(OkxDemoWriteBlocked, match="different approval"):
        normalize_order_command(
            approved(),
            submission_grant=submission_grant(approval_id=99),
            instrument=instrument(),
            now=NOW,
        )
    with pytest.raises(OkxDemoWriteBlocked, match="expired"):
        normalize_order_command(
            approved(),
            submission_grant=submission_grant(expires_at=NOW),
            instrument=instrument(),
            now=NOW,
        )


def test_structural_approved_execution_adapter_does_not_define_risk_model() -> None:
    raw = SimpleNamespace(
        **approved().model_dump(),
    )

    view = approved_execution_view(raw)

    assert view.trade_intent_id == 7
    with pytest.raises(OkxDemoWriteBlocked, match="#446"):
        approved_execution_view(SimpleNamespace(approval_id=3))


def test_write_state_machine_keeps_recovery_sticky() -> None:
    assert (
        transition_write_state(WriteState.PREPARED, WriteEvent.OUTCOME_UNKNOWN)
        == WriteState.RECOVERY_REQUIRED
    )
    assert (
        transition_write_state(
            WriteState.RECOVERY_REQUIRED,
            WriteEvent.RECOVERY_STILL_UNKNOWN,
        )
        == WriteState.RECOVERY_REQUIRED
    )
    assert (
        transition_write_state(
            WriteState.RECOVERY_REQUIRED,
            WriteEvent.RECONCILE,
        )
        == WriteState.RECONCILED
    )
    with pytest.raises(OkxDemoWriteBlocked, match="invalid"):
        transition_write_state(WriteState.REJECTED, WriteEvent.ACKNOWLEDGE)
