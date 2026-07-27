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
    (OKX_DEMO_TARGET_ID, "EXCHANGE_TARGET", True, False, False, False),
    (LOCAL_DRY_RUN_SCOPE_ID, "NON_EXCHANGE", False, True, False, False),
    (UNKNOWN_LEGACY_SCOPE_ID, "LEGACY", False, False, False, False),
)


def ensure_execution_scope_catalog(db: Session) -> None:
    """Seed only immutable scope identities; never reclassify persisted records."""

    if db.get_bind().dialect.name == "postgresql":
        rows = db.execute(
            select(
                ExecutionScope.scope_id,
                ExecutionScope.scope_kind,
                ExecutionScope.exchange_capable,
                ExecutionScope.executable,
                ExecutionScope.exchange_writes,
                ExecutionScope.order_submission_authorized,
            ).where(
                ExecutionScope.scope_id.in_(
                    [item[0] for item in EXECUTION_SCOPE_CATALOG]
                )
            )
        ).all()
        if set(tuple(row) for row in rows) != set(EXECUTION_SCOPE_CATALOG):
            raise ValueError(
                "PostgreSQL execution scope catalog is missing or altered"
            )
        return
    for (
        scope_id,
        scope_kind,
        exchange_capable,
        executable,
        exchange_writes,
        order_submission_authorized,
    ) in EXECUTION_SCOPE_CATALOG:
        if db.get(ExecutionScope, scope_id) is None:
            db.add(
                ExecutionScope(
                    scope_id=scope_id,
                    scope_kind=scope_kind,
                    exchange_capable=exchange_capable,
                    executable=executable,
                    exchange_writes=exchange_writes,
                    order_submission_authorized=order_submission_authorized,
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
        self.db.flush()
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
        self.db.flush()
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
        self.db.flush()
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
        self.db.flush()
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
        self.db.flush()
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
        self.db.flush()
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
    if execution_scope_id == UNKNOWN_LEGACY_SCOPE_ID:
        raise ValueError("UNKNOWN_LEGACY manifest scope is read-only")
    if execution_scope_id == OKX_DEMO_TARGET_ID and executable_evidence:
        raise ValueError(
            "OKX_DEMO executable evidence is blocked while order authorization is false"
        )
    if execution_scope_id not in {
        OKX_DEMO_TARGET_ID,
        LOCAL_DRY_RUN_SCOPE_ID,
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
    db.flush()
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
