from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.adapters.okx_demo.models import (
    TrustedSignalBundle,
    TrustedSnapshotReference,
)
from app.schemas.strategy_blueprint import StrategyBlueprint
from app.schemas.strategy_signal import BlueprintSignalEvaluation
from app.services.blueprint_signal_evaluator import (
    BlueprintSignalEvaluationBlocked,
)
from app.services.okx_demo_execution_orchestrator import (
    OkxDemoExecutionOrchestrationBlocked,
    OkxDemoExecutionOrchestrator,
    _StrategyMaterial,
)
from app.services.risk_chain import RiskChainResult


NOW = datetime(2026, 7, 29, 12, 5, tzinfo=timezone.utc)


class FakeDatabase:
    def __init__(self) -> None:
        self.rollback_count = 0
        self.transaction_active = True

    def in_transaction(self) -> bool:
        return self.transaction_active

    def rollback(self) -> None:
        self.rollback_count += 1
        self.transaction_active = False


class FakeDeployments:
    def __init__(self, *, terminal=None, fail_actionable_completion=False) -> None:
        self.evaluation = terminal or SimpleNamespace(
            id=11,
            status="LEASED",
            result_snapshot={},
            input_digest=None,
            closed_candle_at=NOW - timedelta(minutes=5),
        )
        self.lease_token = "lease"
        self.fencing_sequence = 4
        self.deployment = SimpleNamespace(
            id=7,
            strategy_version_id=3,
            candidate_digest="c" * 64,
            instrument_id="BTC-USDT-SWAP",
            timeframe="5m",
        )
        self.completions = []
        self.checkpoints = []
        self.blocked_chains = []
        self.fail_actionable_completion = fail_actionable_completion

    def get_evaluation(self, evaluation_id):
        assert evaluation_id == 11
        return self.evaluation

    def require_active_lease(self, evaluation_id, **kwargs):
        assert evaluation_id == 11
        assert kwargs["lease_token"] == self.lease_token
        assert kwargs["fencing_sequence"] == self.fencing_sequence
        return self.evaluation, self.deployment

    def complete(self, evaluation_id, **kwargs):
        assert evaluation_id == 11
        self.completions.append(kwargs)
        if (
            kwargs["status"] == "ACTIONABLE"
            and self.fail_actionable_completion
        ):
            self.fail_actionable_completion = False
            return None
        self.evaluation.status = kwargs["status"]
        self.evaluation.result_snapshot = kwargs["result_snapshot"]
        return self.evaluation

    def checkpoint_leased(self, evaluation_id, **kwargs):
        assert evaluation_id == 11
        self.checkpoints.append(kwargs)
        self.evaluation.input_digest = kwargs["input_digest"]
        self.evaluation.result_snapshot = kwargs["result_snapshot"]
        return self.evaluation

    def renew_checkpoint_execution_authority(self, evaluation_id, **kwargs):
        assert evaluation_id == 11
        return NOW + timedelta(seconds=30)

    def block_checkpoint_execution_chain(self, evaluation_id, **kwargs):
        assert evaluation_id == 11
        self.blocked_chains.append(kwargs)
        return SimpleNamespace(status="BLOCKED")


class FakeChains:
    def __init__(self, *, crash_after_signal=False) -> None:
        self.calls = []
        self.crash_after_signal = crash_after_signal
        self.chain = SimpleNamespace(
            id=21,
            status="APPROVED",
            candidate_approval_id=22,
            strategy_id=1,
            strategy_version_id=3,
            backtest_run_id=4,
            backtest_task_id=5,
            backtest_result_id=6,
            strategy_score_id=7,
            signal_snapshot_id=None,
        )
        self.signal_row = SimpleNamespace(id=31, signal_digest="d" * 64)

    def open_for_signal_evaluation(self, *args, **kwargs):
        self.calls.append(("open", args, kwargs))
        return self.chain

    def prepare_execution_stage(self, chain_id, stage, **kwargs):
        self.calls.append(("prepare", stage, kwargs))
        return SimpleNamespace(status="PREPARED")

    def record_execution_signal(self, chain_id, **kwargs):
        self.calls.append(("record_signal", kwargs))
        return self.signal_row

    def complete_execution_stage(self, chain_id, stage, **kwargs):
        self.calls.append(("complete", stage, kwargs))
        if stage == "SIGNAL":
            self.chain.signal_snapshot_id = 31
        if stage == "SIGNAL" and self.crash_after_signal:
            self.crash_after_signal = False
            raise KeyboardInterrupt("simulated process crash")
        return SimpleNamespace(status="SUCCESS")

    def fail_execution_stage(self, chain_id, stage, **kwargs):
        self.calls.append(("fail", stage, kwargs))
        return SimpleNamespace(status=kwargs["status"])


