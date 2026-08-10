from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import pickle

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import (
    ApprovedExecution,
    BacktestResult,
    BacktestRun,
    BacktestTask,
    Base,
    ExchangeOrder,
    FullChainRun,
    FullChainSignalSnapshot,
    FullChainStageRun,
    OkxDemoAttestedSession,
    OkxDemoTrustedSnapshot,
    RiskBudget,
    RiskDecision,
    ResearchJob,
    ResearchJobAttempt,
    Strategy,
    StrategyCandidateApproval,
    StrategyScore,
    StrategyVersion,
    TradeIntent,
)
from app.models.execution_lineage import OKX_DEMO_TARGET_ID
from app.repositories.execution_lineage import ensure_execution_scope_catalog
from app.services.risk_chain import (
    RiskChainBlocked,
    RiskChainService,
    _issue_attested_session_capability,
    _normalize_attested_snapshot,
    _write_attested_snapshot,
    canonical_digest,
)
import app.services.risk_chain as risk_chain_module


@pytest.fixture()
def session_factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


def _seed_lineage(factory) -> dict[str, int]:
    with factory.begin() as db:
        ensure_execution_scope_catalog(db)
        strategy = Strategy(name="Risk chain", slug="risk-chain")
        db.add(strategy)
        db.flush()
        version = StrategyVersion(
            strategy_id=strategy.id,
            version_number=1,
            blueprint={},
            generated_code="class RiskChain: pass",
            file_path="/tmp/risk-chain.py",
            validation_status="passed",
        )
        db.add(version)
        db.flush()
        strategy.current_version_id = version.id
        run = BacktestRun(
            execution_scope_id="LOCAL_DRY_RUN",
            strategy_version_id=version.id,
            config_snapshot={},
            status="succeeded",
        )
        db.add(run)
        db.flush()
        task = BacktestTask(
            backtest_run_id=run.id,
            pair="BTC/USDT:USDT",
            timeframe="15m",
            status="succeeded",
        )
        db.add(task)
        db.flush()
        result = BacktestResult(
            backtest_run_id=run.id,
            backtest_task_id=task.id,
            result_path="/tmp/result.json",
            metrics_snapshot={},
        )
        db.add(result)
        db.flush()
        score = StrategyScore(
            strategy_id=strategy.id,
            strategy_version_id=version.id,
            backtest_result_id=result.id,
            scoring_version="risk-chain-v1",
            total_score=80,
        )
        db.add(score)
        db.flush()
        job = ResearchJob(
            execution_scope_id="LOCAL_DRY_RUN",
            job_type="deepseek_backtest",
            operation="strategy_generation.deepseek_backtest_loop",
            idempotency_key_digest=hashlib.sha256(
                "risk-job-{}".format(strategy.id).encode()
            ).hexdigest(),
            request_hash=hashlib.sha256(
                "risk-request-{}".format(strategy.id).encode()
            ).hexdigest(),
            request_payload={"allow_real_call": False},
            status="RUNNING",
            stage="RISK",
            attempt_count=1,
            max_attempts=1,
        )
        db.add(job)
        db.flush()
        attempt = ResearchJobAttempt(
            research_job_id=job.id,
            attempt_number=1,
            execution_scope_id="LOCAL_DRY_RUN",
            status="RUNNING",
        )
        db.add(attempt)
        db.flush()
        chain = FullChainRun(
            research_job_id=job.id,
            research_job_attempt_id=attempt.id,
            research_scope_id="LOCAL_DRY_RUN",
            execution_target_id=OKX_DEMO_TARGET_ID,
            status="EXECUTING",
            current_stage="RISK",
            strategy_id=strategy.id,
            strategy_version_id=version.id,
            backtest_run_id=run.id,
            backtest_task_id=task.id,
            backtest_result_id=result.id,
            strategy_score_id=score.id,
        )
        db.add(chain)
        db.flush()
        approval = StrategyCandidateApproval(
            full_chain_run_id=chain.id,
            execution_target_id=OKX_DEMO_TARGET_ID,
            strategy_version_id=version.id,
            backtest_result_id=result.id,
            strategy_score_id=score.id,
            candidate_digest=hashlib.sha256(
                "candidate-{}".format(chain.id).encode()
            ).hexdigest(),
            promotion_policy_version="strategy-promotion-v1",
            promotion_evidence={"eligible": True},
            status="APPROVED",
            requested_by="system:test",
            decided_by="system:test",
            decision_reason="Risk component fixture.",
            requested_at=datetime(2026, 7, 27, 11, tzinfo=timezone.utc),
            decided_at=datetime(2026, 7, 27, 11, tzinfo=timezone.utc),
            expires_at=datetime(2035, 1, 1, tzinfo=timezone.utc),
        )
        db.add(approval)
        db.flush()
        signal_digest = hashlib.sha256(
            "signal-{}".format(chain.id).encode()
        ).hexdigest()
        signal = FullChainSignalSnapshot(
            full_chain_run_id=chain.id,
            candidate_approval_id=approval.id,
            execution_target_id=OKX_DEMO_TARGET_ID,
            instrument_id="BTC-USDT-SWAP",
            signal_digest=signal_digest,
            source_type="api_aggregate",
            core_data=True,
            source_database_ids={"market_snapshot_id": 1},
            signal_snapshot={"side": "buy", "closed_candle": True},
            observed_at=datetime(2026, 7, 27, 11, tzinfo=timezone.utc),
            expires_at=datetime(2035, 1, 1, tzinfo=timezone.utc),
        )
        db.add(signal)
        db.flush()
        chain.candidate_approval_id = approval.id
        chain.signal_snapshot_id = signal.id
        db.add(
            FullChainStageRun(
                full_chain_run_id=chain.id,
                stage="SIGNAL",
                status="SUCCESS",
                idempotency_key_digest=hashlib.sha256(
                    "signal-stage-{}".format(chain.id).encode()
                ).hexdigest(),
                input_digest=hashlib.sha256(
                    "signal-input-{}".format(chain.id).encode()
                ).hexdigest(),
                input_snapshot={"instrument_id": "BTC-USDT-SWAP"},
                output_snapshot={"status": "succeeded"},
                database_ids={"signal_snapshot_id": signal.id},
                prepared_at=datetime(2026, 7, 27, 11, tzinfo=timezone.utc),
                completed_at=datetime(2026, 7, 27, 11, tzinfo=timezone.utc),
            )
        )
        db.flush()
        return {
            "strategy_id": strategy.id,
            "strategy_version_id": version.id,
            "backtest_run_id": run.id,
            "backtest_task_id": task.id,
            "backtest_result_id": result.id,
            "strategy_score_id": score.id,
            "_full_chain_run_id": chain.id,
            "_candidate_approval_id": approval.id,
            "_signal_snapshot_id": signal.id,
            "_signal_digest": signal.signal_digest,
        }


