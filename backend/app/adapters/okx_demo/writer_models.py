from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal, Optional, Protocol

from pydantic import BaseModel, Field, ValidationError, field_serializer, model_validator

from app.adapters.okx_demo.models import InstrumentSpec
from app.adapters.okx_demo.write_semantics import (
    OkxDemoWriteBlocked,
    validate_client_order_id,
)


class ApprovedExecution(Protocol):
    """Structural view of the persisted #446 approved execution claim."""

    approval_id: int
    trade_intent_id: int
    risk_decision_id: int
    execution_target_id: str
    authorization_schema_version: str
    canonical_hash: str
    policy_digest: str
    approved_payload_hash: str
    client_order_id: str
    instrument_id: str
    side: str
    position_side: str
    order_type: str
    contracts: Decimal
    limit_price: Optional[Decimal]
    reduce_only: bool
    margin_mode: str
    leverage: Decimal
    approved_at: datetime
    expires_at: datetime
    policy_version: str
    idempotency_digest: str
    take_profit_trigger_price: Optional[Decimal]
    take_profit_order_price: Optional[Decimal]
    stop_loss_trigger_price: Optional[Decimal]
    stop_loss_order_price: Optional[Decimal]


class StrictModel(BaseModel):
    model_config = {"extra": "forbid", "frozen": True}


class OrderSubmissionAuthorization(StrictModel):
    execution_target_id: Literal["OKX_DEMO"]
    authorization_schema_version: Literal["RISK_V1"]
    canonical_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    allow_real_funds: Literal[False]
    simulated_trading: Literal[True]
    order_submission_enabled: Literal[True]
    writer_instance_id: str = Field(pattern=r"^[A-Za-z0-9]{8,64}$")
    approval_id: int = Field(gt=0)
    expires_at: datetime

    @model_validator(mode="after")
    def validate_expiry(self):
        if self.expires_at.tzinfo is None:
            raise ValueError("writer authorization grant expiry must be timezone-aware")
        return self

    def require_active(
        self,
        *,
        approval_id: int,
        canonical_hash: str,
        policy_digest: str,
        approved_payload_hash: str,
        now: datetime,
    ) -> None:
        if now.tzinfo is None:
            raise OkxDemoWriteBlocked(
                "writer authorization grant clock must be timezone-aware"
            )
        if self.approval_id != approval_id:
            raise OkxDemoWriteBlocked(
                "writer authorization grant is bound to a different approval"
            )
        if (
            self.canonical_hash != canonical_hash
            or self.policy_digest != policy_digest
            or self.approved_payload_hash != approved_payload_hash
        ):
            raise OkxDemoWriteBlocked(
                "writer authorization grant is bound to a different approved payload"
            )
        if now.astimezone(timezone.utc) >= self.expires_at.astimezone(timezone.utc):
            raise OkxDemoWriteBlocked("writer authorization grant is expired")


