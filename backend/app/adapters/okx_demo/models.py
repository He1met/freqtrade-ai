from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_serializer, model_validator


class StableModel(BaseModel):
    model_config = {"extra": "forbid"}


class ImmutableStableModel(BaseModel):
    model_config = {"extra": "forbid", "frozen": True}


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
    bill_id: str
    order_id: str
    inst_id: str
    price: Decimal
    size: Decimal
    fee: Optional[Decimal] = None
    timestamp: datetime

    @field_serializer("price", "size", "fee")
    def serialize_fill_decimal(self, value: Optional[Decimal]) -> Optional[str]:
        return format(value, "f") if value is not None else None


class TrustedClosedCandle(ImmutableStableModel):
    timestamp: datetime
    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    volume: Decimal = Field(ge=0)
    volume_ccy: Decimal = Field(ge=0)
    confirmed: Literal[True] = True

    @field_serializer(
        "open",
        "high",
        "low",
        "close",
        "volume",
        "volume_ccy",
    )
    def serialize_decimal(self, value: Decimal) -> str:
        return format(value, "f")

    @model_validator(mode="after")
    def validate_price_range(self) -> "TrustedClosedCandle":
        if self.low > min(self.open, self.close) or self.high < max(
            self.open,
            self.close,
        ):
            raise ValueError("closed candle OHLC range is inconsistent")
        return self


class TrustedBbo(ImmutableStableModel):
    bid_price: Decimal = Field(gt=0)
    bid_size: Decimal = Field(gt=0)
    ask_price: Decimal = Field(gt=0)
    ask_size: Decimal = Field(gt=0)
    timestamp: datetime

    @field_serializer("bid_price", "bid_size", "ask_price", "ask_size")
    def serialize_decimal(self, value: Decimal) -> str:
        return format(value, "f")


class TrustedMarkPrice(ImmutableStableModel):
    price: Decimal = Field(gt=0)
    timestamp: datetime

    @field_serializer("price")
    def serialize_decimal(self, value: Decimal) -> str:
        return format(value, "f")


class TrustedPositionSummary(ImmutableStableModel):
    position_side: Literal["long", "short"]
    contracts: Decimal = Field(ge=0)
    mark_price: Optional[Decimal] = Field(default=None, gt=0)
    leverage: Optional[Decimal] = Field(default=None, gt=0)
    timestamp: datetime

    @field_serializer("contracts", "mark_price", "leverage")
    def serialize_decimal(self, value: Optional[Decimal]) -> Optional[str]:
        return format(value, "f") if value is not None else None


class TrustedInstrumentSnapshotContent(ImmutableStableModel):
    execution_target: Literal["OKX_DEMO"] = "OKX_DEMO"
    source: Literal["okx_demo_rest"] = "okx_demo_rest"
    resource: Literal["instrument"] = "instrument"
    stale: Literal[False] = False
    authenticated: Literal[False] = False
    instId: str = Field(pattern=r"^[A-Z0-9]+-[A-Z0-9]+-SWAP$")
    instrument_type: Literal["SWAP"] = "SWAP"
    ctVal: Decimal = Field(gt=0)
    ctValCcy: str
    lotSz: Decimal = Field(gt=0)
    minSz: Decimal = Field(gt=0)
    tickSz: Decimal = Field(gt=0)
    contract_shape: Literal["linear", "inverse"]
    state: str
    expires_at: datetime

    @field_serializer("ctVal", "lotSz", "minSz", "tickSz")
    def serialize_decimal(self, value: Decimal) -> str:
        return format(value, "f")


class TrustedMarketSnapshotContent(ImmutableStableModel):
    execution_target: Literal["OKX_DEMO"] = "OKX_DEMO"
    source: Literal["okx_demo_rest"] = "okx_demo_rest"
    resource: Literal["market"] = "market"
    stale: Literal[False] = False
    authenticated: Literal[False] = False
    instrument_id: str = Field(pattern=r"^[A-Z0-9]+-[A-Z0-9]+-SWAP$")
    timeframe: str
    okx_bar: str
    reference_price: Decimal = Field(gt=0)
    as_of: datetime
    first_candle_at: datetime
    last_candle_at: datetime
    candle_count: int = Field(ge=2, le=300)
    candle_set_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmed_candles: tuple[TrustedClosedCandle, ...] = Field(
        min_length=2,
        max_length=300,
    )
    bbo: TrustedBbo
    mark: TrustedMarkPrice
    expires_at: datetime

    @field_serializer("reference_price")
    def serialize_decimal(self, value: Decimal) -> str:
        return format(value, "f")

    @model_validator(mode="after")
    def validate_market_summary(self) -> "TrustedMarketSnapshotContent":
        if (
            len(self.confirmed_candles) != self.candle_count
            or self.confirmed_candles[0].timestamp != self.first_candle_at
            or self.confirmed_candles[-1].timestamp != self.last_candle_at
            or self.first_candle_at >= self.last_candle_at
            or self.bbo.bid_price >= self.bbo.ask_price
            or self.mark.price != self.reference_price
        ):
            raise ValueError("trusted market snapshot summary is inconsistent")
        return self


