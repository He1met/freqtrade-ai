from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import select

from app.canonical_v13.accounting import post_production_demo_ledger_entry
from app.canonical_v13.continuous_demo_execution import (
    extract_strategy_exit_after_seconds,
    grant_continuous_open,
    grant_position_exit,
)
from app.canonical_v13.execution_common import canonical_execution_digest
from app.canonical_v13.fill_service import record_production_demo_fill
from app.canonical_v13.models import (
    DEPLOYMENT_APPROVALS_TABLE,
    DEPLOYMENTS_TABLE,
    EXECUTION_ATTESTATIONS_TABLE,
    ORDERS_TABLE,
    QUALIFICATION_DECISIONS_TABLE,
    RESEARCH_TARGETS_TABLE,
    SIGNALS_TABLE,
    STRATEGY_ARTIFACTS_TABLE,
    STRATEGY_VERSIONS_TABLE,
    TRADE_INTENTS_TABLE,
)
from app.canonical_v13.phase9_okx_demo import CanonicalOkxDemoSession
from app.canonical_v13.reconciliation import reconcile_production_demo_chain
from app.canonical_v13.risk_service import (
    INTENT_MODE_CONTINUOUS_OPEN,
    INTENT_MODE_POSITION_EXIT,
)
from tests.test_canonical_v13_phase9_canary_policy import _fixture
from tests.test_canonical_v13_phase9_execution_authority import canonical_connection  # noqa: F401, F811
from tests.test_canonical_v13_phase9_okx_demo import FakeRead, FakeWrite
from tests.test_canonical_v13_research_evaluation import NOW


STRATEGY_SOURCE = """from freqtrade.strategy import IStrategy
class ContinuousDemoStrategy(IStrategy):
    can_short = False
    timeframe = "15m"
    def leverage(self, pair, current_time, current_rate, proposed_leverage, max_leverage, entry_tag, side, **kwargs):
        return min(12.0, max_leverage)
    def custom_exit(self, pair, trade, current_time, current_rate, current_profit, **kwargs):
        exposure_seconds = (current_time - trade.open_date_utc).total_seconds()
        if exposure_seconds >= 20 * 60 * 60:
            return "time_exit"
        return None
"""


def _rich_natural_signal(connection):
    decision, _approval, _probe, receipt = _fixture(connection)
    deployment = connection.execute(select(DEPLOYMENTS_TABLE)).mappings().one()
    approval = connection.execute(select(DEPLOYMENT_APPROVALS_TABLE)).mappings().one()
    qualification = connection.execute(
        select(QUALIFICATION_DECISIONS_TABLE).where(
            QUALIFICATION_DECISIONS_TABLE.c.id == decision["id"]
        )
    ).mappings().one()
    version = connection.execute(
        select(STRATEGY_VERSIONS_TABLE).where(
            STRATEGY_VERSIONS_TABLE.c.id == qualification["strategy_version_id"]
        )
    ).mappings().one()
    digest = sha256(STRATEGY_SOURCE.encode()).hexdigest()
    connection.execute(
        STRATEGY_ARTIFACTS_TABLE.update()
        .where(STRATEGY_ARTIFACTS_TABLE.c.id == version["artifact_id"])
        .values(
            normalized_content=STRATEGY_SOURCE,
            content_digest=digest,
            size_bytes=len(STRATEGY_SOURCE.encode()),
        )
    )
    connection.execute(
        RESEARCH_TARGETS_TABLE.update()
        .where(RESEARCH_TARGETS_TABLE.c.id == qualification["research_target_id"])
        .values(timeframe="15m")
    )
    signal = connection.execute(select(SIGNALS_TABLE)).mappings().one()
    evidence = {
        "evidence_class": "PRODUCTION_OKX_DEMO",
        "natural_signal": True,
        "allow_real_funds": False,
        "qualification_decision_id": str(qualification["id"]),
        "qualification_decision_digest": qualification["decision_digest"],
        "deployment_approval_id": str(approval["id"]),
        "deployment_approval_digest": approval["approval_digest"],
        "deployment_id": str(deployment["id"]),
        "deployment_capability_digest": deployment["capability_digest"],
        "strategy_version_id": str(version["id"]),
        "research_target_id": str(qualification["research_target_id"]),
        "configuration_bundle_digest": deployment["configuration_bundle_digest"],
        "market_snapshot_digest": deployment["market_snapshot_digest"],
        "instrument": "BTC-USDT-SWAP",
        "evaluated_at": (NOW + timedelta(seconds=1)).isoformat(),
        "evaluation": {
            "direction": "LONG",
            "closed_candle": True,
            "artifact_digest": digest,
        },
    }
    connection.execute(
        SIGNALS_TABLE.update()
        .where(SIGNALS_TABLE.c.id == signal["id"])
        .values(
            signal_json=evidence,
            signal_digest=canonical_execution_digest(evidence),
            created_at=NOW + timedelta(seconds=1),
        )
    )
    return signal["id"], receipt.probe_receipt_id


