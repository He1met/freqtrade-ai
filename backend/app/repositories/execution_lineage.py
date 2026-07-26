from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.spikes.okx_demo_compatibility import validate_okx_client_order_id
from app.models.execution_lineage import (
    LOCAL_DRY_RUN_SCOPE_ID,
    OKX_DEMO_TARGET_ID,
    UNKNOWN_LEGACY_SCOPE_ID,
    ExchangeFill,
    ExchangeOrder,
    ExchangePosition,
    ExecutionManifest,
    ExecutionScope,
    ReconciliationRun,
    RiskDecision,
    TradeIntent,
)


EXECUTION_SCOPE_CATALOG = (
    (OKX_DEMO_TARGET_ID, "EXCHANGE_TARGET", True, True),
    (LOCAL_DRY_RUN_SCOPE_ID, "NON_EXCHANGE", True, False),
    (UNKNOWN_LEGACY_SCOPE_ID, "LEGACY", False, False),
)


def ensure_execution_scope_catalog(db: Session) -> None:
    """Seed only immutable scope identities; never reclassify persisted records."""

    for scope_id, scope_kind, executable, exchange_writes in EXECUTION_SCOPE_CATALOG:
        if db.get(ExecutionScope, scope_id) is None:
            db.add(
                ExecutionScope(
                    scope_id=scope_id,
                    scope_kind=scope_kind,
                    executable=executable,
                    exchange_writes=exchange_writes,
                )
            )
    db.flush()