def _request(lineage: dict[str, int], now: datetime, factory=None) -> dict:
    expiry = (now + timedelta(minutes=5)).isoformat()
    instrument = {
        "execution_target": OKX_DEMO_TARGET_ID,
        "source": "okx_demo_rest",
        "resource": "instrument",
        "stale": False,
        "authenticated": False,
        "instId": "BTC-USDT-SWAP",
        "instrument_type": "SWAP",
        "ctVal": "1",
        "ctValCcy": "BTC",
        "lotSz": "0.01",
        "minSz": "0.01",
        "tickSz": "0.1",
        "contract_shape": "linear",
        "expires_at": expiry,
    }
    market = {
        "execution_target": OKX_DEMO_TARGET_ID,
        "source": "okx_demo_rest",
        "resource": "market",
        "stale": False,
        "authenticated": False,
        "instrument_id": "BTC-USDT-SWAP",
        "reference_price": "50000",
        "as_of": now.isoformat(),
        "expires_at": expiry,
    }
    account = {
        "execution_target": OKX_DEMO_TARGET_ID,
        "source": "okx_demo_rest",
        "resource": "account",
        "stale": False,
        "authenticated": True,
        "account_mode": "long_short_mode",
        "margin_mode": "isolated",
        "current_exposure": "0",
        "open_positions": 0,
        "exposure_by_position_side": {"long": "0", "short": "0"},
        "open_positions_by_position_side": {"long": 0, "short": 0},
        "leverage_by_position_side": {"long": "2", "short": "2"},
        "as_of": now.isoformat(),
        "expires_at": expiry,
    }
    def envelope(name: str, content: dict) -> dict:
        digest = canonical_digest(content)
        return {
            "ref": "{}:{}".format(name, digest[:24]),
            "digest": digest,
            "expires_at": expiry,
            "content": content,
        }

    request = {
        "execution_target": OKX_DEMO_TARGET_ID,
        "full_chain_run_id": lineage["_full_chain_run_id"],
        "candidate_approval_id": lineage["_candidate_approval_id"],
        "signal_snapshot_id": lineage["_signal_snapshot_id"],
        "signal_digest": lineage["_signal_digest"],
        "lineage": lineage,
        "snapshots": {
            name: envelope(name, content)
            for name, content in (
                ("instrument", instrument),
                ("market", market),
                ("account", account),
            )
        },
        "instrument_id": "BTC-USDT-SWAP",
        "side": "buy",
        "position_side": "long",
        "order_type": "limit",
        "quantity": "0.01",
        "limit_price": "50000",
        "reference_price": "50000",
        "leverage": "2",
        "margin_mode": "isolated",
        "stop_loss": "48000",
        "take_profit": "54000",
        "reduce_only": False,
        "llm_text": "this untrusted text must never authorize an order",
    }
    if factory is not None:
        _register_snapshots(factory, request, now)
    return request