class FakeReadClient:
    def __init__(self, bundle) -> None:
        self.bundle = bundle
        self.calls = 0

    def capture_trusted_signal_bundle(self, db, **kwargs):
        self.calls += 1
        assert kwargs == {
            "inst_id": "BTC-USDT-SWAP",
            "timeframe": "5m",
            "candle_limit": 50,
        }
        return self.bundle


class FakeEvaluator:
    def __init__(self, result=None, error=None) -> None:
        self.result = result
        self.error = error
        self.calls = 0

    @staticmethod
    def _required_candle_count(blueprint):
        return 50

    def evaluate(self, request):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


class FakeRisk:
    def __init__(self, *, db=None) -> None:
        self.calls = []
        self.db = db

    def evaluate(self, **kwargs):
        if self.db is not None and self.db.in_transaction():
            raise RuntimeError("risk received a dirty transaction")
        self.calls.append(kwargs)
        return RiskChainResult(
            status="APPROVED",
            trade_intent_id=41,
            risk_decision_id=42,
            approved_execution_id=43,
            intent_id="e" * 64,
            client_order_id="FAI" + "e" * 29,
            order_submission_authorized=False,
        )


def _blueprint() -> StrategyBlueprint:
    return StrategyBlueprint(
        name="Demo RSI",
        slug="demo-rsi",
        class_name="DemoRsi",
        timeframe="5m",
        stoploss=-0.04,
        minimal_roi={"0": 0.08},
        indicators=[{"name": "rsi", "kind": "rsi", "period": 14}],
        entry_rules=[{"indicator": "rsi", "operator": "<", "value": 30}],
        can_short=True,
        short_entry_rules=[
            {"indicator": "rsi", "operator": ">", "value": 70}
        ],
    )


def _bundle() -> TrustedSignalBundle:
    expiry = NOW + timedelta(minutes=1)
    digest_char = {"instrument": "b", "market": "c", "account": "d"}

    def reference(kind: str, database_id: int):
        character = digest_char[kind]
        return TrustedSnapshotReference(
            kind=kind,
            database_id=database_id,
            snapshot_id=f"{kind}:{character * 48}",
            digest=character * 64,
            expires_at=expiry,
        )

    return TrustedSignalBundle(
        instrument_id="BTC-USDT-SWAP",
        timeframe="5m",
        candle_set_digest="a" * 64,
        observed_at=NOW,
        expires_at=expiry,
        instrument=reference("instrument", 51),
        market=reference("market", 52),
        account=reference("account", 53),
    )


def _snapshots(bundle):
    candles = []
    for index in range(50):
        candles.append(
            {
                "timestamp": NOW - timedelta(minutes=5 * (50 - index)),
                "open": "50000",
                "high": "50100",
                "low": "49900",
                "close": "50000",
                "volume": "1",
            }
        )
    return {
        "instrument": {
            "tickSz": "0.1",
            "minSz": "0.01",
        },
        "market": {
            "reference_price": "50000",
            "candle_set_digest": bundle.candle_set_digest,
            "confirmed_candles": candles,
        },
        "account": {
            "leverage_by_position_side": {"long": "2", "short": "2"},
        },
    }


