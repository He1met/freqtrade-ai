# ruff: noqa: F401, F811
from __future__ import annotations

from datetime import timedelta
from hashlib import sha256
from uuid import uuid4

import pytest
from app.canonical_v13.execution_common import CanonicalExecutionChainBlocked
from app.canonical_v13.models import (
    DEPLOYMENT_APPROVALS_TABLE,
    EXECUTION_CANARY_PROBE_RECEIPTS_TABLE,
    EXECUTION_CANARY_RISK_POLICIES_TABLE,
    QUALIFICATION_DECISIONS_TABLE,
    RESEARCH_TARGETS_TABLE,
    STRATEGY_ARTIFACTS_TABLE,
    STRATEGY_VERSIONS_TABLE,
)
from app.canonical_v13.phase9_canary_policy import (
    authorize_canary_risk_policy,
    persist_canary_probe_receipt,
)
from app.canonical_v13.phase9_execution_authority import (
    authorize_demo_risk_budget,
    record_redacted_demo_attestation,
)
from app.canonical_v13.phase9_okx_demo import CanonicalOkxDemoSession
from sqlalchemy import func, select
from tests.test_canonical_v13_phase9_execution_authority import _production_chain
from tests.test_canonical_v13_phase9_okx_demo import FakeRead, FakeWrite
from tests.test_canonical_v13_research_evaluation import NOW, canonical_connection

STRATEGY_SOURCE = """from freqtrade.strategy import IStrategy
class ExactCanaryStrategy(IStrategy):
    can_short = False
    def leverage(self, pair, current_time, current_rate, proposed_leverage, max_leverage, entry_tag, side, **kwargs):
        return min(12.0, max_leverage)
"""


def _sealed_probe(
    *,
    exchange_max_leverage: str = "20",
    current_long_leverage: str = "12",
    now=NOW,
):
    read = FakeRead()
    for snapshot in read.snapshots.values():
        snapshot.metadata.fetched_at = now - timedelta(seconds=2)
        snapshot.metadata.expires_at = now + timedelta(seconds=30)
        if snapshot.metadata.exchange_timestamp is not None:
            snapshot.metadata.exchange_timestamp = now - timedelta(seconds=1)
    read.snapshots["mark_price"].items[0]["timestamp"] = now - timedelta(seconds=1)
    read.snapshots["exchange_max_leverage"].items[0]["max_leverage"] = (
        exchange_max_leverage
    )
    for item in read.snapshots["leverage"].items:
        if item["position_side"] == "long":
            item["leverage"] = current_long_leverage
    read.snapshots["maximum_order_quantity"].items[0]["leverage"] = (
        current_long_leverage
    )
    session = CanonicalOkxDemoSession(
        read_client=read,
        write_port=FakeWrite(),
        account_fingerprint_digest="d" * 64,
        credential_generation_digest="e" * 64,
        close_callback=lambda: None,
        now_provider=lambda: now,
    )
    return session.probe(instrument="BTC-USDT-SWAP")