def _register_snapshots(factory, request: dict, now: datetime) -> None:
    envelopes = request.pop("snapshots")
    snapshot_ids = {}
    expiries = [
        datetime.fromisoformat(
            envelope["content"]["expires_at"].replace("Z", "+00:00")
        )
        for envelope in envelopes.values()
    ]
    capability = _issue_attested_session_capability(
        attestation_hmac_key=b"t" * 32,
        pinned_fingerprint_sha256="f" * 64,
        created_at=min([now] + [expiry - timedelta(seconds=2) for expiry in expiries]),
        expires_at=max([now + timedelta(minutes=10)] + expiries),
    )
    with factory.begin() as db:
        for name, envelope in envelopes.items():
            expiry = datetime.fromisoformat(
                envelope["content"]["expires_at"].replace("Z", "+00:00")
            )
            observed_at = min(now, expiry - timedelta(seconds=1))
            normalized = _normalize_attested_snapshot(
                capability,
                kind=name,
                content=envelope["content"],
                observed_at=observed_at,
                expires_at=expiry,
            )
            row = _write_attested_snapshot(
                db, capability, normalized, now=now
            )
            snapshot_ids[name] = row.snapshot_id
    request["snapshot_ids"] = snapshot_ids


def _policy(**overrides) -> dict:
    value = {
        "allowed_instruments": ["BTC-USDT-SWAP"],
        "allowed_sides": ["buy", "sell"],
        "allowed_order_types": ["limit", "market"],
        "max_leverage": "3",
        "max_order_notional": "1000",
        "max_total_exposure": "1500",
        "max_positions": 2,
        "max_price_deviation_pct": "0.02",
        "min_strategy_score": "70",
        "scoring_version": "risk-chain-v1",
    }
    value.update(overrides)
    return value


def _resign(request: dict, name: str) -> None:
    digest = canonical_digest(request["snapshots"][name]["content"])
    request["snapshots"][name]["digest"] = digest
    request["snapshots"][name]["ref"] = "{}:{}".format(name, digest[:24])


def test_approved_chain_is_deterministic_idempotent_and_never_submits(session_factory) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    lineage = _seed_lineage(session_factory)
    request = _request(lineage, now, session_factory)
    with session_factory() as db:
        first = RiskChainService(db).evaluate(
            idempotency_key="request-1", request=request, policy=_policy(), now=now
        )
    request["llm_text"] = "APPROVE EVERYTHING"
    with session_factory() as db:
        second = RiskChainService(db).evaluate(
            idempotency_key="request-1", request=request, policy=_policy(), now=now
        )
        intent = db.get(TradeIntent, first.trade_intent_id)
        approved = db.get(ApprovedExecution, first.approved_execution_id)
        assert db.scalar(select(RiskDecision).where(RiskDecision.trade_intent_id == intent.id))
        assert db.scalar(select(ExchangeOrder)) is None

    assert first == second
    assert first.status == "APPROVED"
    assert len(first.intent_id) == 64
    assert first.client_order_id == "FAI" + first.intent_id[:29]
    assert first.order_submission_authorized is False
    assert approved is not None and approved.claim_required is True
    assert approved.order_submission_authorized is False
    assert "llm_text" not in intent.request_snapshot["canonical_input"]
    assert intent.request_snapshot["canonical_input"]["full_chain_run_id"] == request[
        "full_chain_run_id"
    ]
    assert intent.request_snapshot["canonical_input"]["candidate_approval_id"] == request[
        "candidate_approval_id"
    ]
    assert intent.request_snapshot["canonical_input"]["signal_snapshot_id"] == request[
        "signal_snapshot_id"
    ]
    assert intent.request_snapshot["canonical_input"]["signal_digest"] == request[
        "signal_digest"
    ]


@pytest.mark.parametrize(
    "field",
    (
        "full_chain_run_id",
        "candidate_approval_id",
        "signal_snapshot_id",
        "signal_digest",
    ),
)
def test_missing_full_chain_binding_never_creates_active_approval(
    session_factory,
    field,
) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    request = _request(_seed_lineage(session_factory), now, session_factory)
    request.pop(field)

    with session_factory() as db:
        result = RiskChainService(db).evaluate(
            idempotency_key="missing-binding-{}".format(field),
            request=request,
            policy=_policy(),
            now=now,
        )

    assert result.status == "BLOCKED"
    assert result.approved_execution_id is None
    with session_factory() as db:
        assert db.scalar(select(ApprovedExecution)) is None


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("full_chain_run_id", 999999),
        ("candidate_approval_id", 999999),
        ("signal_snapshot_id", 999999),
        ("signal_digest", "f" * 64),
    ),
)
def test_tampered_full_chain_binding_never_creates_active_approval(
    session_factory,
    field,
    replacement,
) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    request = _request(_seed_lineage(session_factory), now, session_factory)
    request[field] = replacement

    with session_factory() as db:
        result = RiskChainService(db).evaluate(
            idempotency_key="tampered-binding-{}".format(field),
            request=request,
            policy=_policy(),
            now=now,
        )

    assert result.status == "BLOCKED"
    assert result.approved_execution_id is None
    with session_factory() as db:
        assert db.scalar(select(ApprovedExecution)) is None


