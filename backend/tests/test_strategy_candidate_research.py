from argparse import Namespace
import json
from pathlib import Path
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.run_strategy_candidate_research import (
    MIN_STRATEGY_SCORE,
    MAX_VALIDATION_DRAWDOWN,
    WINDOWS,
    _FAILURE_CONTEXT,
    _discover_candidates,
    _project_score,
    _record_unhandled_failure,
    _stress_metrics,
    _validate_run_id,
)


def test_research_bundle_contains_exactly_ten_static_safe_candidates() -> None:
    candidates = _discover_candidates(REPO_ROOT / "research" / "strategy_candidates")

    assert len(candidates) == 10
    assert len({candidate.class_name for candidate in candidates}) == 10
    assert len({candidate.sha256 for candidate in candidates}) == 10
    assert all(len(candidate.sha256) == 64 for candidate in candidates)


def test_research_windows_are_non_overlapping_and_cover_required_regimes() -> None:
    intervals = []
    for _name, _kind, _regime, timerange in WINDOWS:
        start, end = timerange.split("-", maxsplit=1)
        intervals.append((start, end))

    for index, left in enumerate(intervals):
        for right in intervals[index + 1 :]:
            assert left[1] <= right[0] or right[1] <= left[0]
    assert {regime for _name, kind, regime, _timerange in WINDOWS if kind == "WALK_FORWARD"} == {
        "bull",
        "bear",
        "range",
    }
    assert sum(kind == "OOS" for _name, kind, _regime, _timerange in WINDOWS) == 1


def test_slippage_stress_and_project_score_are_deterministic() -> None:
    strategy = {
        "trades": [
            {
                "close_timestamp": 1,
                "stake_amount": 100.0,
                "profit_ratio": 0.01,
                "profit_abs": 1.0,
            },
            {
                "close_timestamp": 2,
                "stake_amount": 100.0,
                "profit_ratio": -0.005,
                "profit_abs": -0.5,
            },
            {
                "close_timestamp": 3,
                "stake_amount": 100.0,
                "profit_ratio": 0.002,
                "profit_abs": 0.2,
            },
        ]
    }

    metrics = _stress_metrics(strategy)
    score = _project_score(metrics)

    assert metrics["profit_total_abs"] == 0.58
    assert metrics["profit_pct"] == 0.00058
    assert metrics["win_rate"] == pytest.approx(2 / 3)
    assert score["scoring_version"] == "phase2-quality-v1"
    assert score["total_score"] >= MIN_STRATEGY_SCORE
    assert MAX_VALIDATION_DRAWDOWN == 0.15


def test_hourly_run_ids_are_unique_and_calendar_valid() -> None:
    assert _validate_run_id("20260807") == "20260807"
    assert _validate_run_id("2026080710") == "2026080710"
    assert _validate_run_id("202608071059") == "202608071059"
    with pytest.raises(RuntimeError, match="valid calendar"):
        _validate_run_id("2026023010")


def test_unhandled_validation_failure_keeps_all_generated_candidates(tmp_path) -> None:
    output = tmp_path / "failed.json"
    candidates = _discover_candidates(REPO_ROOT / "research" / "strategy_candidates")
    results = {
        candidate.class_name: {
            "file": str(candidate.path.relative_to(REPO_ROOT)),
            "sha256": candidate.sha256,
            "static_check": "PASSED",
            "loadable": True,
            "windows": {},
        }
        for candidate in candidates
    }
    _FAILURE_CONTEXT.update(
        {
            "args": Namespace(
                run_id="2026080912",
                persist_database=False,
                repository_commit=None,
            ),
            "repo": REPO_ROOT,
            "strategy_path": REPO_ROOT / "research" / "strategy_candidates",
            "output": output,
            "stage": "LOOKAHEAD",
            "results": results,
            "candidates": candidates,
        }
    )
    try:
        _record_unhandled_failure(RuntimeError("token=should-not-leak"))
    finally:
        _FAILURE_CONTEXT.clear()

    payload = json.loads(output.read_text())
    assert payload["status"] == "FAILED"
    assert payload["failed_stage"] == "LOOKAHEAD"
    assert payload["generated_count"] == 10
    assert payload["persisted_count"] == 0
    assert len(payload["candidates"]) == 10
    assert "should-not-leak" not in payload["failure_reason"]