def _fixture(
    connection,
    *,
    exchange_max_leverage: str = "20",
    current_long_leverage: str = "12",
):
    approval, deployment, _runtime, _intent, _launcher = _production_chain(
        connection, create_intent=False
    )
    approval_row = (
        connection.execute(
            select(DEPLOYMENT_APPROVALS_TABLE).where(
                DEPLOYMENT_APPROVALS_TABLE.c.id == approval.deployment_approval_id
            )
        )
        .mappings()
        .one()
    )
    decision = (
        connection.execute(
            select(QUALIFICATION_DECISIONS_TABLE).where(
                QUALIFICATION_DECISIONS_TABLE.c.id
                == approval_row["qualification_decision_id"]
            )
        )
        .mappings()
        .one()
    )
    version = (
        connection.execute(
            select(STRATEGY_VERSIONS_TABLE).where(
                STRATEGY_VERSIONS_TABLE.c.id == decision["strategy_version_id"]
            )
        )
        .mappings()
        .one()
    )
    artifact_digest = sha256(STRATEGY_SOURCE.encode()).hexdigest()
    connection.execute(
        STRATEGY_ARTIFACTS_TABLE.update()
        .where(STRATEGY_ARTIFACTS_TABLE.c.id == version["artifact_id"])
        .values(
            normalized_content=STRATEGY_SOURCE,
            content_digest=artifact_digest,
            size_bytes=len(STRATEGY_SOURCE.encode()),
        )
    )
    probe = _sealed_probe(
        exchange_max_leverage=exchange_max_leverage,
        current_long_leverage=current_long_leverage,
    )
    attestation = record_redacted_demo_attestation(
        connection,
        deployment_id=deployment.deployment_id,
        instrument=probe.instrument,
        account_fingerprint_digest=probe.account_fingerprint_digest,
        credential_generation_digest=probe.credential_generation_digest,
        permissions=probe.permissions,
        observed_at=probe.observed_at,
        expires_at=probe.expires_at,
        evaluated_at=NOW,
    )
    receipt = persist_canary_probe_receipt(
        connection,
        probe=probe,
        deployment_id=deployment.deployment_id,
        execution_attestation_id=attestation.attestation_id,
        evaluated_at=NOW,
    )
    return decision, approval, probe, receipt


def _authorize(
    connection,
    decision,
    approval,
    receipt,
    *,
    evaluated_at=NOW,
    probe_receipt_id=None,
    idempotency_key="exact-canary-policy",
):
    return authorize_canary_risk_policy(
        connection,
        qualification_decision_id=decision["id"],
        deployment_approval_id=approval.deployment_approval_id,
        probe_receipt_id=probe_receipt_id or receipt.probe_receipt_id,
        actor_identity="phase9-human-policy-owner",
        idempotency_key=idempotency_key,
        reason="one reviewed canonical Demo canary",
        evaluated_at=evaluated_at,
    )


def test_sealed_probe_persists_then_authorizes_exact_one_shot_policy(
    canonical_connection,
):
    with canonical_connection.begin():
        decision, approval, _probe, receipt = _fixture(canonical_connection)
        persisted = (
            canonical_connection.execute(select(EXECUTION_CANARY_PROBE_RECEIPTS_TABLE))
            .mappings()
            .one()
        )
        result = _authorize(canonical_connection, decision, approval, receipt)
        budget = authorize_demo_risk_budget(
            canonical_connection,
            deployment_approval_id=approval.deployment_approval_id,
            actor_identity="phase9-human-policy-owner",
            reason="freeze the exact one-shot canary policy",
            policy_source_receipt_digest=result.receipt_digest,
            evaluated_at=NOW,
        )
        policy = (
            canonical_connection.execute(select(EXECUTION_CANARY_RISK_POLICIES_TABLE))
            .mappings()
            .one()
        )
    assert str(result.max_notional) == "100.001"
    assert result.effective_leverage == 12
    assert result.expires_at - result.accepted_at == timedelta(minutes=30)
    assert policy["probe_receipt_id"] == receipt.probe_receipt_id
    assert policy["metadata_receipt_digest"] == persisted["instrument_digest"]
    assert policy["mark_price_receipt_digest"] == persisted["mark_price_digest"]
    assert policy["position_policy"] == "LONG_ONLY"
    assert policy["max_order_count"] == 1
    assert policy["allow_real_funds"] is False
    assert budget.repeat_noop is False


def test_exchange_max_below_strategy_cap_is_effective_leverage(canonical_connection):
    with canonical_connection.begin():
        decision, approval, _probe, receipt = _fixture(
            canonical_connection,
            exchange_max_leverage="5",
            current_long_leverage="5",
        )
        result = _authorize(canonical_connection, decision, approval, receipt)
    assert result.effective_leverage == 5


def test_current_long_leverage_above_effective_cap_blocks(canonical_connection):
    with canonical_connection.begin():
        decision, approval, _probe, receipt = _fixture(
            canonical_connection,
            exchange_max_leverage="20",
            current_long_leverage="13",
        )
        with pytest.raises(
            CanonicalExecutionChainBlocked,
            match="BLOCKED_CANARY_CURRENT_LEVERAGE_EXCEEDS_POLICY",
        ):
            _authorize(canonical_connection, decision, approval, receipt)