@pytest.mark.parametrize(
    ("model", "request_id_field"),
    (
        (StrategyCandidateApproval, "candidate_approval_id"),
        (FullChainSignalSnapshot, "signal_snapshot_id"),
    ),
)
def test_expired_full_chain_authority_never_creates_active_approval(
    session_factory,
    model,
    request_id_field,
) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    request = _request(_seed_lineage(session_factory), now, session_factory)
    with session_factory.begin() as db:
        row = db.get(model, request[request_id_field])
        assert row is not None
        row.expires_at = now

    with session_factory() as db:
        result = RiskChainService(db).evaluate(
            idempotency_key="expired-full-chain-{}".format(model.__tablename__),
            request=request,
            policy=_policy(),
            now=now,
        )

    assert result.status == "BLOCKED"
    assert result.approved_execution_id is None
    with session_factory() as db:
        assert db.scalar(select(ApprovedExecution)) is None


def test_idempotent_retry_and_claim_revoke_stale_session_permission(
    session_factory,
) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    lineage = _seed_lineage(session_factory)
    request = _request(lineage, now, session_factory)
    with session_factory() as db:
        first = RiskChainService(db).evaluate(
            idempotency_key="revoke-before-retry",
            request=request,
            policy=_policy(),
            now=now,
        )
    with session_factory.begin() as db:
        approved = db.get(ApprovedExecution, first.approved_execution_id)
        assert approved is not None
        snapshot = db.scalars(
            select(OkxDemoTrustedSnapshot).where(
                OkxDemoTrustedSnapshot.snapshot_id
                == approved.instrument_snapshot_id
            )
        ).one()
        session = db.get(OkxDemoAttestedSession, snapshot.attested_session_id)
        assert session is not None
        session.revoked_at = now + timedelta(seconds=1)
        session.revoke_reason = "IDENTITY_DRIFT"
        budget_before = db.get(RiskBudget, OKX_DEMO_TARGET_ID)
        assert budget_before is not None
        reserved_before = budget_before.reserved_notional
        reserved_permission = approved.reserved_notional
    with session_factory() as db:
        retry = RiskChainService(db).evaluate(
            idempotency_key="revoke-before-retry",
            request=request,
            policy=_policy(),
            now=now + timedelta(seconds=2),
        )
    assert retry.status == "BLOCKED"
    assert retry.approved_execution_id is None
    with session_factory() as db:
        assert (
            RiskChainService(db).claim_active_approval(
                first.approved_execution_id,
                now=now + timedelta(seconds=2),
            )
            is None
        )
    with session_factory() as db:
        assert db.get(ApprovedExecution, first.approved_execution_id) is None
        budget = db.get(RiskBudget, OKX_DEMO_TARGET_ID)
        assert budget is not None
        assert budget.reserved_notional == reserved_before - reserved_permission


@pytest.mark.parametrize(
    ("session_state", "expected_status"),
    [("REVOKED", "BLOCKED"), ("EXPIRED", "EXPIRED")],
)
def test_claim_active_approval_directly_invalidates_stale_session_atomically(
    session_factory,
    session_state,
    expected_status,
) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    lineage = _seed_lineage(session_factory)
    request = _request(lineage, now, session_factory)
    with session_factory() as db:
        result = RiskChainService(db).evaluate(
            idempotency_key="direct-claim-{}".format(session_state.lower()),
            request=request,
            policy=_policy(),
            now=now,
        )
    with session_factory.begin() as db:
        approved = db.get(ApprovedExecution, result.approved_execution_id)
        assert approved is not None
        snapshot = db.scalars(
            select(OkxDemoTrustedSnapshot).where(
                OkxDemoTrustedSnapshot.snapshot_id
                == approved.instrument_snapshot_id
            )
        ).one()
        session = db.get(
            OkxDemoAttestedSession,
            snapshot.attested_session_id,
        )
        budget = db.get(RiskBudget, OKX_DEMO_TARGET_ID)
        assert session is not None and budget is not None
        intent_id = approved.trade_intent_id
        decision_id = approved.risk_decision_id
        reserved_before = budget.reserved_notional
        positions_before = budget.approved_positions
        permission_reservation = approved.reserved_notional
        if session_state == "REVOKED":
            session.revoked_at = now + timedelta(seconds=1)
            session.revoke_reason = "IDENTITY_DRIFT"
    claim_now = (
        now + timedelta(seconds=2)
        if session_state == "REVOKED"
        else now + timedelta(minutes=11)
    )
    with session_factory() as db:
        assert (
            RiskChainService(db).claim_active_approval(
                result.approved_execution_id,
                now=claim_now,
            )
            is None
        )
    with session_factory() as db:
        intent = db.get(TradeIntent, intent_id)
        decision = db.get(RiskDecision, decision_id)
        budget = db.get(RiskBudget, OKX_DEMO_TARGET_ID)
        assert db.get(ApprovedExecution, result.approved_execution_id) is None
        assert intent is not None and intent.status == expected_status
        assert decision is not None and decision.decision == expected_status
        assert budget is not None
        assert budget.reserved_notional == reserved_before - permission_reservation
        assert budget.approved_positions == positions_before - 1


