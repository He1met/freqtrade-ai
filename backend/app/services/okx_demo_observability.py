from datetime import datetime, timezone
from typing import Callable, Optional, Type, TypeVar

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.execution_target import ONLY_EXCHANGE_EXECUTION_TARGET_ID
from app.models.full_chain import FullChainRun
from app.models.execution_lineage import (
    ExchangeOrder,
    ReconciliationRun,
    RiskDecision,
    TradeIntent,
)
from app.models.okx_demo_reconciliation import (
    OkxDemoAccountSnapshot,
    OkxDemoExchangeEvent,
    OkxDemoFillSnapshot,
    OkxDemoOrderSnapshot,
    OkxDemoPositionSnapshot,
    OkxDemoReconciliationState,
)
from app.schemas.okx_demo_observability import (
    OkxDemoAccountSummary,
    OkxDemoFillSummary,
    OkxDemoLineageSummary,
    OkxDemoObservabilityResponse,
    OkxDemoOrderSummary,
    OkxDemoPositionSummary,
    OkxDemoProjectionScope,
    OkxDemoReadinessCheck,
    OkxDemoReconciliationSummary,
    OkxDemoRiskDecisionSummary,
    OkxDemoTargetSummary,
    OkxDemoTradeIntentSummary,
)
from app.services.okx_demo_runtime_readiness import (
    OkxDemoRuntimeReadiness,
    read_okx_demo_runtime_readiness,
)


RECONCILED_STATUSES = {"RECONCILED", "RECOVERED"}
PROBLEM_RECONCILIATION_STATUSES = {"DRIFTED", "STALE"}
Snapshot = TypeVar(
    "Snapshot",
    OkxDemoOrderSnapshot,
    OkxDemoPositionSnapshot,
)


def order_completion(
    *,
    database_id: Optional[int],
    exchange_order_id: Optional[str],
    intent_id: Optional[str],
    risk_decision: Optional[str],
    authoritative_snapshot_database_id: Optional[int],
    authoritative_event_database_id: Optional[int],
    authoritative_identity_matches: bool,
    reconciliation_database_id: Optional[int],
    reconciliation_state_database_id: Optional[int],
    reconciliation_status: Optional[str],
    reconciliation_opening_frozen: bool,
    reconciliation_artifact_ready: bool,
    reconciliation_source_is_core: bool,
    reconciliation_covers_snapshot: bool,
    reconciliation_covers_event: bool,
    authoritative_fills_covered: bool,
    full_chain_database_id: Optional[int],
    full_chain_complete: bool,
) -> tuple[str, str]:
    """Fail closed unless one persisted order is bound to one authoritative run."""

    missing = []
    if not database_id:
        missing.append("订单数据库 ID")
    if not exchange_order_id:
        missing.append("交易所订单 ID")
    if not intent_id:
        missing.append("TradeIntent ID")
    if risk_decision != "APPROVED":
        missing.append("APPROVED 风控决定")
    if not authoritative_snapshot_database_id:
        missing.append("权威订单快照数据库 ID")
    if not authoritative_event_database_id:
        missing.append("权威事件数据库 ID")
    if not authoritative_identity_matches:
        missing.append("本地订单与权威快照身份一致性")
    if not reconciliation_database_id:
        missing.append("对账数据库 ID")
    if not reconciliation_state_database_id:
        missing.append("对账状态数据库 ID")
    if reconciliation_status not in RECONCILED_STATUSES:
        missing.append("已完成对账状态")
    if reconciliation_opening_frozen:
        missing.append("未冻结的新开仓门禁")
    if not reconciliation_artifact_ready:
        missing.append("READY 对账 artifact")
    if not reconciliation_source_is_core:
        missing.append("真实核心对账来源")
    if not reconciliation_covers_snapshot:
        missing.append("订单快照与对账关联")
    if not reconciliation_covers_event:
        missing.append("权威事件与对账关联")
    if not authoritative_fills_covered:
        missing.append("权威成交与对账关联")
    if not full_chain_database_id or not full_chain_complete:
        missing.append("DeepSeek 到对账的完整持久链")
    if missing:
        return "INCOMPLETE", "缺少" + "、".join(missing) + "，不能显示完成。"
    return "COMPLETE", "完整策略链、订单、风控、权威快照与对账数据库证据完整。"