def _exit_guard():
    read = FakeRead()
    for snapshot in read.snapshots.values():
        snapshot.metadata.fetched_at = NOW - timedelta(seconds=2)
        snapshot.metadata.expires_at = NOW + timedelta(seconds=30)
        if snapshot.metadata.exchange_timestamp is not None:
            snapshot.metadata.exchange_timestamp = NOW - timedelta(seconds=1)
    read.snapshots["mark_price"].items[0]["timestamp"] = NOW - timedelta(seconds=1)
    read.snapshots["positions"] = read._snapshot(
        "positions",
        [
            {
                "inst_id": "BTC-USDT-SWAP",
                "margin_mode": "isolated",
                "position_side": "long",
                "contracts": "1",
                "available_contracts": "1",
                "average_price": "9900",
                "mark_price": "10000.1",
                "leverage": "12",
                "unrealized_pnl": "1",
                "timestamp": NOW - timedelta(days=1),
            }
        ],
        authenticated=True,
        exchange_timestamp=NOW - timedelta(days=1),
    )
    read.snapshots["positions"].metadata.fetched_at = NOW - timedelta(seconds=2)
    read.snapshots["positions"].metadata.expires_at = NOW + timedelta(seconds=30)
    for item in read.snapshots["leverage"].items:
        if item["position_side"] == "long":
            item["leverage"] = "12"
    session = CanonicalOkxDemoSession(
        read_client=read,
        write_port=FakeWrite(),
        account_fingerprint_digest="d" * 64,
        credential_generation_digest="e" * 64,
        close_callback=lambda: None,
        now_provider=lambda: NOW,
    )
    return session.exit_guard(instrument="BTC-USDT-SWAP", expected_contracts="1")


def test_exit_threshold_parser_is_exact():
    assert extract_strategy_exit_after_seconds(STRATEGY_SOURCE) == 20 * 60 * 60


def test_natural_signal_open_grant_is_independent_and_replay_noop(  # noqa: F811
    canonical_connection,  # noqa: F811
):
    with canonical_connection.begin():
        signal_id, probe_id = _rich_natural_signal(canonical_connection)
        first = grant_continuous_open(
            canonical_connection,
            signal_id=signal_id,
            probe_receipt_id=probe_id,
            evaluated_at=NOW + timedelta(seconds=2),
        )
        replay = grant_continuous_open(
            canonical_connection,
            signal_id=signal_id,
            probe_receipt_id=probe_id,
            evaluated_at=NOW + timedelta(seconds=3),
        )
        intent = canonical_connection.execute(
            select(TRADE_INTENTS_TABLE).where(
                TRADE_INTENTS_TABLE.c.id == first.trade_intent_id
            )
        ).mappings().one()
    assert first.decision_mode == INTENT_MODE_CONTINUOUS_OPEN
    assert replay.risk_decision_id == first.risk_decision_id
    assert replay.grant_digest == first.grant_digest
    assert replay.repeat_noop is True
    assert intent["intent_json"]["exchange_body"]["side"] == "buy"
    assert intent["intent_json"]["notional"] == "100.001"
    assert "hour" not in str(intent["intent_json"]).lower()
    assert "day" not in str(intent["intent_json"]).lower()