def test_idempotency_conflict_rolls_back_without_second_chain(session_factory) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    lineage = _seed_lineage(session_factory)
    request = _request(lineage, now, session_factory)
    with session_factory() as db:
        RiskChainService(db).evaluate(
            idempotency_key="request-1", request=request, policy=_policy(), now=now
        )
    request["quantity"] = "0.02"
    with session_factory() as db:
        conflict = RiskChainService(db).evaluate(
            idempotency_key="request-1", request=request, policy=_policy(), now=now
        )
    with session_factory() as db:
        assert len(db.scalars(select(TradeIntent)).all()) == 1
        assert len(db.scalars(select(RiskDecision)).all()) == 1
        assert conflict.status == "BLOCKED"
        assert db.scalar(select(ApprovedExecution)) is None


def test_inconsistent_lineage_is_blocked_and_atomic(session_factory) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    lineage = _seed_lineage(session_factory)
    lineage["strategy_version_id"] = 99999
    with session_factory() as db:
        blocked = RiskChainService(db).evaluate(
            idempotency_key="bad-lineage",
            request=_request(lineage, now, session_factory),
            policy=_policy(),
            now=now,
        )
    with session_factory() as db:
        assert blocked.status == "BLOCKED"
        assert db.scalar(select(TradeIntent)).status == "BLOCKED"
        assert db.scalar(select(RiskDecision)).decision == "BLOCKED"
        assert db.scalar(select(ApprovedExecution)) is None


def test_expired_evidence_persists_expired_decision_without_budget(session_factory) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    lineage = _seed_lineage(session_factory)
    request = _request(lineage, now)
    expired = (now - timedelta(seconds=1)).isoformat()
    request["snapshots"]["market"]["content"]["as_of"] = (
        now - timedelta(minutes=1)
    ).isoformat()
    request["snapshots"]["market"]["expires_at"] = expired
    request["snapshots"]["market"]["content"]["expires_at"] = expired
    _resign(request, "market")
    _register_snapshots(session_factory, request, now)
    with session_factory() as db:
        result = RiskChainService(db).evaluate(
            idempotency_key="expired", request=request, policy=_policy(), now=now
        )
    with session_factory() as db:
        assert result.status == "EXPIRED"
        assert result.approved_execution_id is None
        assert db.scalar(select(ApprovedExecution)) is None
        assert db.scalar(select(RiskBudget)) is None


@pytest.mark.parametrize(
    ("change", "expected_reason"),
    [
        ({"quantity": "0.03"}, "notional"),
        ({"limit_price": "52000"}, "deviation"),
        ({"stop_loss": "51000"}, "SL/TP"),
    ],
)
def test_policy_rejections_are_persisted_without_permission(
    session_factory, change, expected_reason
) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    lineage = _seed_lineage(session_factory)
    request = _request(lineage, now, session_factory)
    request.update(change)
    with session_factory() as db:
        result = RiskChainService(db).evaluate(
            idempotency_key="reject", request=request, policy=_policy(), now=now
        )
    with session_factory() as db:
        decision = db.get(RiskDecision, result.risk_decision_id)
        assert result.status == "REJECTED"
        assert result.approved_execution_id is None
        assert expected_reason in " ".join(decision.evidence_snapshot["reasons"])


def test_total_exposure_and_position_limit_use_locked_budget(session_factory) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    lineage = _seed_lineage(session_factory)
    with session_factory() as db:
        first = RiskChainService(db).evaluate(
            idempotency_key="budget-1",
            request=_request(lineage, now, session_factory),
            policy=_policy(max_total_exposure="500", max_positions=1),
            now=now,
        )
    with session_factory() as db:
        second = RiskChainService(db).evaluate(
            idempotency_key="budget-2",
            request=_request(lineage, now, session_factory),
            policy=_policy(max_total_exposure="500", max_positions=1),
            now=now,
        )
    with session_factory() as db:
        budget = db.get(RiskBudget, OKX_DEMO_TARGET_ID)
        assert first.status == "APPROVED"
        assert second.status == "REJECTED"
        assert budget.reserved_notional == Decimal("500")
        assert budget.approved_positions == 1


