# ruff: noqa: F401, F811
from __future__ import annotations

from datetime import timedelta
from hashlib import sha256

import pytest
from app.canonical_v13.execution_common import (
    CanonicalExecutionChainBlocked,
    canonical_execution_digest,
)
from app.canonical_v13.models import (
    DEPLOYMENT_APPROVALS_TABLE,
    EXECUTION_CANARY_RISK_POLICIES_TABLE,
    QUALIFICATION_DECISIONS_TABLE,
    STRATEGY_ARTIFACTS_TABLE,
    STRATEGY_VERSIONS_TABLE,
)
from app.canonical_v13.phase9_canary_policy import authorize_canary_risk_policy
from app.canonical_v13.phase9_execution_authority import (
    authorize_demo_risk_budget,
    record_redacted_demo_attestation,
)
from sqlalchemy import func, select
from tests.test_canonical_v13_phase9_execution_authority import _production_chain
from tests.test_canonical_v13_research_evaluation import (
    NOW,
    canonical_connection,
)

STRATEGY_SOURCE = """from freqtrade.strategy import IStrategy
class ExactCanaryStrategy(IStrategy):
    can_short = False
    def leverage(self, pair, current_time, current_rate, proposed_leverage, max_leverage, entry_tag, side, **kwargs):
        return min(14.0, max_leverage)
"""


def _metadata(*, exchange_max_leverage: str = "100", contract_type: str = "linear"):
    return {
        "instrument": "BTC-USDT-SWAP",
        "instrument_type": "SWAP",
        "contract_type": contract_type,
        "base_currency": "BTC",
        "quote_currency": "USDT",
        "settle_currency": "USDT",
        "contract_value": "0.01",
        "contract_value_currency": "BTC",
        "lot_size": "0.01",
        "minimum_size": "0.01",
        "exchange_max_leverage": exchange_max_leverage,
        "state": "live",
    }


def _evidence(
    attestation_digest: str,
    *,
    exchange_max_leverage="100",
    mark="60000",
    contract_type="linear",
):
    metadata = _metadata(
        exchange_max_leverage=exchange_max_leverage, contract_type=contract_type
    )
    metadata_digest = canonical_execution_digest(
        {
            "contract": "canonical-v13-okx-demo-instrument-metadata-receipt-v1",
            "execution_target": "OKX_DEMO",
            "instrument": "BTC-USDT-SWAP",
            "instrument_metadata": metadata,
        }
    )
    mark_digest = canonical_execution_digest(
        {
            "contract": "canonical-v13-okx-demo-mark-price-receipt-v1",
            "execution_target": "OKX_DEMO",
            "instrument": "BTC-USDT-SWAP",
            "metadata_receipt_digest": metadata_digest,
            "mark_price": mark,
            "observed_at": NOW.isoformat(),
        }
    )
    return {
        "contract": "canonical-v13-okx-demo-canary-policy-evidence-v1",
        "execution_target": "OKX_DEMO",
        "instrument": "BTC-USDT-SWAP",
        "position_policy": "LONG_ONLY",
        "max_order_count": 1,
        "allow_real_funds": False,
        "instrument_metadata": metadata,
        "metadata_receipt_digest": metadata_digest,
        "mark_price": mark,
        "mark_observed_at": NOW.isoformat(),
        "mark_price_receipt_digest": mark_digest,
        "attestation_digest": attestation_digest,
    }


def _fixture(connection):
    approval, deployment, _runtime, _intent, _launcher = _production_chain(connection)
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
    attestation = record_redacted_demo_attestation(
        connection,
        deployment_id=deployment.deployment_id,
        instrument="BTC-USDT-SWAP",
        account_fingerprint_digest="d" * 64,
        credential_generation_digest="e" * 64,
        permissions={"read": True, "trade": True, "withdraw": False},
        observed_at=NOW,
        expires_at=NOW + timedelta(seconds=60),
        evaluated_at=NOW,
    )
    return decision, approval, attestation


def _authorize(
    connection,
    decision,
    approval,
    attestation,
    *,
    evidence=None,
    evaluated_at=NOW,
    idempotency_key="exact-canary-policy",
):
    return authorize_canary_risk_policy(
        connection,
        qualification_decision_id=decision["id"],
        deployment_approval_id=approval.deployment_approval_id,
        execution_attestation_id=attestation.attestation_id,
        actor_identity="phase9-human-policy-owner",
        idempotency_key=idempotency_key,
        reason="one reviewed canonical Demo canary",
        redacted_evidence=evidence or _evidence(attestation.attestation_digest),
        evaluated_at=evaluated_at,
    )


def test_one_shot_policy_derives_exact_minimum_notional_and_receipt(
    canonical_connection,
):
    with canonical_connection.begin():
        decision, approval, attestation = _fixture(canonical_connection)
        result = _authorize(canonical_connection, decision, approval, attestation)
        budget = authorize_demo_risk_budget(
            canonical_connection,
            deployment_approval_id=approval.deployment_approval_id,
            actor_identity="phase9-human-policy-owner",
            reason="freeze the exact one-shot canary policy",
            policy_source_receipt_digest=result.receipt_digest,
            evaluated_at=NOW,
        )
        row = (
            canonical_connection.execute(select(EXECUTION_CANARY_RISK_POLICIES_TABLE))
            .mappings()
            .one()
        )
    assert str(result.max_notional) == "6.0000"
    assert result.effective_leverage == 14
    assert result.expires_at - result.accepted_at == timedelta(minutes=30)
    assert row["position_policy"] == "LONG_ONLY"
    assert row["max_order_count"] == 1
    assert row["allow_real_funds"] is False
    assert row["receipt_digest"] == result.receipt_digest
    assert budget.repeat_noop is False


