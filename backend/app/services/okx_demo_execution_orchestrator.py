from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Any, Callable, Mapping, Optional

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.adapters.okx_demo.models import TrustedSignalBundle
from app.core.config import get_settings
from app.models import OkxDemoTrustedSnapshot, StrategyVersion
from app.repositories.full_chain import (
    FullChainBlocked,
    FullChainConflict,
    FullChainRepository,
)
from app.repositories.strategy_deployments import (
    StrategyDeploymentBlocked,
    StrategyDeploymentConflict,
    StrategyDeploymentRepository,
)
from app.schemas.dry_run_status import redact_secret_text
from app.schemas.strategy_blueprint import StrategyBlueprint
from app.schemas.strategy_signal import (
    BlueprintSignalEvaluation,
    BlueprintSignalEvaluationRequest,
    ClosedCandle,
)
from app.services.blueprint_signal_evaluator import (
    BlueprintSignalEvaluationBlocked,
    BlueprintSignalEvaluator,
)
from app.services.risk_chain import RiskChainBlocked, RiskChainResult, RiskChainService


class OkxDemoExecutionOrchestrationBlocked(RuntimeError):
    """One leased evaluation could not safely reach a completed RISK checkpoint."""


@dataclass(frozen=True)
class OkxDemoExecutionOrchestrationResult:
    evaluation_id: int
    status: str
    signal_digest: Optional[str] = None
    full_chain_run_id: Optional[int] = None
    signal_snapshot_id: Optional[int] = None
    trade_intent_id: Optional[int] = None
    risk_decision_id: Optional[int] = None
    approved_execution_id: Optional[int] = None


@dataclass(frozen=True)
class _StrategyMaterial:
    blueprint: StrategyBlueprint
    generated_code: str
    code_hash: str