class ApprovedExecutionView(StrictModel):
    approval_id: int = Field(gt=0)
    trade_intent_id: int = Field(gt=0)
    risk_decision_id: int = Field(gt=0)
    execution_target_id: Literal["OKX_DEMO"]
    authorization_schema_version: Literal["RISK_V1"]
    canonical_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    client_order_id: str
    instrument_id: str = Field(pattern=r"^[A-Z0-9]+-[A-Z0-9]+-SWAP$")
    side: Literal["buy", "sell"]
    position_side: Literal["long", "short"]
    order_type: Literal["limit", "post_only", "market"]
    contracts: Decimal = Field(gt=0)
    limit_price: Optional[Decimal] = Field(default=None, gt=0)
    reduce_only: bool = False
    margin_mode: Literal["isolated"]
    leverage: Decimal = Field(gt=0)
    approved_at: datetime
    expires_at: datetime
    policy_version: str = Field(min_length=1, max_length=80)
    idempotency_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    take_profit_trigger_price: Optional[Decimal] = Field(default=None, gt=0)
    take_profit_order_price: Optional[Decimal] = None
    stop_loss_trigger_price: Optional[Decimal] = Field(default=None, gt=0)
    stop_loss_order_price: Optional[Decimal] = None

    @model_validator(mode="after")
    def validate_order_shape(self):
        validate_client_order_id(self.client_order_id)
        if self.approved_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("approval timestamps must be timezone-aware")
        if self.expires_at <= self.approved_at:
            raise ValueError("approval expiry must follow approval time")
        if self.order_type == "market" and self.limit_price is not None:
            raise ValueError("market order must not contain limit_price")
        if self.order_type != "market" and self.limit_price is None:
            raise ValueError("limit/post-only order requires limit_price")
        if self.reduce_only and (
            self.order_type != "market"
            or self.take_profit_trigger_price is not None
            or self.stop_loss_trigger_price is not None
        ):
            raise ValueError(
                "reduce-only close must be market without attached exits"
            )
        expected_position_side = (
            "long"
            if (self.side == "buy") is not self.reduce_only
            else "short"
        )
        if self.position_side != expected_position_side:
            raise ValueError(
                "side, position_side and reduce_only do not describe one OKX long/short action"
            )
        for trigger, order_price, label in (
            (
                self.take_profit_trigger_price,
                self.take_profit_order_price,
                "take profit",
            ),
            (
                self.stop_loss_trigger_price,
                self.stop_loss_order_price,
                "stop loss",
            ),
        ):
            if trigger is None and order_price is not None:
                raise ValueError("{} order price requires trigger price".format(label))
            if order_price is not None and order_price != Decimal("-1") and order_price <= 0:
                raise ValueError(
                    "{} order price must be positive or -1".format(label)
                )
        return self


class NormalizedOrderCommand(StrictModel):
    execution_target_id: Literal["OKX_DEMO"] = "OKX_DEMO"
    trade_intent_id: int
    risk_decision_id: int
    approval_id: int
    authorization_schema_version: Literal["RISK_V1"]
    canonical_hash: str
    policy_digest: str
    approved_payload_hash: str
    client_order_id: str
    instrument_id: str
    side: Literal["buy", "sell"]
    position_side: Literal["long", "short"]
    order_type: Literal["limit", "post_only", "market"]
    contracts: Decimal
    limit_price: Optional[Decimal]
    reduce_only: bool
    margin_mode: Literal["isolated"]
    leverage: Decimal
    contract_value: Decimal
    normalized_units: Decimal
    request_body: dict[str, Any]

    @field_serializer(
        "contracts",
        "limit_price",
        "contract_value",
        "normalized_units",
        "leverage",
    )
    def serialize_decimal(self, value):
        return None if value is None else format(value, "f")


def approved_execution_view(value: ApprovedExecution) -> ApprovedExecutionView:
    fields = ApprovedExecutionView.model_fields
    try:
        payload = {name: getattr(value, name) for name in fields}
    except AttributeError:
        raise OkxDemoWriteBlocked(
            "approved execution does not satisfy the #446 adapter contract"
        ) from None
    try:
        return ApprovedExecutionView.model_validate(payload)
    except (ValidationError, ValueError):
        raise OkxDemoWriteBlocked(
            "approved execution does not satisfy the #446 adapter contract"
        ) from None


class ClaimedApprovedExecution(ApprovedExecutionView):
    """Read-only #446 lineage projection; the DB claim occurs on PREPARED."""