def test_current_long_leverage_is_frozen_as_effective_leverage(canonical_connection):
    with canonical_connection.begin():
        decision, approval, _probe, receipt = _fixture(
            canonical_connection,
            exchange_max_leverage="20",
            current_long_leverage="2",
        )
        result = _authorize(canonical_connection, decision, approval, receipt)
    assert result.effective_leverage == 2


def test_probe_and_policy_exact_replays_are_noops_and_policy_drift_blocks(
    canonical_connection,
):
    with canonical_connection.begin():
        decision, approval, probe, receipt = _fixture(canonical_connection)
        persisted = (
            canonical_connection.execute(select(EXECUTION_CANARY_PROBE_RECEIPTS_TABLE))
            .mappings()
            .one()
        )
        persisted_replay = persist_canary_probe_receipt(
            canonical_connection,
            probe=probe,
            deployment_id=persisted["deployment_id"],
            execution_attestation_id=persisted["execution_attestation_id"],
            evaluated_at=NOW,
        )
        first = _authorize(canonical_connection, decision, approval, receipt)
        replay = _authorize(
            canonical_connection,
            decision,
            approval,
            receipt,
            evaluated_at=NOW + timedelta(hours=1),
        )
        with pytest.raises(
            CanonicalExecutionChainBlocked, match="BLOCKED_CANARY_POLICY_REPLAY_DRIFT"
        ):
            _authorize(
                canonical_connection,
                decision,
                approval,
                receipt,
                evaluated_at=NOW + timedelta(hours=1),
                probe_receipt_id=uuid4(),
            )
        count = canonical_connection.execute(
            select(func.count()).select_from(EXECUTION_CANARY_RISK_POLICIES_TABLE)
        ).scalar_one()
    assert persisted_replay.repeat_noop is True
    assert replay.repeat_noop is True
    assert replay.policy_id == first.policy_id
    assert replay.receipt_digest == first.receipt_digest
    assert count == 1


def test_expired_policy_without_budget_is_append_only_renewable(
    canonical_connection,
):
    renewed_at = NOW + timedelta(minutes=31)
    with canonical_connection.begin():
        decision, approval, probe, receipt = _fixture(canonical_connection)
        first = _authorize(canonical_connection, decision, approval, receipt)
        fresh_probe = _sealed_probe(now=renewed_at)
        attestation = record_redacted_demo_attestation(
            canonical_connection,
            deployment_id=(
                canonical_connection.execute(
                    select(EXECUTION_CANARY_PROBE_RECEIPTS_TABLE.c.deployment_id).where(
                        EXECUTION_CANARY_PROBE_RECEIPTS_TABLE.c.id
                        == receipt.probe_receipt_id
                    )
                ).scalar_one()
            ),
            instrument=fresh_probe.instrument,
            account_fingerprint_digest=fresh_probe.account_fingerprint_digest,
            credential_generation_digest=fresh_probe.credential_generation_digest,
            permissions=fresh_probe.permissions,
            observed_at=fresh_probe.observed_at,
            expires_at=fresh_probe.expires_at,
            evaluated_at=renewed_at,
        )
        fresh_receipt = persist_canary_probe_receipt(
            canonical_connection,
            probe=fresh_probe,
            deployment_id=(
                canonical_connection.execute(
                    select(EXECUTION_CANARY_PROBE_RECEIPTS_TABLE.c.deployment_id).where(
                        EXECUTION_CANARY_PROBE_RECEIPTS_TABLE.c.id
                        == receipt.probe_receipt_id
                    )
                ).scalar_one()
            ),
            execution_attestation_id=attestation.attestation_id,
            evaluated_at=renewed_at,
        )
        renewed = _authorize(
            canonical_connection,
            decision,
            approval,
            fresh_receipt,
            evaluated_at=renewed_at,
            idempotency_key="renewed-canary-policy",
        )
        rows = (
            canonical_connection.execute(
                select(EXECUTION_CANARY_RISK_POLICIES_TABLE).order_by(
                    EXECUTION_CANARY_RISK_POLICIES_TABLE.c.accepted_at
                )
            )
            .mappings()
            .all()
        )
    assert renewed.policy_id != first.policy_id
    assert [row["status"] for row in rows] == ["EXPIRED", "ACTIVE"]
    assert rows[0]["termination_digest"] is not None