def test_exchange_max_below_strategy_cap_is_effective_leverage(canonical_connection):
    with canonical_connection.begin():
        decision, approval, attestation = _fixture(canonical_connection)
        result = _authorize(
            canonical_connection,
            decision,
            approval,
            attestation,
            evidence=_evidence(
                attestation.attestation_digest, exchange_max_leverage="5"
            ),
        )
    assert result.effective_leverage == 5


def test_exact_replay_after_expiry_is_noop_and_drift_cannot_reset(canonical_connection):
    with canonical_connection.begin():
        decision, approval, attestation = _fixture(canonical_connection)
        first = _authorize(canonical_connection, decision, approval, attestation)
        replay = _authorize(
            canonical_connection,
            decision,
            approval,
            attestation,
            evaluated_at=NOW + timedelta(hours=1),
        )
        with pytest.raises(
            CanonicalExecutionChainBlocked, match="BLOCKED_CANARY_POLICY_REPLAY_DRIFT"
        ):
            _authorize(
                canonical_connection,
                decision,
                approval,
                attestation,
                evidence=_evidence(attestation.attestation_digest, mark="61000"),
                evaluated_at=NOW + timedelta(hours=1),
            )
        count = canonical_connection.execute(
            select(func.count()).select_from(EXECUTION_CANARY_RISK_POLICIES_TABLE)
        ).scalar_one()
    assert replay.repeat_noop is True
    assert replay.policy_id == first.policy_id
    assert replay.receipt_digest == first.receipt_digest
    assert count == 1


@pytest.mark.parametrize(
    ("source", "reason"),
    (
        (
            STRATEGY_SOURCE.replace("14.0", "15.0"),
            "BLOCKED_CANARY_STRATEGY_LEVERAGE_AST",
        ),
        (
            STRATEGY_SOURCE.replace("can_short = False", "can_short = True"),
            "BLOCKED_CANARY_STRATEGY_NOT_LONG_ONLY",
        ),
    ),
)
def test_strategy_ast_must_prove_long_only_exact_14_cap(
    canonical_connection, source, reason
):
    with canonical_connection.begin():
        decision, approval, attestation = _fixture(canonical_connection)
        digest = sha256(source.encode()).hexdigest()
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
                content_digest=digest,
                size_bytes=len(source.encode()),
            )
        )
        with pytest.raises(CanonicalExecutionChainBlocked, match=reason):
            _authorize(canonical_connection, decision, approval, attestation)


def test_inverse_or_stale_or_digest_drifted_market_evidence_blocks(
    canonical_connection,
):
    with canonical_connection.begin():
        decision, approval, attestation = _fixture(canonical_connection)
        inverse = _evidence(attestation.attestation_digest, contract_type="inverse")
        with pytest.raises(
            CanonicalExecutionChainBlocked, match="BLOCKED_CANARY_METADATA_LINEAGE"
        ):
            _authorize(
                canonical_connection, decision, approval, attestation, evidence=inverse
            )

        stale = _evidence(attestation.attestation_digest)
        stale["mark_observed_at"] = (NOW - timedelta(minutes=2)).isoformat()
        with pytest.raises(
            CanonicalExecutionChainBlocked, match="BLOCKED_CANARY_MARK_FRESHNESS"
        ):
            _authorize(
                canonical_connection, decision, approval, attestation, evidence=stale
            )

        drifted = _evidence(attestation.attestation_digest)
        drifted["metadata_receipt_digest"] = "f" * 64
        with pytest.raises(
            CanonicalExecutionChainBlocked, match="BLOCKED_CANARY_METADATA_DIGEST"
        ):
            _authorize(
                canonical_connection, decision, approval, attestation, evidence=drifted
            )


def test_wrong_qualified_target_is_blocked(canonical_connection):
    with canonical_connection.begin():
        decision, approval, attestation = _fixture(canonical_connection)
        from app.canonical_v13.models import RESEARCH_TARGETS_TABLE

        canonical_connection.execute(
            RESEARCH_TARGETS_TABLE.update()
            .where(RESEARCH_TARGETS_TABLE.c.id == decision["research_target_id"])
            .values(instrument="ETH-USDT-SWAP")
        )
        with pytest.raises(
            CanonicalExecutionChainBlocked, match="BLOCKED_CANARY_POLICY_TARGET"
        ):
            _authorize(canonical_connection, decision, approval, attestation)


def test_non_redacted_or_fixed_policy_field_drift_is_blocked(canonical_connection):
    with canonical_connection.begin():
        decision, approval, attestation = _fixture(canonical_connection)
        extra = _evidence(attestation.attestation_digest)
        extra["api_secret"] = "must-never-be-accepted"
        with pytest.raises(
            CanonicalExecutionChainBlocked,
            match="BLOCKED_CANARY_POLICY_EVIDENCE_FIELDS",
        ):
            _authorize(
                canonical_connection, decision, approval, attestation, evidence=extra
            )

        count_drift = _evidence(attestation.attestation_digest)
        count_drift["max_order_count"] = 2
        with pytest.raises(
            CanonicalExecutionChainBlocked, match="BLOCKED_CANARY_POLICY_EVIDENCE"
        ):
            _authorize(
                canonical_connection,
                decision,
                approval,
                attestation,
                evidence=count_drift,
            )