def _signal(decision: str) -> BlueprintSignalEvaluation:
    actionable = decision == "ACTIONABLE"
    return BlueprintSignalEvaluation(
        instrument_id="BTC-USDT-SWAP",
        strategy_version_id=3,
        candidate_digest="c" * 64,
        market_snapshot_id="market:" + "c" * 48,
        market_digest="c" * 64,
        code_hash="f" * 64,
        strategy_slug="demo-rsi",
        class_name="DemoRsi",
        timeframe="5m",
        decision=decision,
        candle_open_at=NOW - timedelta(minutes=5),
        candle_close_at=NOW,
        latest_closed_candle_at=NOW - timedelta(minutes=5),
        evaluated_at=NOW,
        enter_long=actionable,
        enter_short=False,
        indicator_values={"rsi": "20"},
        rule_evidence=[],
        candle_count=50,
        signal_digest="e" * 64,
    )


def _signal_for_candle(
    decision: str,
    closed_candle_at: datetime,
) -> BlueprintSignalEvaluation:
    signal = _signal(decision)
    return signal.model_copy(
        update={"latest_closed_candle_at": closed_candle_at}
    )


def _orchestrator(
    *,
    signal,
    deployments=None,
    chains=None,
    evaluator=None,
    db=None,
    risk=None,
    clock=None,
):
    bundle = _bundle()
    db = db or FakeDatabase()
    deployments = deployments or FakeDeployments()
    chains = chains or FakeChains()
    risk = risk or FakeRisk()
    service = OkxDemoExecutionOrchestrator(
        db,
        read_client=FakeReadClient(bundle),
        deployment_repository=deployments,
        full_chain_repository=chains,
        evaluator=evaluator or FakeEvaluator(signal),
        risk_service=risk,
        risk_policy={
            "allowed_instruments": ["BTC-USDT-SWAP"],
            "allowed_sides": ["buy", "sell"],
            "allowed_order_types": ["limit"],
            "max_leverage": 2,
            "max_order_notional": 1000,
            "max_total_exposure": 2000,
            "max_positions": 2,
            "max_price_deviation_pct": 0.01,
            "min_strategy_score": 70,
            "scoring_version": "phase2-quality-v1",
        },
        strategy_loader=lambda _: _StrategyMaterial(
            blueprint=_blueprint(),
            generated_code="generated",
            code_hash="f" * 64,
        ),
        snapshot_loader=lambda _: _snapshots(bundle),
        clock=clock or (lambda: NOW),
    )
    return service, deployments, chains, risk


def test_actionable_refreshes_verifier_clock_after_slow_snapshot_capture():
    captured_before_snapshot = NOW - timedelta(seconds=45)
    clock_values = iter((NOW, NOW))
    service, deployments, chains, risk = _orchestrator(
        signal=_signal("ACTIONABLE"),
        clock=lambda: next(clock_values),
    )

    result = service.process(
        11,
        lease_token="lease",
        fencing_sequence=4,
        now=captured_before_snapshot,
    )

    assert result.status == "ACTIONABLE"
    assert risk.calls[0]["now"] == NOW
    assert deployments.completions[0]["now"] == NOW


def test_no_action_completes_evaluation_without_opening_execution_chain():
    service, deployments, chains, risk = _orchestrator(
        signal=_signal("NO_ACTION")
    )

    result = service.process(
        11, lease_token="lease", fencing_sequence=4, now=NOW
    )

    assert result.status == "NO_ACTION"
    assert result.full_chain_run_id is None
    assert deployments.completions[0]["status"] == "NO_ACTION"
    assert chains.calls == []
    assert risk.calls == []