class OkxDemoObservabilityService:
    """Read-only allowlist over the authoritative #448 reconciliation models."""

    def __init__(
        self,
        db: Session,
        *,
        runtime_readiness_provider: Optional[
            Callable[[], OkxDemoRuntimeReadiness]
        ] = None,
    ) -> None:
        self.db = db
        self._runtime_readiness_provider = (
            runtime_readiness_provider or read_okx_demo_runtime_readiness
        )

    def build(self, limit: int = 100) -> OkxDemoObservabilityResponse:
        target_id = ONLY_EXCHANGE_EXECUTION_TARGET_ID
        as_of = datetime.now(timezone.utc)
        intent_total_count = int(self.db.scalar(
            select(func.count()).select_from(TradeIntent).where(
                TradeIntent.execution_target_id == target_id
            )
        ) or 0)
        order_total_count = int(self.db.scalar(
            select(func.count()).select_from(ExchangeOrder).where(
                ExchangeOrder.execution_target_id == target_id
            )
        ) or 0)
        intents = list(
            self.db.scalars(
                select(TradeIntent)
                .where(TradeIntent.execution_target_id == target_id)
                .order_by(TradeIntent.created_at.desc(), TradeIntent.id.desc())
                .limit(limit)
            ).all()
        )
        local_orders = list(
            self.db.scalars(
                select(ExchangeOrder)
                .where(ExchangeOrder.execution_target_id == target_id)
                .order_by(ExchangeOrder.updated_at.desc(), ExchangeOrder.id.desc())
                .limit(limit)
            ).all()
        )
        intent_by_id = {intent.id: intent for intent in intents}
        missing_intent_ids = {
            order.trade_intent_id for order in local_orders
            if order.trade_intent_id not in intent_by_id
        }
        if missing_intent_ids:
            referenced_intents = self.db.scalars(
                select(TradeIntent).where(
                    TradeIntent.execution_target_id == target_id,
                    TradeIntent.id.in_(missing_intent_ids),
                )
            ).all()
            intents.extend(referenced_intents)
        state = self.db.scalars(
            select(OkxDemoReconciliationState).where(
                OkxDemoReconciliationState.execution_target_id == target_id
            )
        ).first()
        run = (
            self.db.get(ReconciliationRun, state.last_reconciliation_run_id)
            if state is not None and state.last_reconciliation_run_id is not None
            else None
        )
        order_snapshot_ids = self._database_id_set(run, "order_snapshots")
        position_snapshot_ids = self._database_id_set(run, "position_snapshots")
        account_snapshot_ids = self._database_id_set(run, "account_snapshots")
        event_ids = self._database_id_set(run, "exchange_events")
        latest_exchange_event_id = self.db.scalars(
            select(OkxDemoExchangeEvent.database_id)
            .where(OkxDemoExchangeEvent.execution_target_id == target_id)
            .order_by(OkxDemoExchangeEvent.database_id.desc())
            .limit(1)
        ).first()
        authoritative_orders = self._latest_by_identity(
            OkxDemoOrderSnapshot,
            "exchange_order_id",
            database_ids=order_snapshot_ids,
            limit=max(limit * 4, 200),
        )
        authoritative_positions = self._latest_positions(
            database_ids=position_snapshot_ids,
            limit=max(limit * 4, 200),
        )
        latest_account = self.db.scalars(
            select(OkxDemoAccountSnapshot)
            .where(OkxDemoAccountSnapshot.execution_target_id == target_id)
            .where(OkxDemoAccountSnapshot.database_id.in_(account_snapshot_ids))
            .order_by(
                OkxDemoAccountSnapshot.observed_at.desc(),
                OkxDemoAccountSnapshot.database_id.desc(),
            )
            .limit(1)
        ).first() if account_snapshot_ids else None

        intent_by_id = {intent.id: intent for intent in intents}
        decisions = self._decisions(intent_by_id)
        fills, fill_coverage, fills_current = self._authoritative_fills(
            {order.exchange_order_id for order in local_orders if order.exchange_order_id},
            run=run,
        )
        reconciliation = self._reconciliation(state, run)
        order_projection_current = all(
            row.event_database_id in event_ids
            for row in authoritative_orders.values()
        )
        position_projection_current = all(
            row.event_database_id in event_ids
            for row in authoritative_positions.values()
        )
        chains = self._full_chains(local_orders, run=run, decisions=decisions)
        projection_current = (
            fills_current
            and order_projection_current
            and position_projection_current
            and (
                latest_exchange_event_id is None
                or latest_exchange_event_id in event_ids
            )
            and (
                latest_account is None
                or (
                    latest_account.event_database_id in event_ids
                )
            )
        )
        order_summaries = []
        for local in local_orders:
            authoritative = (
                authoritative_orders.get(local.exchange_order_id)
                if local.exchange_order_id
                else None
            )
            order_summaries.append(
                self._order(
                    local,
                    intent=intent_by_id.get(local.trade_intent_id),
                    decision=decisions.get(local.trade_intent_id),
                    authoritative=authoritative,
                    fills=fills.get(local.exchange_order_id or "", []),
                    state=state,
                    run=run,
                    reconciliation=reconciliation,
                    full_chain=chains.get(local.id),
                    authoritative_fills_covered=fill_coverage.get(
                        local.exchange_order_id or "",
                        False,
                    ),
                )
            )

        runtime_readiness = self._runtime_readiness_provider()
        readiness = self._readiness(
            decisions=list(decisions.values()),
            orders=order_summaries,
            state=state,
            run=run,
            reconciliation=reconciliation,
            projection_current=projection_current,
            runtime_readiness=runtime_readiness,
        )
        account = self._account(latest_account, state=state, run=run)
        acceptable = (
            bool(order_summaries)
            and intent_total_count <= limit
            and order_total_count <= limit
            and all(order.completion_state == "COMPLETE" for order in order_summaries)
            and account.status == "READY"
            and len(readiness) == 6
            and all(check.status == "READY" for check in readiness)
        )
        lineage = [
            self._lineage(
                intent,
                decisions=decisions,
                orders=order_summaries,
                state=state,
                reconciliation=reconciliation,
                chains=chains,
            )
            for intent in intents
        ]

        return OkxDemoObservabilityResponse(
            generated_at=as_of,
            source_type="api_aggregate",
            core_data=True,
            scope=OkxDemoProjectionScope(
                as_of=as_of,
                requested_limit=limit,
                intent_total_count=intent_total_count,
                intent_returned_count=len(intents),
                order_total_count=order_total_count,
                order_returned_count=len(local_orders),
                truncated=intent_total_count > limit or order_total_count > limit,
            ),
            target=OkxDemoTargetSummary(
                target_id="OKX_DEMO",
                label="OKX_DEMO / 模拟盘",
                exchange="okx",
                product_type="SWAP",
                margin_mode="isolated",
                account_mode="demo",
                simulated_trading=True,
                allow_real_funds=False,
            ),
            readiness=readiness,
            intents=[self._intent(intent) for intent in intents],
            orders=order_summaries,
            positions=[
                self._position(position) for position in authoritative_positions.values()
            ],
            account=account,
            latest_reconciliation=reconciliation,
            lineage=lineage,
            acceptance_state="ACCEPTABLE" if acceptable else "NOT_ACCEPTABLE",
            acceptance_reason=(
                "DeepSeek、策略、回测、评分、批准、信号、订单、成交、账户和权威对账证据全部就绪。"
                if acceptable
                else "完整持久链、订单证据、账户快照或运行门禁仍不完整；空结果、历史记录、截断窗口、部分记录和 UNKNOWN 均不能验收。"
            ),
        )

    def _decisions(
        self, intent_by_id: dict[int, TradeIntent]
    ) -> dict[int, OkxDemoRiskDecisionSummary]:
        if not intent_by_id:
            return {}
        rows = self.db.scalars(
            select(RiskDecision)
            .where(
                RiskDecision.execution_target_id == ONLY_EXCHANGE_EXECUTION_TARGET_ID,
                RiskDecision.trade_intent_id.in_(intent_by_id),
            )
            .order_by(RiskDecision.created_at.desc(), RiskDecision.id.desc())
        ).all()
        result: dict[int, OkxDemoRiskDecisionSummary] = {}
        for row in rows:
            result.setdefault(
                row.trade_intent_id,
                OkxDemoRiskDecisionSummary(
                    database_id=row.id,
                    trade_intent_database_id=row.trade_intent_id,
                    decision=row.decision,
                    policy_version=row.policy_version,
                    created_at=row.created_at,
                    reason=self._risk_reason(row.decision),
                ),
            )
        return result

    def _latest_by_identity(
        self,
        model: Type[Snapshot],
        identity_name: str,
        *,
        database_ids: set[int],
        limit: int,
    ) -> dict[str, Snapshot]:
        if not database_ids:
            return {}
        rows = self.db.scalars(
            select(model)
            .where(model.execution_target_id == ONLY_EXCHANGE_EXECUTION_TARGET_ID)
            .where(model.database_id.in_(database_ids))
            .order_by(model.observed_at.desc(), model.database_id.desc())
            .limit(limit)
        ).all()
        result: dict[str, Snapshot] = {}
        for row in rows:
            result.setdefault(str(getattr(row, identity_name)), row)
        return result

    def _latest_positions(
        self,
        *,
        database_ids: set[int],
        limit: int,
    ) -> dict[str, OkxDemoPositionSnapshot]:
        if not database_ids:
            return {}
        rows = self.db.scalars(
            select(OkxDemoPositionSnapshot)
            .where(
                OkxDemoPositionSnapshot.execution_target_id
                == ONLY_EXCHANGE_EXECUTION_TARGET_ID
            )
            .where(OkxDemoPositionSnapshot.database_id.in_(database_ids))
            .order_by(
                OkxDemoPositionSnapshot.observed_at.desc(),
                OkxDemoPositionSnapshot.database_id.desc(),
            )
            .limit(limit)
        ).all()
        result: dict[str, OkxDemoPositionSnapshot] = {}
        for row in rows:
            result.setdefault(
                "{}:{}".format(row.instrument_id, row.position_side),
                row,
            )
        return result

    def _authoritative_fills(
        self,
        exchange_order_ids: set[str],
        *,
        run: Optional[ReconciliationRun],
    ) -> tuple[
        dict[str, list[OkxDemoFillSummary]],
        dict[str, bool],
        bool,
    ]:
        if not exchange_order_ids:
            return {}, {}, True
        snapshot_ids = self._database_id_set(run, "fill_snapshots")
        if not snapshot_ids:
            return {}, {}, True
        rows = self.db.scalars(
            select(OkxDemoFillSnapshot)
            .where(
                OkxDemoFillSnapshot.execution_target_id
                == ONLY_EXCHANGE_EXECUTION_TARGET_ID,
                OkxDemoFillSnapshot.exchange_order_id.in_(exchange_order_ids),
                OkxDemoFillSnapshot.database_id.in_(snapshot_ids),
            )
            .order_by(
                OkxDemoFillSnapshot.observed_at.asc(),
                OkxDemoFillSnapshot.database_id.asc(),
            )
        ).all()
        result: dict[str, list[OkxDemoFillSummary]] = {}
        coverage: dict[str, bool] = {}
        all_current = True
        event_ids = self._database_id_set(run, "exchange_events")
        for row in rows:
            covered = (
                row.event_database_id in event_ids
            )
            if not covered:
                all_current = False
                continue
            result.setdefault(row.exchange_order_id, []).append(
                OkxDemoFillSummary(
                    database_id=row.database_id,
                    exchange_fill_id=row.exchange_fill_id,
                    price=row.price,
                    quantity=row.quantity,
                    fee=row.fee,
                    created_at=row.observed_at,
                )
            )
            coverage[row.exchange_order_id] = True
        return result, coverage, all_current

    def _full_chains(
        self,
        orders: list[ExchangeOrder],
        *,
        run: Optional[ReconciliationRun],
        decisions: dict[int, OkxDemoRiskDecisionSummary],
    ) -> dict[int, FullChainRun]:
        order_ids = [row.id for row in orders]
        if not order_ids or run is None:
            return {}
        rows = self.db.scalars(
            select(FullChainRun)
            .where(
                FullChainRun.execution_target_id
                == ONLY_EXCHANGE_EXECUTION_TARGET_ID,
                FullChainRun.exchange_order_id.in_(order_ids),
            )
            .order_by(FullChainRun.id.desc())
        ).all()
        result: dict[int, FullChainRun] = {}
        order_by_id = {row.id: row for row in orders}
        for chain in rows:
            order = order_by_id.get(chain.exchange_order_id or 0)
            decision = (
                decisions.get(order.trade_intent_id)
                if order is not None
                else None
            )
            if (
                order is not None
                and decision is not None
                and self._chain_is_complete(chain, order, decision, run)
            ):
                result.setdefault(order.id, chain)
        return result

    @staticmethod
    def _chain_is_complete(
        chain: FullChainRun,
        order: ExchangeOrder,
        decision: OkxDemoRiskDecisionSummary,
        run: ReconciliationRun,
    ) -> bool:
        required_ids = (
            chain.strategy_generation_run_id,
            chain.strategy_id,
            chain.strategy_version_id,
            chain.backtest_run_id,
            chain.backtest_task_id,
            chain.backtest_result_id,
            chain.strategy_score_id,
            chain.candidate_approval_id,
            chain.signal_snapshot_id,
            chain.trade_intent_id,
            chain.risk_decision_id,
            chain.approved_execution_id,
            chain.exchange_order_id,
            chain.exchange_fill_id,
            chain.reconciliation_run_id,
        )
        return bool(
            chain.status == "SUCCESS"
            and chain.current_stage == "RECONCILIATION"
            and all(isinstance(value, int) and value > 0 for value in required_ids)
            and chain.trade_intent_id == order.trade_intent_id
            and chain.risk_decision_id == decision.database_id
            and chain.exchange_order_id == order.id
            and chain.reconciliation_run_id == run.id
        )

    @staticmethod
    def _risk_reason(decision: str) -> Optional[str]:
        if decision == "APPROVED":
            return None
        if decision == "EXPIRED":
            return "风控授权已过期；重新生成意图并完成风险检查。"
        if decision == "REJECTED":
            return "风险策略拒绝该意图；检查仓位、名义价值和止损参数。"
        return "风控链未批准该意图；先解除阻塞并重新评估。"

    @staticmethod
    def _reconciliation(
        state: Optional[OkxDemoReconciliationState],
        run: Optional[ReconciliationRun],
    ) -> Optional[OkxDemoReconciliationSummary]:
        if state is None or run is None or state.last_reconciliation_run_id != run.id:
            return None
        reason = None
        if state.status in PROBLEM_RECONCILIATION_STATUSES:
            reason = "交易所与数据库尚未一致；已冻结新开仓，需运行权威恢复。"
        elif state.status not in RECONCILED_STATUSES:
            reason = "对账尚未完成，当前记录不可验收。"
        elif run.artifact_status != "READY" or not run.core_data:
            reason = "对账 artifact 或核心数据标记不完整，当前记录不可验收。"
        return OkxDemoReconciliationSummary(
            database_id=run.id,
            state_database_id=state.database_id,
            status=state.status,
            opening_frozen=state.opening_frozen,
            started_at=run.started_at,
            completed_at=run.completed_at,
            authoritative_observed_at=run.authoritative_observed_at,
            artifact_status=run.artifact_status,
            source_type=run.source_type,
            core_data=run.core_data,
            reason=reason,
        )

    @staticmethod
    def _database_id_set(row: Optional[ReconciliationRun], key: str) -> set[int]:
        if row is None or not isinstance(row.database_ids, dict):
            return set()
        values = row.database_ids.get(key)
        if not isinstance(values, list):
            return set()
        return {
            value
            for value in values
            if isinstance(value, int) and not isinstance(value, bool) and value > 0
        }

    @classmethod
    def _run_is_core_ready(
        cls,
        state: Optional[OkxDemoReconciliationState],
        run: Optional[ReconciliationRun],
    ) -> bool:
        return bool(
            state is not None
            and run is not None
            and state.last_reconciliation_run_id == run.id
            and state.status in RECONCILED_STATUSES
            and not state.opening_frozen
            and run.status == state.status
            and run.completed_at is not None
            and run.authoritative_observed_at is not None
            and run.artifact_status == "READY"
            and bool(run.artifact_sha256)
            and run.source_type == "api_aggregate"
            and run.core_data is True
            and run.id in cls._database_id_set(run, "reconciliation_run")
            and state.database_id in cls._database_id_set(run, "reconciliation_state")
        )

    @staticmethod
    def _intent(row: TradeIntent) -> OkxDemoTradeIntentSummary:
        return OkxDemoTradeIntentSummary(
            database_id=row.id,
            intent_id=row.intent_id,
            client_order_id=row.client_order_id,
            strategy_version_id=row.strategy_version_id,
            instrument_id=row.instrument_id,
            side=row.side,
            position_side=row.position_side,
            order_type=row.order_type,
            quantity=row.quantity,
            limit_price=row.limit_price,
            leverage=row.leverage,
            margin_mode=row.margin_mode,
            reduce_only=row.reduce_only,
            status=row.status,
            expires_at=row.expires_at,
            created_at=row.created_at,
        )

    @staticmethod
    def _position(row: OkxDemoPositionSnapshot) -> OkxDemoPositionSummary:
        return OkxDemoPositionSummary(
            database_id=row.database_id,
            event_database_id=row.event_database_id,
            instrument_id=row.instrument_id,
            position_side=row.position_side,
            quantity=row.quantity,
            average_price=row.average_price,
            observed_at=row.observed_at,
        )

    @classmethod
    def _account(
        cls,
        row: Optional[OkxDemoAccountSnapshot],
        *,
        state: Optional[OkxDemoReconciliationState],
        run: Optional[ReconciliationRun],
    ) -> OkxDemoAccountSummary:
        if row is None:
            return OkxDemoAccountSummary(
                status="NOT_AVAILABLE",
                reason="尚未读取到权威账户快照；账户数据不能用订单或仓位推算。",
            )
        covered = (
            cls._run_is_core_ready(state, run)
            and row.database_id in cls._database_id_set(run, "account_snapshots")
            and row.event_database_id in cls._database_id_set(run, "exchange_events")
        )
        return OkxDemoAccountSummary(
            status="READY" if covered else "STALE",
            reason=(
                "账户快照已包含在当前完成对账中。"
                if covered
                else "账户快照未包含在当前完成对账中，只能作为待更新证据。"
            ),
            database_id=row.database_id,
            event_database_id=row.event_database_id,
            equity=row.equity,
            available_balance=row.available_balance,
            margin_balance=row.margin_balance,
            observed_at=row.observed_at,
        )

    @classmethod
    def _order(
        cls,
        row: ExchangeOrder,
        *,
        intent: Optional[TradeIntent],
        decision: Optional[OkxDemoRiskDecisionSummary],
        authoritative: Optional[OkxDemoOrderSnapshot],
        fills: list[OkxDemoFillSummary],
        state: Optional[OkxDemoReconciliationState],
        run: Optional[ReconciliationRun],
        reconciliation: Optional[OkxDemoReconciliationSummary],
        full_chain: Optional[FullChainRun],
        authoritative_fills_covered: bool,
    ) -> OkxDemoOrderSummary:
        identity_matches = bool(
            authoritative is not None
            and row.exchange_order_id == authoritative.exchange_order_id
            and (
                authoritative.client_order_id is None
                or row.client_order_id == authoritative.client_order_id
            )
            and (
                intent is None
                or intent.instrument_id == authoritative.instrument_id
            )
        )
        core_ready = cls._run_is_core_ready(state, run)
        snapshot_covered = bool(
            authoritative is not None
            and authoritative.database_id
            in cls._database_id_set(run, "order_snapshots")
        )
        event_covered = bool(
            authoritative is not None
            and authoritative.event_database_id
            in cls._database_id_set(run, "exchange_events")
        )
        completion_state, completion_reason = order_completion(
            database_id=row.id,
            exchange_order_id=row.exchange_order_id,
            intent_id=intent.intent_id if intent else None,
            risk_decision=decision.decision if decision else None,
            authoritative_snapshot_database_id=(
                authoritative.database_id if authoritative else None
            ),
            authoritative_event_database_id=(
                authoritative.event_database_id if authoritative else None
            ),
            authoritative_identity_matches=identity_matches,
            reconciliation_database_id=reconciliation.database_id if reconciliation else None,
            reconciliation_state_database_id=(
                reconciliation.state_database_id if reconciliation else None
            ),
            reconciliation_status=reconciliation.status if reconciliation else None,
            reconciliation_opening_frozen=(
                reconciliation.opening_frozen if reconciliation else True
            ),
            reconciliation_artifact_ready=bool(
                reconciliation and reconciliation.artifact_status == "READY"
            ),
            reconciliation_source_is_core=core_ready,
            reconciliation_covers_snapshot=snapshot_covered,
            reconciliation_covers_event=event_covered,
            authoritative_fills_covered=authoritative_fills_covered,
            full_chain_database_id=full_chain.id if full_chain else None,
            full_chain_complete=full_chain is not None,
        )
        return OkxDemoOrderSummary(
            database_id=row.id,
            trade_intent_database_id=row.trade_intent_id,
            client_order_id=row.client_order_id,
            exchange_order_id=row.exchange_order_id,
            authoritative_snapshot_database_id=(
                authoritative.database_id if authoritative else None
            ),
            authoritative_event_database_id=(
                authoritative.event_database_id if authoritative else None
            ),
            full_chain_database_id=full_chain.id if full_chain else None,
            instrument_id=(
                authoritative.instrument_id
                if authoritative is not None
                else intent.instrument_id if intent else None
            ),
            side=intent.side if intent else None,
            order_type=intent.order_type if intent else None,
            quantity=(
                authoritative.quantity
                if authoritative is not None
                else intent.quantity if intent else None
            ),
            status=row.status,
            authoritative_status=authoritative.status if authoritative else None,
            filled_quantity=authoritative.filled_quantity if authoritative else None,
            average_price=authoritative.average_price if authoritative else None,
            reduce_only=authoritative.reduce_only if authoritative else None,
            authoritative_observed_at=(
                authoritative.observed_at if authoritative else None
            ),
            created_at=row.created_at,
            updated_at=row.updated_at,
            completion_state=completion_state,
            completion_reason=completion_reason,
            risk_decision=decision,
            fills=fills,
        )

    @staticmethod
    def _lineage(
        intent: TradeIntent,
        *,
        decisions: dict[int, OkxDemoRiskDecisionSummary],
        orders: list[OkxDemoOrderSummary],
        state: Optional[OkxDemoReconciliationState],
        reconciliation: Optional[OkxDemoReconciliationSummary],
        chains: dict[int, FullChainRun],
    ) -> OkxDemoLineageSummary:
        order = next(
            (
                item
                for item in orders
                if item.trade_intent_database_id == intent.id
            ),
            None,
        )
        chain = chains.get(order.database_id) if order else None
        return OkxDemoLineageSummary(
            full_chain_database_id=chain.id if chain else None,
            strategy_generation_run_database_id=(
                chain.strategy_generation_run_id if chain else None
            ),
            strategy_database_id=chain.strategy_id if chain else None,
            strategy_version_database_id=chain.strategy_version_id if chain else None,
            backtest_run_database_id=chain.backtest_run_id if chain else None,
            backtest_task_database_id=chain.backtest_task_id if chain else None,
            backtest_result_database_id=chain.backtest_result_id if chain else None,
            strategy_score_database_id=chain.strategy_score_id if chain else None,
            candidate_approval_database_id=chain.candidate_approval_id if chain else None,
            signal_snapshot_database_id=chain.signal_snapshot_id if chain else None,
            trade_intent_database_id=intent.id,
            risk_decision_database_id=(
                decisions[intent.id].database_id if intent.id in decisions else None
            ),
            approved_execution_database_id=(
                chain.approved_execution_id if chain else None
            ),
            order_database_id=order.database_id if order else None,
            fill_database_id=chain.exchange_fill_id if chain else None,
            exchange_order_id=order.exchange_order_id if order else None,
            authoritative_order_snapshot_database_id=(
                order.authoritative_snapshot_database_id if order else None
            ),
            authoritative_event_database_id=(
                order.authoritative_event_database_id if order else None
            ),
            reconciliation_database_id=(
                reconciliation.database_id if chain and reconciliation else None
            ),
            reconciliation_state_database_id=(
                state.database_id if chain and state else None
            ),
        )

    @classmethod
    def _readiness(
        cls,
        *,
        decisions: list[OkxDemoRiskDecisionSummary],
        orders: list[OkxDemoOrderSummary],
        state: Optional[OkxDemoReconciliationState],
        run: Optional[ReconciliationRun],
        reconciliation: Optional[OkxDemoReconciliationSummary],
        projection_current: bool,
        runtime_readiness: OkxDemoRuntimeReadiness,
    ) -> list[OkxDemoReadinessCheck]:
        latest_decision = decisions[0] if decisions else None
        risk_ready = latest_decision is not None and latest_decision.decision == "APPROVED"
        core_ready = cls._run_is_core_ready(state, run) and projection_current
        exchange_state_status = "READY" if core_ready else (
            state.status
            if state is not None and state.status in PROBLEM_RECONCILIATION_STATUSES
            else "UNKNOWN"
        )
        return [
            OkxDemoReadinessCheck(
                key="credentials",
                label="凭据能力",
                status="READY" if runtime_readiness.credentials_ready else "BLOCKED",
                summary=(
                    "唯一运行进程已提供当前、已认证的模拟盘 capability。"
                    if runtime_readiness.credentials_ready
                    else "当前模拟盘 capability 不可证明；页面不会读取或展示凭据。"
                ),
                action=(
                    None
                    if runtime_readiness.credentials_ready
                    else "恢复唯一 okx_runtime，并通过安全凭据预检。"
                ),
                observed_at=runtime_readiness.observed_at,
            ),
            OkxDemoReadinessCheck(
                key="instrument",
                label="合约规格",
                status="READY" if runtime_readiness.target_ready else "BLOCKED",
                summary=(
                    "活动运行目标与唯一 OKX SWAP / isolated / demo 配置一致。"
                    if runtime_readiness.target_ready
                    else "无法证明活动运行目标与唯一 OKX_DEMO 配置一致。"
                ),
                action=(
                    None
                    if runtime_readiness.target_ready
                    else "恢复绑定 OKX_DEMO 的唯一运行进程。"
                ),
                observed_at=runtime_readiness.observed_at,
            ),
            OkxDemoReadinessCheck(
                key="market",
                label="交易所状态新鲜度",
                status=exchange_state_status,
                summary=(
                    "四类权威交易所状态已完成新鲜度对账。"
                    if core_ready
                    else "权威订单、成交、仓位和账户状态尚未通过新鲜度门禁。"
                ),
                action=None if core_ready else "恢复完整 REST/WS 状态流并重新对账。",
                observed_at=state.last_event_observed_at if state else None,
            ),
            OkxDemoReadinessCheck(
                key="risk",
                label="风控",
                status="READY" if risk_ready else "BLOCKED",
                summary=(
                    "最近风控决定为 APPROVED。"
                    if risk_ready
                    else "没有可证明的 APPROVED 风控决定。"
                ),
                action=None if risk_ready else "运行风控链并生成持久化决定。",
                observed_at=latest_decision.created_at if latest_decision else None,
            ),
            OkxDemoReadinessCheck(
                key="writer",
                label="唯一写入器",
                status="READY" if runtime_readiness.writer_ready else "BLOCKED",
                summary=(
                    "唯一 okx_runtime 写入器 heartbeat 当前有效。"
                    if runtime_readiness.writer_ready
                    else "无法证明唯一 okx_runtime 写入器 heartbeat 当前有效。"
                ),
                action=(
                    None
                    if runtime_readiness.writer_ready
                    else "恢复唯一写入器进程、私有 heartbeat 与排他锁。"
                ),
                observed_at=runtime_readiness.observed_at,
            ),
            OkxDemoReadinessCheck(
                key="reconciliation",
                label="对账",
                status=exchange_state_status,
                summary=(
                    "当前对账状态、artifact 与数据库 ID 闭环完整。"
                    if core_ready
                    else "没有完整的权威对账闭环。"
                ),
                action=None if core_ready else "运行权威对账并处理漂移。",
                observed_at=(
                    reconciliation.authoritative_observed_at
                    if reconciliation else None
                ),
            ),
        ]