def test_missing_snapshot_is_blocked_without_partial_rows(session_factory) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    lineage = _seed_lineage(session_factory)
    request = _request(lineage, now)
    del request["snapshots"]["account"]
    _register_snapshots(session_factory, request, now)
    with session_factory() as db:
        blocked = RiskChainService(db).evaluate(
            idempotency_key="missing-snapshot",
            request=request,
            policy=_policy(),
            now=now,
        )
    with session_factory() as db:
        assert blocked.status == "BLOCKED"
        assert db.scalar(select(TradeIntent)).status == "BLOCKED"
        assert db.scalar(select(RiskDecision)).decision == "BLOCKED"
        assert db.scalar(select(RiskBudget)) is None


def test_snapshot_content_tampering_is_durable_blocked(session_factory) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    request = _request(_seed_lineage(session_factory), now)
    request["snapshots"]["market"]["content"]["reference_price"] = "1"
    _register_snapshots(session_factory, request, now)
    with session_factory() as db:
        result = RiskChainService(db).evaluate(
            idempotency_key="tampered", request=request, policy=_policy(), now=now
        )
    with session_factory() as db:
        intent = db.get(TradeIntent, result.trade_intent_id)
        assert result.status == "BLOCKED"
        assert intent.request_snapshot["blocked_input_redacted"] is True
        assert db.scalar(select(RiskBudget)) is None


def test_market_snapshot_future_binding_is_blocked_without_permission(
    session_factory,
) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    request = _request(_seed_lineage(session_factory), now)
    request["snapshots"]["market"]["content"]["as_of"] = (
        now + timedelta(seconds=1)
    ).isoformat()
    _resign(request, "market")
    _register_snapshots(session_factory, request, now)

    with session_factory() as db:
        result = RiskChainService(db).evaluate(
            idempotency_key="future-market-binding",
            request=request,
            policy=_policy(),
            now=now,
        )

    with session_factory() as db:
        decision = db.get(RiskDecision, result.risk_decision_id)
        assert result.status == "BLOCKED"
        assert result.approved_execution_id is None
        assert decision.evidence_snapshot["reasons"] == [
            "market snapshot binding is invalid"
        ]
        assert db.scalar(select(RiskBudget)) is None
        assert db.scalar(select(ExchangeOrder)) is None


def test_caller_cannot_self_sign_snapshot_content(session_factory) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    request = _request(_seed_lineage(session_factory), now)
    for envelope in request["snapshots"].values():
        envelope["digest"] = canonical_digest(envelope["content"])
        envelope["ref"] = "forged:{}".format(envelope["digest"][:32])
    with session_factory() as db:
        result = RiskChainService(db).evaluate(
            idempotency_key="self-signed",
            request=request,
            policy=_policy(),
            now=now,
        )
    assert result.status == "BLOCKED"
    assert result.approved_execution_id is None


def test_unknown_registry_snapshot_id_is_blocked(session_factory) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    request = _request(_seed_lineage(session_factory), now, session_factory)
    request["snapshot_ids"]["market"] = "market:" + "0" * 48
    with session_factory() as db:
        result = RiskChainService(db).evaluate(
            idempotency_key="unknown-registry-id",
            request=request,
            policy=_policy(),
            now=now,
        )
    assert result.status == "BLOCKED"


def test_registry_digest_mismatch_is_blocked(session_factory) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    request = _request(_seed_lineage(session_factory), now, session_factory)
    with session_factory.begin() as db:
        row = db.scalar(
            select(OkxDemoTrustedSnapshot).where(
                OkxDemoTrustedSnapshot.snapshot_id
                == request["snapshot_ids"]["market"]
            )
        )
        content = dict(row.content_json)
        content["reference_price"] = "1"
        row.content_json = content
    with session_factory() as db:
        result = RiskChainService(db).evaluate(
            idempotency_key="registry-digest-mismatch",
            request=request,
            policy=_policy(),
            now=now,
        )
    assert result.status == "BLOCKED"


def test_registry_content_and_digest_rewrite_cannot_forge_snapshot_id(
    session_factory,
) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    request = _request(_seed_lineage(session_factory), now, session_factory)
    with session_factory.begin() as db:
        row = db.scalar(
            select(OkxDemoTrustedSnapshot).where(
                OkxDemoTrustedSnapshot.snapshot_id
                == request["snapshot_ids"]["market"]
            )
        )
        content = dict(row.content_json)
        content["reference_price"] = "1"
        row.content_json = content
        row.digest = canonical_digest(content)
    with session_factory() as db:
        result = RiskChainService(db).evaluate(
            idempotency_key="registry-identity-forgery",
            request=request,
            policy=_policy(),
            now=now,
        )
    assert result.status == "BLOCKED"