def test_actionable_signal_completes_signal_then_risk_and_evaluation():
    service, deployments, chains, risk = _orchestrator(
        signal=_signal("ACTIONABLE")
    )

    result = service.process(
        11, lease_token="lease", fencing_sequence=4, now=NOW
    )

    assert result.status == "ACTIONABLE"
    assert result.full_chain_run_id == 21
    assert result.approved_execution_id == 43
    assert [call[:2] for call in chains.calls] == [
        ("open", (11, "lease", 4)),
        ("prepare", "SIGNAL"),
        ("record_signal", {
            "evaluation_id": 11,
            "lease_token": "lease",
            "fencing_sequence": 4,
            "instrument_id": "BTC-USDT-SWAP",
            "source_type": "api_aggregate",
            "source_database_ids": {
                "instrument_snapshot": 51,
                "market_snapshot": 52,
                "account_snapshot": 53,
            },
            "signal_snapshot": _signal("ACTIONABLE").model_dump(mode="json"),
            "observed_at": NOW,
            "expires_at": NOW + timedelta(minutes=1),
        }),
        ("complete", "SIGNAL"),
        ("prepare", "RISK"),
        ("complete", "RISK"),
    ]
    request = risk.calls[0]["request"]
    assert request["side"] == "buy"
    assert request["position_side"] == "long"
    assert request["quantity"] == "0.01"
    assert request["full_chain_run_id"] == 21
    risk_prepare = next(
        call for call in chains.calls
        if call[0] == "prepare" and call[1] == "RISK"
    )
    assert risk_prepare[2]["idempotency_key"] == (
        "risk-evaluation:11:" + "d" * 64
    )
    assert deployments.completions[0]["result_snapshot"][
        "approved_execution_id"
    ] == 43


def test_risk_boundary_does_not_refresh_expired_evaluation_after_rollback():
    db = FakeDatabase()

    class ExpiringEvaluation:
        status = "LEASED"
        result_snapshot = {}
        input_digest = None
        closed_candle_at = NOW - timedelta(minutes=5)

        @property
        def id(self):
            if db.rollback_count:
                db.transaction_active = True
            return 11

    deployments = FakeDeployments(terminal=ExpiringEvaluation())
    risk = FakeRisk(db=db)
    service, _, _, _ = _orchestrator(
        signal=_signal("ACTIONABLE"),
        deployments=deployments,
        db=db,
        risk=risk,
    )

    result = service.process(
        11, lease_token="lease", fencing_sequence=4, now=NOW
    )

    assert result.status == "ACTIONABLE"
    assert db.rollback_count == 1
    assert len(risk.calls) == 1
    assert risk.calls[0]["idempotency_key"] == "signal-evaluation-11"


def test_terminal_evaluation_replay_does_not_repeat_signal_or_risk():
    terminal = SimpleNamespace(
        id=11,
        status="ACTIONABLE",
        input_digest="a" * 64,
        closed_candle_at=NOW - timedelta(minutes=5),
        result_snapshot={
            "signal_digest": "e" * 64,
            "full_chain_run_id": 21,
            "signal_snapshot_id": 31,
            "trade_intent_id": 41,
            "risk_decision_id": 42,
            "approved_execution_id": 43,
        },
    )
    deployments = FakeDeployments(terminal=terminal)
    service, _, chains, risk = _orchestrator(
        signal=_signal("ACTIONABLE"),
        deployments=deployments,
    )

    result = service.process(
        11, lease_token="different", fencing_sequence=99, now=NOW
    )

    assert result.approved_execution_id == 43
    assert deployments.completions == []
    assert chains.calls == []
    assert risk.calls == []


def test_signal_checkpoint_survives_expiry_and_new_fence_without_recapture():
    deployments = FakeDeployments()
    chains = FakeChains(crash_after_signal=True)
    evaluator = FakeEvaluator(_signal("ACTIONABLE"))
    service, _, _, risk = _orchestrator(
        signal=None,
        deployments=deployments,
        chains=chains,
        evaluator=evaluator,
    )

    with pytest.raises(KeyboardInterrupt):
        service.process(
            11, lease_token="lease", fencing_sequence=4, now=NOW
        )
    assert service.read_client.calls == 1
    assert evaluator.calls == 1
    assert deployments.evaluation.input_digest is not None

    # Repository lease-expiry recovery preserves the immutable checkpoint while
    # issuing a new token/fencing sequence.
    deployments.evaluation.status = "LEASED"
    deployments.lease_token = "new-lease"
    deployments.fencing_sequence = 5
    result = service.process(
        11, lease_token="new-lease", fencing_sequence=5, now=NOW
    )

    assert result.status == "ACTIONABLE"
    assert service.read_client.calls == 1
    assert evaluator.calls == 1
    assert len(risk.calls) == 1