class ExecutionLineageRepository:
    """Target-bound persistence only; it deliberately contains no OKX transport."""

    def __init__(self, db: Session, execution_target_id: str) -> None:
        if execution_target_id != OKX_DEMO_TARGET_ID:
            raise ValueError("exchange persistence is restricted to OKX_DEMO")
        self.db = db
        self.execution_target_id = execution_target_id

    def create_trade_intent(
        self,
        *,
        client_order_id: str,
        instrument_id: str,
        side: str,
        position_side: str,
        order_type: str,
        quantity: Decimal,
        limit_price: Optional[Decimal] = None,
        strategy_version_id: Optional[int] = None,
        request_snapshot: Optional[dict] = None,
    ) -> TradeIntent:
        ensure_execution_scope_catalog(self.db)
        client_order_id = validate_okx_client_order_id(client_order_id)
        intent = TradeIntent(
            execution_target_id=self.execution_target_id,
            client_order_id=client_order_id,
            instrument_id=instrument_id,
            side=side,
            position_side=position_side,
            order_type=order_type,
            quantity=quantity,
            limit_price=limit_price,
            strategy_version_id=strategy_version_id,
            request_snapshot=request_snapshot or {},
        )
        self.db.add(intent)
        self.db.commit()
        self.db.refresh(intent)
        return intent

    def record_risk_decision(
        self,
        *,
        trade_intent_id: int,
        decision: str,
        policy_version: str,
        evidence_snapshot: Optional[dict] = None,
    ) -> RiskDecision:
        intent = self.db.get(TradeIntent, trade_intent_id)
        if intent is None or intent.execution_target_id != self.execution_target_id:
            raise ValueError("risk decision parent intent is missing or target-mismatched")
        decision_row = RiskDecision(
            execution_target_id=self.execution_target_id,
            trade_intent_id=trade_intent_id,
            decision=decision,
            policy_version=policy_version,
            evidence_snapshot=evidence_snapshot or {},
        )
        self.db.add(decision_row)
        self.db.commit()
        self.db.refresh(decision_row)
        return decision_row

    def record_order(
        self,
        *,
        trade_intent_id: int,
        client_order_id: str,
        status: str,
        exchange_order_id: Optional[str] = None,
        request_snapshot: Optional[dict] = None,
        response_snapshot: Optional[dict] = None,
    ) -> ExchangeOrder:
        intent = self.db.get(TradeIntent, trade_intent_id)
        if intent is None or intent.execution_target_id != self.execution_target_id:
            raise ValueError("order parent intent is missing or target-mismatched")
        client_order_id = validate_okx_client_order_id(client_order_id)
        if client_order_id != intent.client_order_id:
            raise ValueError("order client_order_id must match its persisted trade intent")
        order = ExchangeOrder(
            execution_target_id=self.execution_target_id,
            trade_intent_id=trade_intent_id,
            client_order_id=client_order_id,
            exchange_order_id=exchange_order_id,
            status=status,
            request_snapshot=request_snapshot or {},
            response_snapshot=response_snapshot or {},
        )
        self.db.add(order)
        self.db.commit()
        self.db.refresh(order)
        return order

    def list_orders(self, limit: int = 100) -> list[ExchangeOrder]:
        statement = (
            select(ExchangeOrder)
            .where(ExchangeOrder.execution_target_id == self.execution_target_id)
            .order_by(ExchangeOrder.created_at.desc(), ExchangeOrder.id.desc())
            .limit(limit)
        )
        return list(self.db.scalars(statement).all())

    def record_fill(
        self,
        *,
        exchange_order_row_id: int,
        exchange_fill_id: str,
        price: Decimal,
        quantity: Decimal,
        fee: Optional[Decimal] = None,
        snapshot: Optional[dict] = None,
    ) -> ExchangeFill:
        order = self.db.get(ExchangeOrder, exchange_order_row_id)
        if order is None or order.execution_target_id != self.execution_target_id:
            raise ValueError("fill parent order is missing or target-mismatched")
        fill = ExchangeFill(
            execution_target_id=self.execution_target_id,
            exchange_order_row_id=exchange_order_row_id,
            exchange_fill_id=exchange_fill_id,
            price=price,
            quantity=quantity,
            fee=fee,
            snapshot=snapshot or {},
        )
        self.db.add(fill)
        self.db.commit()
        self.db.refresh(fill)
        return fill

    def upsert_position(
        self,
        *,
        instrument_id: str,
        position_side: str,
        quantity: Decimal,
        observed_at: datetime,
        average_price: Optional[Decimal] = None,
        snapshot: Optional[dict] = None,
    ) -> ExchangePosition:
        statement = select(ExchangePosition).where(
            ExchangePosition.execution_target_id == self.execution_target_id,
            ExchangePosition.instrument_id == instrument_id,
            ExchangePosition.position_side == position_side,
        )
        position = self.db.scalars(statement).first()
        if position is None:
            position = ExchangePosition(
                execution_target_id=self.execution_target_id,
                instrument_id=instrument_id,
                position_side=position_side,
                quantity=quantity,
                observed_at=observed_at,
            )
            self.db.add(position)
        position.quantity = quantity
        position.average_price = average_price
        position.snapshot = snapshot or {}
        position.observed_at = observed_at
        self.db.commit()
        self.db.refresh(position)
        return position

    def create_reconciliation(
        self, *, status: str, summary_snapshot: Optional[dict] = None
    ) -> ReconciliationRun:
        row = ReconciliationRun(
            execution_target_id=self.execution_target_id,
            status=status,
            summary_snapshot=summary_snapshot or {},
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row


def record_execution_manifest(
    db: Session,
    *,
    execution_scope_id: str,
    manifest_kind: str,
    schema_version: str,
    artifact_path: str,
    artifact_sha256: str,
    database_ids: dict,
    executable_evidence: bool,
) -> ExecutionManifest:
    ensure_execution_scope_catalog(db)
    if execution_scope_id == UNKNOWN_LEGACY_SCOPE_ID and executable_evidence:
        raise ValueError("UNKNOWN_LEGACY cannot be executable evidence")
    if execution_scope_id not in {
        OKX_DEMO_TARGET_ID,
        LOCAL_DRY_RUN_SCOPE_ID,
        UNKNOWN_LEGACY_SCOPE_ID,
    }:
        raise ValueError("unknown execution scope")
    manifest = ExecutionManifest(
        execution_scope_id=execution_scope_id,
        manifest_kind=manifest_kind,
        schema_version=schema_version,
        artifact_path=artifact_path,
        artifact_sha256=artifact_sha256,
        database_ids=database_ids,
        executable_evidence=executable_evidence,
    )
    db.add(manifest)
    db.commit()
    db.refresh(manifest)
    return manifest


def list_execution_manifests(
    db: Session, *, execution_scope_id: str, limit: int = 100
) -> list[ExecutionManifest]:
    statement = (
        select(ExecutionManifest)
        .where(ExecutionManifest.execution_scope_id == execution_scope_id)
        .order_by(ExecutionManifest.created_at.desc(), ExecutionManifest.id.desc())
        .limit(limit)
    )
    return list(db.scalars(statement).all())
