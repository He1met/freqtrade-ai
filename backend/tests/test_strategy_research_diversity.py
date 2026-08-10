import hashlib

import pytest

from app.core.strategy_research_diversity import (
    REQUIRED_STRATEGY_FAMILIES,
    ResearchDiversityContractError,
    validate_research_diversity_contract,
)
from app.core.strategy_research_matrix import RESEARCH_TARGETS


def candidates():
    families = sorted(REQUIRED_STRATEGY_FAMILIES)
    rows = []
    for target in RESEARCH_TARGETS:
        for slot in range(1, 11):
            identity = f"{target.key}|{slot}"
            rows.append(
                {
                    "pair": target.pair,
                    "timeframe": target.timeframe,
                    "unit_slot": slot,
                    "strategy_family": families[(slot - 1) % len(families)],
                    "regime_hypothesis": "explicit market-state hypothesis",
                    "expected_holding_period": "3-12 candles",
                    "expected_trade_frequency": "1-4 trades per week",
                    "structure_fingerprint": hashlib.sha256(identity.encode()).hexdigest(),
                    "similarity_evidence": {
                        "status": "PASSED",
                        "max_signal_similarity": 0.5,
                        "evidence_digest": hashlib.sha256((identity + "signal").encode()).hexdigest(),
                    },
                    "correlation_evidence": {
                        "status": "PASSED",
                        "max_abs_pnl_correlation": 0.4,
                        "evidence_digest": hashlib.sha256((identity + "pnl").encode()).hexdigest(),
                    },
                }
            )
    return rows


def test_exact_six_by_ten_diversity_contract_passes():
    assert len(validate_research_diversity_contract(candidates())) == 60


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "family", "signal", "pnl"])
def test_incomplete_or_correlated_batches_fail_closed(mutation):
    rows = candidates()
    if mutation == "missing":
        rows.pop()
    elif mutation == "duplicate":
        rows[-1]["pair"] = rows[0]["pair"]
        rows[-1]["timeframe"] = rows[0]["timeframe"]
        rows[-1]["unit_slot"] = rows[0]["unit_slot"]
    elif mutation == "family":
        rows[0]["strategy_family"] = "UNKNOWN"
    elif mutation == "signal":
        rows[0]["similarity_evidence"]["status"] = "UNKNOWN"
    else:
        rows[0]["correlation_evidence"]["evidence_digest"] = "invalid"
    with pytest.raises(ResearchDiversityContractError):
        validate_research_diversity_contract(rows)
