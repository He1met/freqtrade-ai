from datetime import datetime
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, Field


OkxDemoReadinessStatus = Literal[
    "READY",
    "BLOCKED",
    "FAILED",
    "DRIFTED",
    "STALE",
    "UNKNOWN",
]


class OkxDemoTargetSummary(BaseModel):
    target_id: Literal["OKX_DEMO"]
    label: Literal["OKX_DEMO / 模拟盘"]
    exchange: Literal["okx"]
    product_type: Literal["SWAP"]
    margin_mode: Literal["isolated"]
    account_mode: Literal["demo"]
    simulated_trading: Literal[True]
    allow_real_funds: Literal[False]

    model_config = {"extra": "forbid"}


class OkxDemoReadinessCheck(BaseModel):
    key: Literal[
        "credentials",
        "instrument",
        "market",
        "risk",
        "writer",
        "reconciliation",
    ]
    label: str
    status: OkxDemoReadinessStatus
    summary: str
    action: Optional[str] = None
    observed_at: Optional[datetime] = None

    model_config = {"extra": "forbid"}


class OkxDemoRiskDecisionSummary(BaseModel):
    database_id: int = Field(gt=0)
    trade_intent_database_id: int = Field(gt=0)
    decision: str
    policy_version: str
    created_at: datetime
    reason: Optional[str] = None

    model_config = {"extra": "forbid"}


class OkxDemoTradeIntentSummary(BaseModel):
    database_id: int = Field(gt=0)
    intent_id: Optional[str] = None
    client_order_id: str
    strategy_version_id: Optional[int] = None
    instrument_id: Optional[str] = None
    side: Optional[str] = None
    position_side: Optional[str] = None
    order_type: Optional[str] = None
    quantity: Optional[Decimal] = None
    limit_price: Optional[Decimal] = None
    leverage: Optional[Decimal] = None
    margin_mode: Optional[str] = None
    reduce_only: Optional[bool] = None
    status: str
    expires_at: Optional[datetime] = None
    created_at: datetime

    model_config = {"extra": "forbid"}


class OkxDemoFillSummary(BaseModel):
    database_id: int = Field(gt=0)
    exchange_fill_id: str
    price: Decimal
    quantity: Decimal
    fee: Optional[Decimal] = None
    created_at: datetime

    model_config = {"extra": "forbid"}


class OkxDemoOrderSummary(BaseModel):
    database_id: int = Field(gt=0)
    trade_intent_database_id: int = Field(gt=0)
    client_order_id: str
    exchange_order_id: Optional[str] = None
    authoritative_snapshot_database_id: Optional[int] = None
    authoritative_event_database_id: Optional[int] = None
    full_chain_database_id: Optional[int] = None
    instrument_id: Optional[str] = None
    side: Optional[str] = None
    order_type: Optional[str] = None
    quantity: Optional[Decimal] = None
    status: str
    authoritative_status: Optional[str] = None
    filled_quantity: Optional[Decimal] = None
    average_price: Optional[Decimal] = None
    reduce_only: Optional[bool] = None
    authoritative_observed_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    completion_state: Literal["COMPLETE", "INCOMPLETE"]
    completion_reason: str
    risk_decision: Optional[OkxDemoRiskDecisionSummary] = None
    fills: list[OkxDemoFillSummary] = Field(default_factory=list)

    model_config = {"extra": "forbid"}


class OkxDemoPositionSummary(BaseModel):
    database_id: int = Field(gt=0)
    instrument_id: str
    position_side: str
    quantity: Decimal
    average_price: Optional[Decimal] = None
    observed_at: datetime
    event_database_id: int = Field(gt=0)

    model_config = {"extra": "forbid"}


class OkxDemoAccountSummary(BaseModel):
    status: Literal["READY", "STALE", "NOT_AVAILABLE"]
    reason: str
    database_id: Optional[int] = None
    event_database_id: Optional[int] = None
    equity: Optional[Decimal] = None
    available_balance: Optional[Decimal] = None
    margin_balance: Optional[Decimal] = None
    observed_at: Optional[datetime] = None

    model_config = {"extra": "forbid"}


class OkxDemoReconciliationSummary(BaseModel):
    database_id: int = Field(gt=0)
    state_database_id: int = Field(gt=0)
    status: str
    opening_frozen: bool
    started_at: datetime
    completed_at: Optional[datetime] = None
    authoritative_observed_at: Optional[datetime] = None
    artifact_status: str
    source_type: str
    core_data: bool
    reason: Optional[str] = None

    model_config = {"extra": "forbid"}


class OkxDemoLineageSummary(BaseModel):
    full_chain_database_id: Optional[int] = None
    strategy_generation_run_database_id: Optional[int] = None
    strategy_database_id: Optional[int] = None
    strategy_version_database_id: Optional[int] = None
    backtest_run_database_id: Optional[int] = None
    backtest_task_database_id: Optional[int] = None
    backtest_result_database_id: Optional[int] = None
    strategy_score_database_id: Optional[int] = None
    candidate_approval_database_id: Optional[int] = None
    signal_snapshot_database_id: Optional[int] = None
    trade_intent_database_id: int = Field(gt=0)
    risk_decision_database_id: Optional[int] = None
    approved_execution_database_id: Optional[int] = None
    order_database_id: Optional[int] = None
    fill_database_id: Optional[int] = None
    exchange_order_id: Optional[str] = None
    authoritative_order_snapshot_database_id: Optional[int] = None
    authoritative_event_database_id: Optional[int] = None
    reconciliation_database_id: Optional[int] = None
    reconciliation_state_database_id: Optional[int] = None

    model_config = {"extra": "forbid"}


class OkxDemoObservabilityResponse(BaseModel):
    generated_at: datetime
    source_type: Literal["api_aggregate"]
    core_data: Literal[True]
    target: OkxDemoTargetSummary
    readiness: list[OkxDemoReadinessCheck]
    intents: list[OkxDemoTradeIntentSummary]
    orders: list[OkxDemoOrderSummary]
    positions: list[OkxDemoPositionSummary]
    account: OkxDemoAccountSummary
    latest_reconciliation: Optional[OkxDemoReconciliationSummary] = None
    lineage: list[OkxDemoLineageSummary]
    acceptance_state: Literal["ACCEPTABLE", "NOT_ACCEPTABLE"]
    acceptance_reason: str

    model_config = {"extra": "forbid"}
