from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_serializer


class StableModel(BaseModel):
    model_config = {"extra": "forbid"}


class SnapshotMetadata(StableModel):
    execution_target: Literal["OKX_DEMO"] = "OKX_DEMO"
    source: Literal["okx_demo_rest"] = "okx_demo_rest"
    resource: str = Field(min_length=1, max_length=64)
    fetched_at: datetime
    exchange_timestamp: Optional[datetime] = None
    expires_at: datetime
    stale: bool
    authenticated: bool


class OkxReadSnapshot(StableModel):
    schema_version: Literal["1"] = "1"
    status: Literal["READY"] = "READY"
    metadata: SnapshotMetadata
    items: list[dict[str, Any]]


class ContractConversion(StableModel):
    requested_units: Decimal
    contracts: Decimal
    normalized_units: Decimal
    unit: str
    lot_size: Decimal
    min_size: Decimal

    @field_serializer(
        "requested_units",
        "contracts",
        "normalized_units",
        "lot_size",
        "min_size",
    )
    def serialize_decimal(self, value: Decimal) -> str:
        return format(value, "f")


class InstrumentSpec(StableModel):
    inst_id: str
    inst_type: Literal["SWAP"]
    base_ccy: str
    quote_ccy: str
    settle_ccy: str
    contract_type: str
    contract_value: Decimal = Field(gt=0)
    contract_value_ccy: str
    lot_size: Decimal = Field(gt=0)
    min_size: Decimal = Field(gt=0)
    tick_size: Decimal = Field(gt=0)
    state: str
    listed_at: Optional[datetime] = None

    @field_serializer("contract_value", "lot_size", "min_size", "tick_size")
    def serialize_decimal(self, value: Decimal) -> str:
        return format(value, "f")

    def contracts_to_units(self, contracts: Decimal) -> Decimal:
        self._validate_contract_count(contracts)
        return contracts * self.contract_value

    def units_to_contracts(self, units: Decimal) -> ContractConversion:
        if units <= 0:
            raise ValueError("requested units must be positive")
        raw_contracts = units / self.contract_value
        lots = (raw_contracts / self.lot_size).to_integral_value(rounding=ROUND_DOWN)
        contracts = lots * self.lot_size
        if contracts < self.min_size:
            raise ValueError("requested units are below the instrument minimum size")
        return ContractConversion(
            requested_units=units,
            contracts=contracts,
            normalized_units=contracts * self.contract_value,
            unit=self.contract_value_ccy,
            lot_size=self.lot_size,
            min_size=self.min_size,
        )

    def _validate_contract_count(self, contracts: Decimal) -> None:
        if contracts < self.min_size:
            raise ValueError("contract count is below min_size")
        if contracts % self.lot_size != 0:
            raise ValueError("contract count is not aligned to lot_size")


class Ticker(StableModel):
    inst_id: str
    last: Decimal
    bid: Decimal
    ask: Decimal
    open_24h: Decimal
    high_24h: Decimal
    low_24h: Decimal
    volume_24h: Decimal
    volume_ccy_24h: Decimal
    timestamp: datetime


class Candle(StableModel):
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    volume_ccy: Decimal
    confirmed: bool


class OrderBookLevel(StableModel):
    price: Decimal
    size: Decimal
    orders: int = Field(ge=0)


class OrderBook(StableModel):
    inst_id: str
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]
    timestamp: datetime


class ReferencePrice(StableModel):
    inst_id: str
    price_kind: Literal["mark", "index"]
    price: Decimal
    timestamp: datetime


class FundingRate(StableModel):
    inst_id: str
    funding_rate: Decimal
    next_funding_rate: Optional[Decimal] = None
    funding_time: datetime
    next_funding_time: Optional[datetime] = None


class OpenInterest(StableModel):
    inst_id: str
    open_interest_contracts: Decimal
    open_interest_ccy: Optional[Decimal] = None
    timestamp: datetime


class AccountConfig(StableModel):
    account_level: str
    position_mode: str
    auto_loan: bool
    greeks_type: str


class Balance(StableModel):
    currency: str
    total_equity: Optional[Decimal] = None
    available_balance: Optional[Decimal] = None
    cash_balance: Optional[Decimal] = None
    frozen_balance: Optional[Decimal] = None
    equity: Optional[Decimal] = None
    unrealized_pnl: Optional[Decimal] = None
    timestamp: datetime


class Position(StableModel):
    inst_id: str
    margin_mode: str
    position_side: str
    contracts: Decimal
    available_contracts: Decimal
    average_price: Optional[Decimal] = None
    mark_price: Optional[Decimal] = None
    liquidation_price: Optional[Decimal] = None
    leverage: Optional[Decimal] = None
    margin_ratio: Optional[Decimal] = None
    unrealized_pnl: Optional[Decimal] = None
    timestamp: datetime


class LeverageInfo(StableModel):
    inst_id: str
    margin_mode: str
    position_side: str
    leverage: Decimal


class TradingFee(StableModel):
    inst_type: Literal["SWAP"]
    inst_id: Optional[str] = None
    maker_rate: Decimal
    taker_rate: Decimal


class OrderQuery(StableModel):
    inst_id: str
    order_id: str
    client_order_id: Optional[str] = None
    state: str
    side: str
    position_side: str
    margin_mode: Optional[str] = None
    order_type: str
    reduce_only: Optional[bool] = None
    price: Optional[Decimal] = None
    size: Decimal
    accumulated_fill_size: Decimal
    average_price: Optional[Decimal] = None
    fee: Optional[Decimal] = None
    fee_currency: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class FillQuery(StableModel):
    fill_id: str
    order_id: str
    inst_id: str
    price: Decimal
    size: Decimal
    fee: Optional[Decimal] = None
    timestamp: datetime

    @field_serializer("price", "size", "fee")
    def serialize_fill_decimal(self, value: Optional[Decimal]) -> Optional[str]:
        return format(value, "f") if value is not None else None