class OkxDemoExecutionOrchestrator:
    """Advance one fenced closed-candle evaluation through SIGNAL and RISK.

    Network reads, deterministic signal evaluation, persistence, and risk
    evaluation are injected independently.  This keeps the orchestration fully
    testable without credentials while production defaults use the existing
    repositories and services.
    """

    def __init__(
        self,
        db: Session,
        *,
        read_client: Any,
        deployment_repository: Optional[StrategyDeploymentRepository] = None,
        full_chain_repository: Optional[FullChainRepository] = None,
        evaluator: Optional[BlueprintSignalEvaluator] = None,
        risk_service: Optional[RiskChainService] = None,
        risk_policy: Optional[Mapping[str, Any]] = None,
        strategy_loader: Optional[Callable[[int], _StrategyMaterial]] = None,
        snapshot_loader: Optional[
            Callable[[TrustedSignalBundle], Mapping[str, Mapping[str, Any]]]
        ] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.db = db
        self.read_client = read_client
        self.deployments = deployment_repository or StrategyDeploymentRepository(db)
        self.chains = full_chain_repository or FullChainRepository(db)
        self.evaluator = evaluator or BlueprintSignalEvaluator()
        self.risk = risk_service or RiskChainService(db)
        configured_policy = (
            get_settings().demo_automation_policy.demo_risk_policy.model_dump(
                mode="json"
            )
            if risk_policy is None
            else dict(risk_policy)
        )
        configured_policy.pop("schema_version", None)
        self.risk_policy = configured_policy
        self._strategy_loader = strategy_loader or self._load_strategy
        self._snapshot_loader = snapshot_loader or self._load_snapshots
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def process(
        self,
        evaluation_id: int,
        *,
        lease_token: str,
        fencing_sequence: int,
        now: Optional[datetime] = None,
    ) -> OkxDemoExecutionOrchestrationResult:
        active_now = _aware(now or self._clock())
        replay = self._terminal_replay(evaluation_id)
        if replay is not None:
            return replay

        chain_id: Optional[int] = None
        prepared_stage: Optional[str] = None
        input_digest = hashlib.sha256(
            f"evaluation:{evaluation_id}".encode("ascii")
        ).hexdigest()
        try:
            evaluation, deployment = self.deployments.require_active_lease(
                evaluation_id,
                lease_token=lease_token,
                fencing_sequence=fencing_sequence,
                now=active_now,
            )
            # Keep identifiers as plain scalars across repository commits/rollbacks.
            # SQLAlchemy expires ORM instances at those boundaries and re-reading an
            # attribute would implicitly open a transaction before RiskChainService.
            deployment_id = deployment.id
            material = self._strategy_loader(deployment.strategy_version_id)
            checkpoint = evaluation.result_snapshot or {}
            if evaluation.input_digest is not None or checkpoint:
                bundle, signal, input_digest = self._restore_checkpoint(
                    evaluation=evaluation,
                    deployment=deployment,
                )
                self._require_fresh_bundle(bundle, active_now)
                snapshots = self._snapshot_loader(bundle)
            else:
                bundle = self.read_client.capture_trusted_signal_bundle(
                    self.db,
                    inst_id=deployment.instrument_id,
                    timeframe=deployment.timeframe,
                    candle_limit=self.evaluator._required_candle_count(
                        material.blueprint
                    ),
                )
                if (
                    bundle.execution_target != "OKX_DEMO"
                    or bundle.instrument_id != deployment.instrument_id
                    or bundle.timeframe != deployment.timeframe
                ):
                    raise OkxDemoExecutionOrchestrationBlocked(
                        "trusted signal bundle is stale or deployment-inconsistent"
                    )
                # Capturing authoritative OKX snapshots can take materially longer
                # than the bundle's short TTL.  A timestamp taken before that I/O
                # makes a newly observed snapshot look as though it came from the
                # future.  Refresh only the verifier clock; bundle timestamps and
                # expiry remain immutable and are still checked fail-closed.
                active_now = _aware(self._clock())
                self._require_fresh_bundle(bundle, active_now)
                snapshots = self._snapshot_loader(bundle)
                signal_request = self._signal_request(
                    deployment=deployment,
                    material=material,
                    bundle=bundle,
                    snapshots=snapshots,
                    evaluated_at=active_now,
                )
                input_digest = _digest(signal_request.model_dump(mode="json"))
                signal = self.evaluator.evaluate(signal_request)
                self._require_evaluation_candle(evaluation, signal)
                checkpoint = {
                    "checkpoint_schema": "SIGNAL_EVALUATION_V1",
                    "evaluation_id": evaluation.id,
                    "deployment_id": deployment.id,
                    "closed_candle_at": _aware(
                        evaluation.closed_candle_at
                    ).isoformat(),
                    "bundle": bundle.model_dump(mode="json"),
                    "signal": signal.model_dump(mode="json"),
                }
                self.deployments.checkpoint_leased(
                    evaluation.id,
                    lease_token=lease_token,
                    fencing_sequence=fencing_sequence,
                    input_digest=input_digest,
                    result_snapshot=checkpoint,
                    now=active_now,
                )
            if signal.decision == "NO_ACTION":
                completed = self.deployments.complete(
                    evaluation.id,
                    lease_token=lease_token,
                    fencing_sequence=fencing_sequence,
                    status="NO_ACTION",
                    input_digest=input_digest,
                    result_snapshot={
                        "status": "NO_ACTION",
                        "signal_digest": signal.signal_digest,
                        "market_snapshot_id": bundle.market.snapshot_id,
                        "candle_set_digest": bundle.candle_set_digest,
                    },
                    now=active_now,
                )
                if completed is None:
                    raise StrategyDeploymentBlocked(
                        "evaluation lease was lost before NO_ACTION completion"
                    )
                return OkxDemoExecutionOrchestrationResult(
                    evaluation_id=evaluation.id,
                    status="NO_ACTION",
                    signal_digest=signal.signal_digest,
                )

            chain = self.chains.open_for_signal_evaluation(
                evaluation.id,
                lease_token,
                fencing_sequence,
                now=active_now,
            )
            chain_id = chain.id
            if chain.status in {"FAILED", "BLOCKED", "CANCELLED", "STALE"}:
                raise OkxDemoExecutionOrchestrationBlocked(
                    "checkpoint execution chain is terminal"
                )
            if chain.signal_snapshot_id is not None:
                # On a new fencing lease the inherited execution approval has
                # reached its old lease expiry. Renew it only to the shorter of
                # the new lease and the immutable signal expiry before replaying
                # record_signal/RISK.
                self.deployments.renew_checkpoint_execution_authority(
                    evaluation.id,
                    lease_token=lease_token,
                    fencing_sequence=fencing_sequence,
                    full_chain_run_id=chain.id,
                    signal_snapshot_id=chain.signal_snapshot_id,
                    now=active_now,
                )
            signal_input = {
                "evaluation_id": evaluation.id,
                "fencing_sequence": fencing_sequence,
                "strategy_version_id": deployment.strategy_version_id,
                "candidate_digest": deployment.candidate_digest,
                "market_snapshot_id": bundle.market.snapshot_id,
                "market_digest": bundle.market.digest,
                "candle_set_digest": bundle.candle_set_digest,
                "signal_digest": signal.signal_digest,
            }
            self.chains.prepare_execution_stage(
                chain.id,
                "SIGNAL",
                evaluation_id=evaluation.id,
                lease_token=lease_token,
                fencing_sequence=fencing_sequence,
                idempotency_key=f"signal-evaluation:{evaluation.id}:{signal.signal_digest}",
                input_snapshot=signal_input,
                now=active_now,
            )
            prepared_stage = "SIGNAL"
            signal_row = self.chains.record_execution_signal(
                chain.id,
                evaluation_id=evaluation.id,
                lease_token=lease_token,
                fencing_sequence=fencing_sequence,
                instrument_id=deployment.instrument_id,
                source_type="api_aggregate",
                source_database_ids={
                    "instrument_snapshot": bundle.instrument.database_id,
                    "market_snapshot": bundle.market.database_id,
                    "account_snapshot": bundle.account.database_id,
                },
                signal_snapshot=signal.model_dump(mode="json"),
                observed_at=bundle.observed_at,
                expires_at=bundle.expires_at,
            )
            self.chains.complete_execution_stage(
                chain.id,
                "SIGNAL",
                evaluation_id=evaluation.id,
                lease_token=lease_token,
                fencing_sequence=fencing_sequence,
                database_ids={"signal_snapshot_id": signal_row.id},
                output_snapshot={
                    "status": "ACTIONABLE",
                    "signal_digest": signal.signal_digest,
                    "persisted_signal_digest": signal_row.signal_digest,
                },
                now=active_now,
            )
            prepared_stage = None
            # Re-check immediately before the owner-mediated risk boundary.
            # Slow signal persistence must not turn an expired bundle into an
            # approved risk chain.
            active_now = _aware(self._clock())
            self._require_fresh_bundle(bundle, active_now)
            self.deployments.renew_checkpoint_execution_authority(
                evaluation.id,
                lease_token=lease_token,
                fencing_sequence=fencing_sequence,
                full_chain_run_id=chain.id,
                signal_snapshot_id=signal_row.id,
                now=active_now,
            )

            risk_request = self._risk_request(
                chain=chain,
                signal_row=signal_row,
                signal=signal,
                bundle=bundle,
                snapshots=snapshots,
                material=material,
            )
            risk_input_digest = _digest(risk_request)
            risk_canonical_hash = _digest(
                RiskChainService._authorization_input(risk_request)
            )
            risk_idempotency_digest = hashlib.sha256(
                f"signal-evaluation-{evaluation_id}".encode("utf-8")
            ).hexdigest()
            risk_intent_id = _digest(
                {
                    "execution_target": "OKX_DEMO",
                    "input_digest": risk_canonical_hash,
                    "policy_digest": _digest(self.risk_policy),
                    "idempotency_digest": risk_idempotency_digest,
                }
            )
            self.chains.prepare_execution_stage(
                chain.id,
                "RISK",
                evaluation_id=evaluation.id,
                lease_token=lease_token,
                fencing_sequence=fencing_sequence,
                idempotency_key=f"risk-evaluation:{evaluation.id}:{signal.signal_digest}",
                input_snapshot={
                    "evaluation_id": evaluation.id,
                    "signal_snapshot_id": signal_row.id,
                    "signal_digest": signal_row.signal_digest,
                    "risk_input_digest": risk_input_digest,
                    "risk_canonical_hash": risk_canonical_hash,
                    "risk_idempotency_digest": risk_idempotency_digest,
                    "risk_intent_id": risk_intent_id,
                    "risk_client_order_id": "FAI" + risk_intent_id[:29],
                    "policy_digest": _digest(self.risk_policy),
                },
                now=active_now,
            )
            prepared_stage = "RISK"
            # Repository reads above may leave an implicit SQLAlchemy transaction;
            # RiskChainService owns its transaction and requires a clean boundary.
            if self.db.in_transaction():
                self.db.rollback()
            risk_result = self.risk.evaluate(
                # ``rollback()`` expires SQLAlchemy ORM instances.  Reading
                # ``evaluation.id`` here can therefore issue an implicit
                # SELECT and reopen a transaction before RiskChainService
                # takes ownership of its transaction boundary.  Keep using
                # the already-validated scalar function argument instead.
                idempotency_key=f"signal-evaluation-{evaluation_id}",
                request=risk_request,
                policy=self.risk_policy,
                natural_signal_context={
                    "deployment_id": deployment_id,
                    "lease_token": lease_token,
                    "fencing_sequence": fencing_sequence,
                },
                now=active_now,
            )
            if (
                risk_result.status != "APPROVED"
                or risk_result.approved_execution_id is None
            ):
                raise OkxDemoExecutionOrchestrationBlocked(
                    f"risk decision is {risk_result.status}, not APPROVED"
                )
            self.chains.complete_execution_stage(
                chain.id,
                "RISK",
                evaluation_id=evaluation.id,
                lease_token=lease_token,
                fencing_sequence=fencing_sequence,
                database_ids={
                    "trade_intent_id": risk_result.trade_intent_id,
                    "risk_decision_id": risk_result.risk_decision_id,
                    "approved_execution_id": risk_result.approved_execution_id,
                },
                output_snapshot={
                    "status": risk_result.status,
                    "intent_id": risk_result.intent_id,
                    "client_order_id": risk_result.client_order_id,
                    "order_submission_authorized": (
                        risk_result.order_submission_authorized
                    ),
                },
                now=active_now,
            )
            prepared_stage = None
            result_snapshot = {
                "status": "ACTIONABLE",
                "signal_digest": signal.signal_digest,
                "full_chain_run_id": chain.id,
                "signal_snapshot_id": signal_row.id,
                "trade_intent_id": risk_result.trade_intent_id,
                "risk_decision_id": risk_result.risk_decision_id,
                "approved_execution_id": risk_result.approved_execution_id,
            }
            completed = self.deployments.complete(
                evaluation.id,
                lease_token=lease_token,
                fencing_sequence=fencing_sequence,
                status="ACTIONABLE",
                input_digest=input_digest,
                result_snapshot=result_snapshot,
                now=active_now,
            )
            if completed is None:
                raise StrategyDeploymentBlocked(
                    "evaluation lease was lost before ACTIONABLE completion"
                )
            return OkxDemoExecutionOrchestrationResult(
                evaluation_id=evaluation.id,
                status="ACTIONABLE",
                signal_digest=signal.signal_digest,
                full_chain_run_id=chain.id,
                signal_snapshot_id=signal_row.id,
                trade_intent_id=risk_result.trade_intent_id,
                risk_decision_id=risk_result.risk_decision_id,
                approved_execution_id=risk_result.approved_execution_id,
            )
        except _BLOCKED_EXCEPTIONS as exc:
            self._fail_closed(
                evaluation_id=evaluation_id,
                lease_token=lease_token,
                fencing_sequence=fencing_sequence,
                input_digest=input_digest,
                chain_id=chain_id,
                prepared_stage=prepared_stage,
                status="BLOCKED",
                error_code="DEMO_EXECUTION_BLOCKED",
                error_message=str(exc),
                now=active_now,
            )
            raise OkxDemoExecutionOrchestrationBlocked(
                redact_secret_text(str(exc))
            ) from exc
        except Exception as exc:
            self._fail_closed(
                evaluation_id=evaluation_id,
                lease_token=lease_token,
                fencing_sequence=fencing_sequence,
                input_digest=input_digest,
                chain_id=chain_id,
                prepared_stage=prepared_stage,
                status="FAILED",
                error_code="DEMO_EXECUTION_FAILED",
                error_message=type(exc).__name__,
                now=active_now,
            )
            raise

    def _terminal_replay(
        self, evaluation_id: int
    ) -> Optional[OkxDemoExecutionOrchestrationResult]:
        evaluation = self.deployments.get_evaluation(evaluation_id)
        if evaluation is None or evaluation.status not in {
            "NO_ACTION",
            "ACTIONABLE",
            "BLOCKED",
            "FAILED",
        }:
            return None
        snapshot = evaluation.result_snapshot or {}
        return OkxDemoExecutionOrchestrationResult(
            evaluation_id=evaluation.id,
            status=evaluation.status,
            signal_digest=snapshot.get("signal_digest"),
            full_chain_run_id=snapshot.get("full_chain_run_id"),
            signal_snapshot_id=snapshot.get("signal_snapshot_id"),
            trade_intent_id=snapshot.get("trade_intent_id"),
            risk_decision_id=snapshot.get("risk_decision_id"),
            approved_execution_id=snapshot.get("approved_execution_id"),
        )

    @staticmethod
    def _require_evaluation_candle(
        evaluation: Any,
        signal: BlueprintSignalEvaluation,
    ) -> None:
        if _aware(evaluation.closed_candle_at) != _aware(
            signal.latest_closed_candle_at
        ):
            raise OkxDemoExecutionOrchestrationBlocked(
                "signal does not belong to the leased closed candle"
            )

    def _restore_checkpoint(
        self,
        *,
        evaluation: Any,
        deployment: Any,
    ) -> tuple[TrustedSignalBundle, BlueprintSignalEvaluation, str]:
        checkpoint = evaluation.result_snapshot or {}
        if (
            evaluation.input_digest is None
            or checkpoint.get("checkpoint_schema") != "SIGNAL_EVALUATION_V1"
            or checkpoint.get("evaluation_id") != evaluation.id
            or checkpoint.get("deployment_id") != deployment.id
            or _aware(
                datetime.fromisoformat(
                    str(checkpoint.get("closed_candle_at")).replace("Z", "+00:00")
                )
            )
            != _aware(evaluation.closed_candle_at)
        ):
            raise OkxDemoExecutionOrchestrationBlocked(
                "leased evaluation checkpoint is incomplete or inconsistent"
            )
        bundle = TrustedSignalBundle.model_validate(checkpoint.get("bundle"))
        signal = BlueprintSignalEvaluation.model_validate(checkpoint.get("signal"))
        if (
            bundle.execution_target != "OKX_DEMO"
            or bundle.instrument_id != deployment.instrument_id
            or bundle.timeframe != deployment.timeframe
            or signal.strategy_version_id != deployment.strategy_version_id
            or signal.candidate_digest != deployment.candidate_digest
            or signal.market_snapshot_id != bundle.market.snapshot_id
            or signal.market_digest != bundle.market.digest
        ):
            raise OkxDemoExecutionOrchestrationBlocked(
                "leased evaluation checkpoint binding is inconsistent"
            )
        self._require_evaluation_candle(evaluation, signal)
        return bundle, signal, evaluation.input_digest

    @staticmethod
    def _require_fresh_bundle(
        bundle: TrustedSignalBundle,
        active_now: datetime,
    ) -> None:
        expiries = {
            _aware(bundle.expires_at),
            _aware(bundle.instrument.expires_at),
            _aware(bundle.market.expires_at),
            _aware(bundle.account.expires_at),
        }
        if (
            len(expiries) != 1
            or _aware(bundle.observed_at) >= _aware(bundle.expires_at)
            or _aware(bundle.expires_at) <= active_now
        ):
            raise OkxDemoExecutionOrchestrationBlocked(
                "trusted signal checkpoint is expired or reference-inconsistent"
            )

    def _load_strategy(self, strategy_version_id: int) -> _StrategyMaterial:
        version = self.db.get(StrategyVersion, strategy_version_id)
        if (
            version is None
            or version.validation_status != "passed"
            or not version.code_hash
        ):
            raise OkxDemoExecutionOrchestrationBlocked(
                "deployed strategy version is missing or not validated"
            )
        return _StrategyMaterial(
            blueprint=StrategyBlueprint.model_validate(version.blueprint),
            generated_code=version.generated_code,
            code_hash=version.code_hash,
        )

    def _load_snapshots(
        self, bundle: TrustedSignalBundle
    ) -> Mapping[str, Mapping[str, Any]]:
        snapshots: dict[str, Mapping[str, Any]] = {}
        for reference in (bundle.instrument, bundle.market, bundle.account):
            row = self.db.get(OkxDemoTrustedSnapshot, reference.database_id)
            if (
                row is None
                or row.snapshot_id != reference.snapshot_id
                or row.digest != reference.digest
                or row.kind != reference.kind
                or row.execution_target_id != "OKX_DEMO"
            ):
                raise OkxDemoExecutionOrchestrationBlocked(
                    "trusted signal bundle database binding is inconsistent"
                )
            snapshots[reference.kind] = dict(row.content_json)
        return snapshots

    @staticmethod
    def _signal_request(
        *,
        deployment: Any,
        material: _StrategyMaterial,
        bundle: TrustedSignalBundle,
        snapshots: Mapping[str, Mapping[str, Any]],
        evaluated_at: datetime,
    ) -> BlueprintSignalEvaluationRequest:
        market = snapshots["market"]
        if market.get("candle_set_digest") != bundle.candle_set_digest:
            raise OkxDemoExecutionOrchestrationBlocked(
                "trusted candle digest does not match market snapshot"
            )
        candles = [
            ClosedCandle(
                open_time=item["timestamp"],
                open=item["open"],
                high=item["high"],
                low=item["low"],
                close=item["close"],
                volume=item["volume"],
                confirmed=True,
            )
            for item in market.get("confirmed_candles", [])
        ]
        return BlueprintSignalEvaluationRequest(
            execution_target="OKX_DEMO",
            instrument_id=deployment.instrument_id,
            strategy_version_id=deployment.strategy_version_id,
            candidate_digest=deployment.candidate_digest,
            market_snapshot_id=bundle.market.snapshot_id,
            market_digest=bundle.market.digest,
            blueprint=material.blueprint,
            generated_code=material.generated_code,
            code_hash=material.code_hash,
            candles=candles,
            evaluated_at=evaluated_at,
        )

    def _risk_request(
        self,
        *,
        chain: Any,
        signal_row: Any,
        signal: BlueprintSignalEvaluation,
        bundle: TrustedSignalBundle,
        snapshots: Mapping[str, Mapping[str, Any]],
        material: _StrategyMaterial,
    ) -> dict[str, Any]:
        instrument = snapshots["instrument"]
        market = snapshots["market"]
        account = snapshots["account"]
        reference = Decimal(str(market["reference_price"]))
        tick = Decimal(str(instrument["tickSz"]))
        position_side = "long" if signal.enter_long else "short"
        side = "buy" if position_side == "long" else "sell"
        limit_price = _align_price(
            reference,
            tick,
            rounding=ROUND_DOWN if position_side == "long" else ROUND_UP,
        )
        stop_fraction = Decimal(str(abs(material.blueprint.stoploss)))
        roi_fraction = Decimal(
            str(max(material.blueprint.minimal_roi.values()))
        )
        if position_side == "long":
            stop_loss = _align_price(
                limit_price * (Decimal("1") - stop_fraction),
                tick,
                rounding=ROUND_DOWN,
            )
            take_profit = _align_price(
                limit_price * (Decimal("1") + roi_fraction),
                tick,
                rounding=ROUND_UP,
            )
        else:
            stop_loss = _align_price(
                limit_price * (Decimal("1") + stop_fraction),
                tick,
                rounding=ROUND_UP,
            )
            take_profit = _align_price(
                limit_price * (Decimal("1") - roi_fraction),
                tick,
                rounding=ROUND_DOWN,
            )
        return {
            "execution_target": "OKX_DEMO",
            "full_chain_run_id": chain.id,
            "candidate_approval_id": chain.candidate_approval_id,
            "signal_snapshot_id": signal_row.id,
            "signal_digest": signal_row.signal_digest,
            "lineage": {
                "strategy_id": chain.strategy_id,
                "strategy_version_id": chain.strategy_version_id,
                "backtest_run_id": chain.backtest_run_id,
                "backtest_task_id": chain.backtest_task_id,
                "backtest_result_id": chain.backtest_result_id,
                "strategy_score_id": chain.strategy_score_id,
            },
            "snapshot_ids": {
                "instrument": bundle.instrument.snapshot_id,
                "market": bundle.market.snapshot_id,
                "account": bundle.account.snapshot_id,
            },
            "instrument_id": bundle.instrument_id,
            "side": side,
            "position_side": position_side,
            "order_type": "limit",
            "quantity": str(instrument["minSz"]),
            "limit_price": format(limit_price, "f"),
            "reference_price": format(reference, "f"),
            "leverage": str(
                account["leverage_by_position_side"][position_side]
            ),
            "margin_mode": "isolated",
            "stop_loss": format(stop_loss, "f"),
            "take_profit": format(take_profit, "f"),
            "reduce_only": False,
        }

    def _fail_closed(
        self,
        *,
        evaluation_id: int,
        lease_token: str,
        fencing_sequence: int,
        input_digest: str,
        chain_id: Optional[int],
        prepared_stage: Optional[str],
        status: str,
        error_code: str,
        error_message: str,
        now: datetime,
    ) -> None:
        safe_message = redact_secret_text(error_message)[:2000]
        if chain_id is not None and prepared_stage is not None:
            try:
                self.chains.fail_execution_stage(
                    chain_id,
                    prepared_stage,
                    evaluation_id=evaluation_id,
                    lease_token=lease_token,
                    fencing_sequence=fencing_sequence,
                    status=status,
                    error_code=error_code,
                    error_message=safe_message,
                    now=now,
                )
            except Exception:
                self.db.rollback()
        if chain_id is not None:
            try:
                self.deployments.block_checkpoint_execution_chain(
                    evaluation_id,
                    lease_token=lease_token,
                    fencing_sequence=fencing_sequence,
                    full_chain_run_id=chain_id,
                    reason=safe_message,
                    now=now,
                )
            except Exception:
                self.db.rollback()
        try:
            self.deployments.complete(
                evaluation_id,
                lease_token=lease_token,
                fencing_sequence=fencing_sequence,
                status=status,
                input_digest=input_digest,
                result_snapshot={"status": status, "reason": error_code},
                error_code=error_code,
                error_message=safe_message,
                now=now,
            )
        except Exception:
            self.db.rollback()


_BLOCKED_EXCEPTIONS = (
    OkxDemoExecutionOrchestrationBlocked,
    BlueprintSignalEvaluationBlocked,
    FullChainBlocked,
    FullChainConflict,
    RiskChainBlocked,
    StrategyDeploymentBlocked,
    StrategyDeploymentConflict,
    ValidationError,
    ValueError,
)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise OkxDemoExecutionOrchestrationBlocked(
            "orchestration clock must be timezone-aware"
        )
    return value.astimezone(timezone.utc)


def _digest(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _align_price(
    value: Decimal,
    tick: Decimal,
    *,
    rounding: str,
) -> Decimal:
    if value <= 0 or tick <= 0:
        raise OkxDemoExecutionOrchestrationBlocked(
            "price or tick evidence is invalid"
        )
    return (value / tick).to_integral_value(rounding=rounding) * tick