def test_reconciled_entry_can_grant_one_exact_market_exit(  # noqa: F811
    canonical_connection,  # noqa: F811
):
    with canonical_connection.begin():
        signal_id, probe_id = _rich_natural_signal(canonical_connection)
        opening = grant_continuous_open(
            canonical_connection,
            signal_id=signal_id,
            probe_receipt_id=probe_id,
            evaluated_at=NOW + timedelta(seconds=2),
        )
        order_id = uuid4()
        canonical_connection.execute(
            ORDERS_TABLE.insert().values(
                id=order_id,
                risk_decision_id=opening.risk_decision_id,
                writer_identity="canonical_order_writer",
                idempotency_key="continuous-entry-complete",
                exchange_order_id="entry-order-1",
                status="ACCEPTED",
                demo_only=True,
                allow_real_funds=False,
                request_digest="1" * 64,
                receipt_digest="2" * 64,
                created_at=NOW - timedelta(hours=21),
            )
        )
        fill_id = record_production_demo_fill(
            canonical_connection,
            order_id=order_id,
            exchange_fill_id="entry-fill-1",
            fill_json={
                "contract": "canonical-v13-okx-demo-fill-evidence-v1",
                "evidence_class": "PRODUCTION_OKX_DEMO",
                "allow_real_funds": False,
                "instrument": "BTC-USDT-SWAP",
                "exchange_order_id": "entry-order-1",
                "exchange_fill_id": "entry-fill-1",
                "price": "10000",
                "size": "1",
                "timestamp": str(int((NOW - timedelta(hours=21)).timestamp() * 1000)),
                "side": "buy",
                "position_side": "long",
                "requested_size": "1",
            },
        )
        ledger_id = post_production_demo_ledger_entry(
            canonical_connection,
            fill_id=fill_id,
            entry_key="continuous-entry-fill-1:long-contracts",
            asset="BTC-USDT-SWAP",
            amount=Decimal("1"),
            entry_type="OKX_DEMO_LONG_FILL_CONTRACTS",
        )
        reconcile_production_demo_chain(
            canonical_connection,
            order_id=order_id,
            fill_id=fill_id,
            ledger_entry_id=ledger_id,
        )
        guard = _exit_guard()
        attestation_id = canonical_connection.execute(
            select(EXECUTION_ATTESTATIONS_TABLE.c.id)
        ).scalar_one()
        attestation = canonical_connection.execute(
            select(EXECUTION_ATTESTATIONS_TABLE).where(
                EXECUTION_ATTESTATIONS_TABLE.c.id == attestation_id
            )
        ).mappings().one()
        assert attestation["account_fingerprint_digest"] == guard.account_fingerprint_digest
        assert attestation["credential_generation_digest"] == guard.credential_generation_digest
        assert attestation["observed_at"].replace(tzinfo=guard.observed_at.tzinfo) <= guard.observed_at
        assert attestation["expires_at"].replace(tzinfo=guard.expires_at.tzinfo) >= guard.expires_at
        closing = grant_position_exit(
            canonical_connection,
            entry_order_id=order_id,
            attestation_id=attestation_id,
            guard=guard,
            evaluated_at=NOW,
        )
        replay = grant_position_exit(
            canonical_connection,
            entry_order_id=order_id,
            attestation_id=attestation_id,
            guard=guard,
            evaluated_at=NOW,
        )
        intent = canonical_connection.execute(
            select(TRADE_INTENTS_TABLE).where(
                TRADE_INTENTS_TABLE.c.id == closing.trade_intent_id
            )
        ).mappings().one()
    body = intent["intent_json"]["exchange_body"]
    assert closing.decision_mode == INTENT_MODE_POSITION_EXIT
    assert replay.risk_decision_id == closing.risk_decision_id
    assert replay.repeat_noop is True
    assert body["side"] == "sell"
    assert body["posSide"] == "long"
    assert body["ordType"] == "market"
    assert "px" not in body