def test_expired_recovery_checkpoint_blocks_without_recapture_or_no_action():
    deployments = FakeDeployments()
    chains = FakeChains(crash_after_signal=True)
    evaluator = FakeEvaluator(_signal("ACTIONABLE"))
    service, _, _, risk = _orchestrator(
        signal=None,
        deployments=deployments,
        chains=chains,
        evaluator=evaluator,
    )
    with pytest.raises(KeyboardInterrupt):
        service.process(
            11, lease_token="lease", fencing_sequence=4, now=NOW
        )
    deployments.lease_token = "new-lease"
    deployments.fencing_sequence = 5

    with pytest.raises(
        OkxDemoExecutionOrchestrationBlocked,
        match="expired",
    ):
        service.process(
            11,
            lease_token="new-lease",
            fencing_sequence=5,
            now=NOW + timedelta(minutes=2),
        )

    assert service.read_client.calls == 1
    assert evaluator.calls == 1
    assert risk.calls == []
    assert deployments.evaluation.status == "BLOCKED"


def test_signal_must_match_exact_evaluation_closed_candle():
    evaluator = FakeEvaluator(
        _signal_for_candle(
            "ACTIONABLE",
            NOW - timedelta(minutes=10),
        )
    )
    service, deployments, chains, risk = _orchestrator(
        signal=None,
        evaluator=evaluator,
    )

    with pytest.raises(
        OkxDemoExecutionOrchestrationBlocked,
        match="leased closed candle",
    ):
        service.process(
            11, lease_token="lease", fencing_sequence=4, now=NOW
        )

    assert deployments.evaluation.status == "BLOCKED"
    assert deployments.checkpoints == []
    assert chains.calls == []
    assert risk.calls == []


def test_actionable_completion_failure_blocks_already_risked_chain():
    deployments = FakeDeployments(fail_actionable_completion=True)
    service, _, chains, risk = _orchestrator(
        signal=_signal("ACTIONABLE"),
        deployments=deployments,
    )

    with pytest.raises(
        OkxDemoExecutionOrchestrationBlocked,
        match="lease was lost",
    ):
        service.process(
            11, lease_token="lease", fencing_sequence=4, now=NOW
        )

    assert len(risk.calls) == 1
    assert deployments.blocked_chains
    assert deployments.evaluation.status == "BLOCKED"
    assert chains.calls[-1][0:2] == ("complete", "RISK")


def test_failure_after_signal_success_terminally_blocks_partial_chain():
    service, deployments, chains, risk = _orchestrator(
        signal=_signal("ACTIONABLE")
    )
    original_loader = service._snapshot_loader

    def incomplete_snapshot_loader(bundle):
        snapshots = dict(original_loader(bundle))
        snapshots["account"] = {}
        return snapshots

    service._snapshot_loader = incomplete_snapshot_loader

    with pytest.raises(KeyError):
        service.process(
            11, lease_token="lease", fencing_sequence=4, now=NOW
        )

    assert ("complete", "SIGNAL") in [
        call[:2] for call in chains.calls
    ]
    assert deployments.blocked_chains
    assert deployments.evaluation.status == "FAILED"
    assert risk.calls == []


def test_conflicting_signal_fails_closed_and_marks_evaluation_blocked():
    evaluator = FakeEvaluator(
        error=BlueprintSignalEvaluationBlocked(
            "latest closed candle produces conflicting long and short entries"
        )
    )
    service, deployments, chains, risk = _orchestrator(
        signal=None,
        evaluator=evaluator,
    )

    with pytest.raises(OkxDemoExecutionOrchestrationBlocked):
        service.process(
            11, lease_token="lease", fencing_sequence=4, now=NOW
        )

    assert deployments.completions[0]["status"] == "BLOCKED"
    assert deployments.completions[0]["error_code"] == "DEMO_EXECUTION_BLOCKED"
    assert chains.calls == []
    assert risk.calls == []