@pytest.mark.parametrize(
    ("model", "field", "value"),
    [
        (StrategyVersion, "validation_status", "failed"),
        (BacktestRun, "status", "failed"),
        (BacktestTask, "status", "failed"),
    ],
)
def test_failed_lineage_never_executes(
    session_factory, model, field, value
) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    lineage = _seed_lineage(session_factory)
    with session_factory.begin() as db:
        row_id = {
            StrategyVersion: lineage["strategy_version_id"],
            BacktestRun: lineage["backtest_run_id"],
            BacktestTask: lineage["backtest_task_id"],
        }[model]
        setattr(db.get(model, row_id), field, value)
    with session_factory() as db:
        result = RiskChainService(db).evaluate(
            idempotency_key="failed-lineage",
            request=_request(lineage, now, session_factory),
            policy=_policy(),
            now=now,
        )
    assert result.status == "BLOCKED"
    assert result.approved_execution_id is None


def test_score_threshold_is_authorization_evidence(session_factory) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    request = _request(_seed_lineage(session_factory), now, session_factory)
    with session_factory() as db:
        result = RiskChainService(db).evaluate(
            idempotency_key="score-threshold",
            request=request,
            policy=_policy(min_strategy_score="90"),
            now=now,
        )
        decision = db.get(RiskDecision, result.risk_decision_id)
    assert result.status == "BLOCKED"
    assert "threshold" in decision.evidence_snapshot["reasons"][0]


@pytest.mark.parametrize(
    ("side", "position_side", "reduce_only"),
    [
        ("buy", "short", False),
        ("sell", "long", False),
        ("buy", "long", True),
        ("sell", "short", True),
    ],
)
def test_ambiguous_or_opposite_side_risk_request_is_blocked(
    session_factory, side, position_side, reduce_only
) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    request = _request(_seed_lineage(session_factory), now, session_factory)
    request.update(
        side=side,
        position_side=position_side,
        reduce_only=reduce_only,
    )

    with session_factory() as db:
        result = RiskChainService(db).evaluate(
            idempotency_key="direction-{}-{}-{}".format(side, position_side, reduce_only),
            request=request,
            policy=_policy(),
            now=now,
        )

    assert result.status == "BLOCKED"


def test_gross_long_and_short_exposure_cannot_net_to_zero(session_factory) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    lineage = _seed_lineage(session_factory)
    request = _request(lineage, now)
    account = request["snapshots"]["account"]["content"]
    account.update(
        current_exposure="1400",
        open_positions=2,
        exposure_by_position_side={"long": "700", "short": "700"},
        open_positions_by_position_side={"long": 1, "short": 1},
    )
    _resign(request, "account")
    _register_snapshots(session_factory, request, now)

    with session_factory() as db:
        result = RiskChainService(db).evaluate(
            idempotency_key="gross-exposure",
            request=request,
            policy=_policy(),
            now=now,
        )

    assert result.status == "REJECTED"


def test_policy_change_blocks_retry_and_revokes_old_permission(session_factory) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    request = _request(_seed_lineage(session_factory), now, session_factory)
    with session_factory() as db:
        first = RiskChainService(db).evaluate(
            idempotency_key="policy-change",
            request=request,
            policy=_policy(),
            now=now,
        )
    with session_factory() as db:
        second = RiskChainService(db).evaluate(
            idempotency_key="policy-change",
            request=request,
            policy=_policy(max_order_notional="100"),
            now=now,
        )
    with session_factory() as db:
        budget = db.get(RiskBudget, OKX_DEMO_TARGET_ID)
        assert first.status == "APPROVED"
        assert second.status == "BLOCKED"
        assert db.scalar(select(ApprovedExecution)) is None
        assert budget.reserved_notional == 0
        assert budget.approved_positions == 0


def test_retry_after_expiry_persists_expired_and_releases_budget(session_factory) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    request = _request(_seed_lineage(session_factory), now, session_factory)
    with session_factory() as db:
        first = RiskChainService(db).evaluate(
            idempotency_key="expires-later",
            request=request,
            policy=_policy(),
            now=now,
        )
    with session_factory() as db:
        expired = RiskChainService(db).evaluate(
            idempotency_key="expires-later",
            request=request,
            policy=_policy(),
            now=now + timedelta(minutes=6),
        )
    with session_factory() as db:
        budget = db.get(RiskBudget, OKX_DEMO_TARGET_ID)
        assert first.status == "APPROVED"
        assert expired.status == "EXPIRED"
        assert db.scalar(select(ApprovedExecution)) is None
        assert db.get(RiskDecision, expired.risk_decision_id).decision == "EXPIRED"
        assert budget.reserved_notional == 0
        assert budget.approved_positions == 0


def test_secret_like_snapshot_reference_is_redacted_from_database(session_factory) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    request = _request(_seed_lineage(session_factory), now)
    request["snapshots"]["account"]["ref"] = "api_key:do-not-store"
    with session_factory() as db:
        result = RiskChainService(db).evaluate(
            idempotency_key="unsafe-ref",
            request=request,
            policy=_policy(),
            now=now,
        )
    with session_factory() as db:
        stored = str(db.get(TradeIntent, result.trade_intent_id).request_snapshot)
        assert result.status == "BLOCKED"
        assert "do-not-store" not in stored