def test_fresh_active_policy_blocks_different_idempotency_key(canonical_connection):
    with canonical_connection.begin():
        decision, approval, _probe, receipt = _fixture(canonical_connection)
        _authorize(canonical_connection, decision, approval, receipt)
        with pytest.raises(
            CanonicalExecutionChainBlocked, match="BLOCKED_CANARY_POLICY_ACTIVE"
        ):
            _authorize(
                canonical_connection,
                decision,
                approval,
                receipt,
                idempotency_key="different-active-policy",
            )


@pytest.mark.parametrize(
    ("column", "value", "reason"),
    (
        ("instrument_digest", "f" * 64, "BLOCKED_CANARY_PROBE_RESOURCE_DIGEST"),
        ("safe_facts_digest", "f" * 64, "BLOCKED_CANARY_PROBE_FACTS_DRIFT"),
        ("receipt_digest", "f" * 64, "BLOCKED_CANARY_PROBE_RECEIPT_DRIFT"),
        (
            "expires_at",
            NOW + timedelta(seconds=10),
            "BLOCKED_CANARY_PROBE_FRESHNESS",
        ),
    ),
)
def test_persisted_probe_digest_and_timestamp_drift_fail_closed(
    canonical_connection, column, value, reason
):
    with canonical_connection.begin():
        decision, approval, _probe, receipt = _fixture(canonical_connection)
        canonical_connection.execute(
            EXECUTION_CANARY_PROBE_RECEIPTS_TABLE.update().values(**{column: value})
        )
        with pytest.raises(CanonicalExecutionChainBlocked, match=reason):
            _authorize(
                canonical_connection,
                decision,
                approval,
                receipt,
                evaluated_at=(
                    NOW + timedelta(seconds=20) if column == "expires_at" else NOW
                ),
            )


@pytest.mark.parametrize(
    ("source", "reason"),
    (
        (
                STRATEGY_SOURCE.replace("12.0", "-1.0"),
            "BLOCKED_CANARY_STRATEGY_LEVERAGE_AST",
        ),
        (
            STRATEGY_SOURCE.replace("can_short = False", "can_short = True"),
            "BLOCKED_CANARY_STRATEGY_NOT_LONG_ONLY",
        ),
    ),
)
def test_strategy_ast_must_prove_long_only_exact_artifact_cap(
    canonical_connection, source, reason
):
    with canonical_connection.begin():
        decision, approval, _probe, receipt = _fixture(canonical_connection)
        version = (
            canonical_connection.execute(
                select(STRATEGY_VERSIONS_TABLE).where(
                    STRATEGY_VERSIONS_TABLE.c.id == decision["strategy_version_id"]
                )
            )
            .mappings()
            .one()
        )
        canonical_connection.execute(
            STRATEGY_ARTIFACTS_TABLE.update()
            .where(STRATEGY_ARTIFACTS_TABLE.c.id == version["artifact_id"])
            .values(
                normalized_content=source,
                content_digest=sha256(source.encode()).hexdigest(),
                size_bytes=len(source.encode()),
            )
        )
        with pytest.raises(CanonicalExecutionChainBlocked, match=reason):
            _authorize(canonical_connection, decision, approval, receipt)


def test_wrong_qualified_target_is_blocked(canonical_connection):
    with canonical_connection.begin():
        decision, approval, _probe, receipt = _fixture(canonical_connection)
        canonical_connection.execute(
            RESEARCH_TARGETS_TABLE.update()
            .where(RESEARCH_TARGETS_TABLE.c.id == decision["research_target_id"])
            .values(instrument="ETH-USDT-SWAP")
        )
        with pytest.raises(
            CanonicalExecutionChainBlocked, match="BLOCKED_CANARY_POLICY_TARGET"
        ):
            _authorize(canonical_connection, decision, approval, receipt)