class TrustedAccountSnapshotContent(ImmutableStableModel):
    execution_target: Literal["OKX_DEMO"] = "OKX_DEMO"
    source: Literal["okx_demo_rest"] = "okx_demo_rest"
    resource: Literal["account"] = "account"
    stale: Literal[False] = False
    authenticated: Literal[True] = True
    account_mode: Literal["long_short_mode"] = "long_short_mode"
    margin_mode: Literal["isolated"] = "isolated"
    current_exposure: Decimal = Field(ge=0)
    open_positions: int = Field(ge=0)
    exposure_by_position_side: dict[Literal["long", "short"], Decimal]
    open_positions_by_position_side: dict[Literal["long", "short"], int]
    leverage_by_position_side: dict[Literal["long", "short"], Decimal]
    positions: tuple[TrustedPositionSummary, ...]
    as_of: datetime
    expires_at: datetime

    @field_serializer("current_exposure")
    def serialize_decimal(self, value: Decimal) -> str:
        return format(value, "f")

    @field_serializer(
        "exposure_by_position_side",
        "leverage_by_position_side",
    )
    def serialize_decimal_mapping(
        self,
        value: dict[str, Decimal],
    ) -> dict[str, str]:
        return {key: format(item, "f") for key, item in value.items()}

    @model_validator(mode="after")
    def validate_account_summary(self) -> "TrustedAccountSnapshotContent":
        sides = {"long", "short"}
        if (
            set(self.exposure_by_position_side) != sides
            or set(self.open_positions_by_position_side) != sides
            or set(self.leverage_by_position_side) != sides
            or any(
                value < 0
                for value in self.exposure_by_position_side.values()
            )
            or any(
                value < 0
                for value in self.open_positions_by_position_side.values()
            )
            or any(
                value <= 0
                for value in self.leverage_by_position_side.values()
            )
            or self.current_exposure
            != sum(self.exposure_by_position_side.values(), Decimal("0"))
            or self.open_positions
            != sum(self.open_positions_by_position_side.values())
        ):
            raise ValueError("trusted account snapshot summary is inconsistent")
        return self


class TrustedSnapshotReference(ImmutableStableModel):
    kind: Literal["instrument", "market", "account"]
    database_id: int = Field(gt=0)
    snapshot_id: str = Field(
        pattern=r"^(instrument|market|account):[0-9a-f]{48}$"
    )
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expires_at: datetime


class TrustedSignalBundle(ImmutableStableModel):
    schema_version: Literal["1"] = "1"
    execution_target: Literal["OKX_DEMO"] = "OKX_DEMO"
    instrument_id: str = Field(pattern=r"^[A-Z0-9]+-[A-Z0-9]+-SWAP$")
    timeframe: str
    candle_set_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime
    expires_at: datetime
    instrument: TrustedSnapshotReference
    market: TrustedSnapshotReference
    account: TrustedSnapshotReference

    @model_validator(mode="after")
    def validate_bundle_references(self) -> "TrustedSignalBundle":
        if (
            self.instrument.kind != "instrument"
            or self.market.kind != "market"
            or self.account.kind != "account"
            or {
                self.instrument.expires_at,
                self.market.expires_at,
                self.account.expires_at,
                self.expires_at,
            }
            != {self.expires_at}
            or self.observed_at >= self.expires_at
        ):
            raise ValueError("trusted signal bundle references are inconsistent")
        return self


class ExecutionAttestationBundle(ImmutableStableModel):
    """Strategy-independent references for one bounded execution canary.

    This contract deliberately has no candles, strategy, candidate, signal, or
    deployment fields.  It is only enough to bind an instrument/market/account
    snapshot to the existing Demo writer gate; a normal signal bundle remains
    validated by :class:`TrustedSignalBundle` above.
    """

    schema_version: Literal["execution-1"] = "execution-1"
    execution_target: Literal["OKX_DEMO"] = "OKX_DEMO"
    instrument_id: str = Field(pattern=r"^[A-Z0-9]+-[A-Z0-9]+-SWAP$")
    observed_at: datetime
    expires_at: datetime
    instrument: TrustedSnapshotReference
    market: TrustedSnapshotReference
    account: TrustedSnapshotReference

    @model_validator(mode="after")
    def validate_bundle_references(self) -> "ExecutionAttestationBundle":
        if (
            self.instrument.kind != "instrument"
            or self.market.kind != "market"
            or self.account.kind != "account"
            or {
                self.instrument.expires_at,
                self.market.expires_at,
                self.account.expires_at,
                self.expires_at,
            }
            != {self.expires_at}
            or self.observed_at >= self.expires_at
        ):
            raise ValueError("execution attestation references are inconsistent")
        return self
