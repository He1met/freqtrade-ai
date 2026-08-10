from __future__ import annotations

import re
from typing import Any, Iterable

from app.core.strategy_research_matrix import RESEARCH_TARGETS


RESEARCH_CANDIDATES_PER_UNIT = 10
RESEARCH_CANDIDATE_COUNT = len(RESEARCH_TARGETS) * RESEARCH_CANDIDATES_PER_UNIT
REQUIRED_STRATEGY_FAMILIES = frozenset(
    {
        "TREND_BREAKOUT_FOLLOWING",
        "MOMENTUM_VOLUME_CONFIRMATION",
        "MEAN_REVERSION",
        "VOLATILITY_BREAKOUT",
        "PULLBACK_TREND_CONTINUATION",
        "RANGE_LIQUIDITY_FILTER",
    }
)
MAX_SIGNAL_SIMILARITY = 0.90
MAX_ABS_PNL_CORRELATION = 0.85
_DIGEST = re.compile(r"[0-9a-f]{64}")


class ResearchDiversityContractError(ValueError):
    pass


def _required_text(candidate: dict[str, Any], field: str) -> str:
    value = candidate.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ResearchDiversityContractError(f"candidate is missing {field}")
    return value


def _terminal_metric(
    candidate: dict[str, Any],
    field: str,
    metric: str,
    maximum: float,
) -> dict[str, Any]:
    evidence = candidate.get(field)
    if not isinstance(evidence, dict) or evidence.get("status") not in {"PASSED", "BLOCKED"}:
        raise ResearchDiversityContractError(f"candidate {field} is not terminal")
    digest = evidence.get("evidence_digest")
    value = evidence.get(metric)
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
        raise ResearchDiversityContractError(f"candidate {field} digest is invalid")
    if evidence["status"] == "PASSED" and (
        not isinstance(value, (int, float)) or not 0 <= value <= 1
    ):
        raise ResearchDiversityContractError(f"candidate {field} metric is invalid")
    return evidence


def validate_research_diversity_contract(
    candidates: Iterable[dict[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Validate exact 6x10 units and evidence-backed structural diversity."""

    rows = tuple(candidates)
    if len(rows) != RESEARCH_CANDIDATE_COUNT:
        raise ResearchDiversityContractError(
            f"research batch must contain exactly {RESEARCH_CANDIDATE_COUNT} candidates"
        )
    expected_units = {
        (target.pair, target.timeframe, slot)
        for target in RESEARCH_TARGETS
        for slot in range(1, RESEARCH_CANDIDATES_PER_UNIT + 1)
    }
    actual_units: set[tuple[str, str, int]] = set()
    families_by_target: dict[tuple[str, str], set[str]] = {}
    for candidate in rows:
        if not isinstance(candidate, dict):
            raise ResearchDiversityContractError("candidate evidence must be an object")
        pair = candidate.get("pair")
        timeframe = candidate.get("timeframe")
        slot = candidate.get("unit_slot")
        unit = (pair, timeframe, slot)
        if unit not in expected_units or unit in actual_units:
            raise ResearchDiversityContractError("candidate unit matrix is incomplete or duplicated")
        actual_units.add(unit)
        family = _required_text(candidate, "strategy_family")
        if family not in REQUIRED_STRATEGY_FAMILIES:
            raise ResearchDiversityContractError("candidate strategy_family is unsupported")
        families_by_target.setdefault((pair, timeframe), set()).add(family)
        _required_text(candidate, "regime_hypothesis")
        _required_text(candidate, "expected_holding_period")
        _required_text(candidate, "expected_trade_frequency")
        fingerprint = _required_text(candidate, "structure_fingerprint")
        if _DIGEST.fullmatch(fingerprint) is None:
            raise ResearchDiversityContractError("candidate structure fingerprint is invalid")
        _terminal_metric(
            candidate,
            "similarity_evidence",
            "max_signal_similarity",
            MAX_SIGNAL_SIMILARITY,
        )
        _terminal_metric(
            candidate,
            "correlation_evidence",
            "max_abs_pnl_correlation",
            MAX_ABS_PNL_CORRELATION,
        )
    if actual_units != expected_units:
        raise ResearchDiversityContractError("candidate unit matrix is incomplete")
    if any(
        not REQUIRED_STRATEGY_FAMILIES.issubset(families)
        for families in families_by_target.values()
    ):
        raise ResearchDiversityContractError(
            "each pair/timeframe unit must cover all required strategy families"
        )
    return rows