def test_market_order_rejects_limit_price_combination(session_factory) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    request = _request(_seed_lineage(session_factory), now, session_factory)
    request["order_type"] = "market"
    with session_factory() as db:
        result = RiskChainService(db).evaluate(
            idempotency_key="market-with-price",
            request=request,
            policy=_policy(),
            now=now,
        )
    assert result.status == "BLOCKED"


def test_inverse_notional_uses_contract_face_value_and_account_baseline(
    session_factory,
) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    request = _request(_seed_lineage(session_factory), now)
    instrument = request["snapshots"]["instrument"]["content"]
    instrument.update(
        {
            "ctVal": "100",
            "ctValCcy": "USD",
            "lotSz": "1",
            "minSz": "1",
            "contract_shape": "inverse",
        }
    )
    _resign(request, "instrument")
    account = request["snapshots"]["account"]["content"]
    account["current_exposure"] = "100"
    account["open_positions"] = 1
    account["exposure_by_position_side"] = {"long": "100", "short": "0"}
    account["open_positions_by_position_side"] = {"long": 1, "short": 0}
    _resign(request, "account")
    request["quantity"] = "2"
    _register_snapshots(session_factory, request, now)
    with session_factory() as db:
        result = RiskChainService(db).evaluate(
            idempotency_key="inverse",
            request=request,
            policy=_policy(
                max_order_notional="500",
                max_total_exposure="500",
                max_positions=3,
            ),
            now=now,
        )
    with session_factory() as db:
        budget = db.get(RiskBudget, OKX_DEMO_TARGET_ID)
        approval = db.get(ApprovedExecution, result.approved_execution_id)
        assert result.status == "APPROVED"
        assert approval.reserved_notional == Decimal("200")
        assert budget.reserved_notional == Decimal("300")
        assert budget.approved_positions == 2


def test_policy_digest_null_cannot_downgrade_risk_v1_row(session_factory) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    request = _request(_seed_lineage(session_factory), now, session_factory)
    with session_factory() as db:
        result = RiskChainService(db).evaluate(
            idempotency_key="no-null-downgrade",
            request=request,
            policy=_policy(),
            now=now,
        )
    with pytest.raises(IntegrityError):
        with session_factory.begin() as db:
            intent = db.get(TradeIntent, result.trade_intent_id)
            intent.policy_digest = None
            intent.side = "hold"


def test_snapshot_recorder_is_not_public_and_capability_is_not_serializable() -> None:
    assert not hasattr(risk_chain_module, "record_attested_snapshot")
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    capability = _issue_attested_session_capability(
        attestation_hmac_key=b"t" * 32,
        pinned_fingerprint_sha256="a" * 64,
        created_at=now,
        expires_at=now + timedelta(minutes=1),
    )
    with pytest.raises(TypeError, match="cannot be serialized"):
        pickle.dumps(capability)
    with pytest.raises(TypeError, match="private"):
        type(capability)(object(), capability._identity, "forged-proof")


def test_only_trusted_market_candles_may_use_the_exchange_window_limit() -> None:
    for root in ("market trusted snapshot", "market snapshot"):
        risk_chain_module._reject_unsafe_content(
            {"confirmed_candles": [0] * 300},
            root,
        )

        with pytest.raises(
            RiskChainBlocked,
            match="confirmed_candles is too large",
        ):
            risk_chain_module._reject_unsafe_content(
                {"confirmed_candles": [0] * 301},
                root,
            )
    with pytest.raises(RiskChainBlocked, match="other_rows is too large"):
        risk_chain_module._reject_unsafe_content(
            {"other_rows": [0] * 101},
            "market snapshot",
        )
    with pytest.raises(RiskChainBlocked, match="confirmed_candles is too large"):
        risk_chain_module._reject_unsafe_content(
            {"confirmed_candles": [0] * 101},
            "account snapshot",
        )


def test_risk_chain_accepts_persisted_202_candle_market_snapshot(
    session_factory,
) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    lineage = _seed_lineage(session_factory)
    request = _request(lineage, now)
    market = request["snapshots"]["market"]
    market["content"]["confirmed_candles"] = [0] * 202
    _resign(request, "market")
    _register_snapshots(session_factory, request, now)

    with session_factory() as db:
        result = RiskChainService(db).evaluate(
            idempotency_key="request-202-candles",
            request=request,
            policy=_policy(),
            now=now,
        )

    assert result.status == "APPROVED"


def test_expired_attested_capability_cannot_normalize_or_write() -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    capability = _issue_attested_session_capability(
        attestation_hmac_key=b"t" * 32,
        pinned_fingerprint_sha256="a" * 64,
        created_at=now - timedelta(minutes=2),
        expires_at=now - timedelta(minutes=1),
    )
    with pytest.raises(Exception, match="expired or revoked"):
        _normalize_attested_snapshot(
            capability,
            kind="market",
            content={},
            observed_at=now,
            expires_at=now + timedelta(minutes=1),
        )