def normalize_order_command(
    approved: ApprovedExecutionView,
    *,
    submission_grant: OrderSubmissionAuthorization,
    instrument: InstrumentSpec,
    now: datetime,
) -> NormalizedOrderCommand:
    submission_grant.require_active(
        approval_id=approved.approval_id,
        canonical_hash=approved.canonical_hash,
        policy_digest=approved.policy_digest,
        approved_payload_hash=approved.approved_payload_hash,
        now=now,
    )
    if now.tzinfo is None:
        raise OkxDemoWriteBlocked("writer clock must be timezone-aware")
    if now.astimezone(timezone.utc) >= approved.expires_at.astimezone(timezone.utc):
        raise OkxDemoWriteBlocked("approved execution is expired")
    if instrument.inst_id != approved.instrument_id:
        raise OkxDemoWriteBlocked("instrument evidence does not match approval")
    if instrument.inst_type != "SWAP" or instrument.state != "live":
        raise OkxDemoWriteBlocked("approved instrument is not a live SWAP")
    if (
        approved.contracts < instrument.min_size
        or approved.contracts % instrument.lot_size != 0
    ):
        raise OkxDemoWriteBlocked(
            "approved contracts violate OKX minSz/lotSz precision"
        )
    prices = [
        approved.limit_price,
        approved.take_profit_trigger_price,
        approved.take_profit_order_price,
        approved.stop_loss_trigger_price,
        approved.stop_loss_order_price,
    ]
    for price in prices:
        if price is not None and price != Decimal("-1") and price % instrument.tick_size != 0:
            raise OkxDemoWriteBlocked("approved price violates OKX tickSz precision")

    body: dict[str, Any] = {
        "instId": approved.instrument_id,
        "tdMode": "isolated",
        "side": approved.side,
        "posSide": approved.position_side,
        "ordType": approved.order_type,
        "sz": _decimal_text(approved.contracts),
        "clOrdId": approved.client_order_id,
    }
    if approved.limit_price is not None:
        body["px"] = _decimal_text(approved.limit_price)
    if approved.reduce_only:
        body["reduceOnly"] = True
    attached = _attached_exit_bodies(approved)
    if attached:
        body["attachAlgoOrds"] = attached
    return NormalizedOrderCommand(
        trade_intent_id=approved.trade_intent_id,
        risk_decision_id=approved.risk_decision_id,
        approval_id=approved.approval_id,
        authorization_schema_version="RISK_V1",
        canonical_hash=approved.canonical_hash,
        policy_digest=approved.policy_digest,
        approved_payload_hash=approved.approved_payload_hash,
        client_order_id=approved.client_order_id,
        instrument_id=approved.instrument_id,
        side=approved.side,
        position_side=approved.position_side,
        order_type=approved.order_type,
        contracts=approved.contracts,
        limit_price=approved.limit_price,
        reduce_only=approved.reduce_only,
        margin_mode=approved.margin_mode,
        leverage=approved.leverage,
        contract_value=instrument.contract_value,
        normalized_units=approved.contracts * instrument.contract_value,
        request_body=body,
    )


def _attached_exit_bodies(approved: ApprovedExecutionView) -> list[dict[str, Any]]:
    attached: dict[str, Any] = {}
    if approved.take_profit_trigger_price is not None:
        attached.update(
            {
                "attachAlgoClOrdId": _derived_child_id(
                    approved.client_order_id,
                    "TP",
                ),
                "tpTriggerPx": _decimal_text(approved.take_profit_trigger_price),
                "tpOrdPx": _decimal_text(
                    approved.take_profit_order_price or Decimal("-1")
                ),
                "tpTriggerPxType": "mark",
            }
        )
    if approved.stop_loss_trigger_price is not None:
        attached.setdefault(
            "attachAlgoClOrdId",
            _derived_child_id(approved.client_order_id, "EX"),
        )
        attached.update(
            {
                "slTriggerPx": _decimal_text(approved.stop_loss_trigger_price),
                "slOrdPx": _decimal_text(
                    approved.stop_loss_order_price or Decimal("-1")
                ),
                "slTriggerPxType": "mark",
            }
        )
    return [attached] if attached else []


def _derived_child_id(parent: str, suffix: str) -> str:
    return validate_client_order_id((parent[: 32 - len(suffix)] + suffix))


def _decimal_text(value: Decimal) -> str:
    rendered = format(value, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered
