from app.schemas.backtest import BacktestResultCreate
from app.services.hyperopt_performance_comparison import (
    HyperoptPerformanceComparisonService,
)


def result_payload(
    *,
    result_path: str = "reports/backtests/before.json",
    profit_total: float = 100.0,
    profit_pct: float = 0.10,
    max_drawdown_pct: float = 0.05,
    win_rate: float = 0.55,
    total_trades: int = 40,
    sharpe: float = 1.20,
    sortino: float = 1.60,
    calmar: float = 1.10,
    sqn: float = 2.40,
    expectancy_ratio: float = 0.32,
) -> BacktestResultCreate:
    return BacktestResultCreate(
        result_path=result_path,
        metrics_snapshot={
            "normalized_metrics": {
                "sharpe": sharpe,
                "sortino": sortino,
                "calmar": calmar,
                "sqn": sqn,
                "expectancy_ratio": expectancy_ratio,
            }
        },
        profit_total=profit_total,
        profit_pct=profit_pct,
        max_drawdown_pct=max_drawdown_pct,
        win_rate=win_rate,
        total_trades=total_trades,
        timerange="20240101-20240201",
    )


def test_builds_stable_before_after_comparison_dto() -> None:
    service = HyperoptPerformanceComparisonService()

    comparison = service.compare(
        before_result=result_payload(),
        after_result=result_payload(
            result_path="reports/backtests/after.json",
            profit_total=130.0,
            profit_pct=0.13,
            max_drawdown_pct=0.04,
            win_rate=0.60,
            total_trades=44,
            sharpe=1.45,
            sortino=1.85,
            calmar=1.30,
            sqn=2.80,
            expectancy_ratio=0.40,
        ),
    )

    assert comparison.status == "IMPROVED"
    assert comparison.before_metrics["profit_pct"] == 0.10
    assert comparison.after_metrics["profit_pct"] == 0.13
    assert comparison.warnings == []
    assert comparison.elimination_signals == []

    dto = comparison.to_dict()
    assert dto["status"] == "IMPROVED"
    assert dto["before_metrics"]["sharpe"] == 1.20
    assert dto["after_metrics"]["sharpe"] == 1.45
    profit_delta = next(
        delta for delta in dto["metric_deltas"] if delta["metric"] == "profit_pct"
    )
    assert profit_delta == {
        "metric": "profit_pct",
        "before": 0.10,
        "after": 0.13,
        "delta": 0.03,
        "delta_pct": 0.3,
        "impact": "improved",
    }


def test_returns_blocked_when_before_or_after_result_is_missing() -> None:
    service = HyperoptPerformanceComparisonService()

    missing_before = service.compare(before_result=None, after_result=result_payload())
    missing_after = service.compare(before_result=result_payload(), after_result=None)

    assert missing_before.status == "BLOCKED"
    assert missing_before.blocked_reason == "BLOCKED: before backtest result is required"
    assert missing_after.status == "BLOCKED"
    assert missing_after.blocked_reason == "BLOCKED: after Hyperopt backtest result is required"


def test_risk_worsening_generates_warnings_and_elimination_signals() -> None:
    service = HyperoptPerformanceComparisonService()

    comparison = service.compare(
        before_result=result_payload(),
        after_result=result_payload(
            result_path="reports/backtests/after.json",
            profit_total=120.0,
            profit_pct=0.12,
            max_drawdown_pct=0.09,
            sharpe=0.90,
            sortino=1.10,
            calmar=0.70,
            sqn=1.90,
            expectancy_ratio=0.22,
        ),
    )

    assert comparison.status == "MIXED"
    assert "Risk worsened: max_drawdown_pct increased from 0.05 to 0.09." in (
        comparison.warnings
    )
    assert "Risk metric worsened: sharpe changed from 1.2 to 0.9." in comparison.warnings
    assert "max_drawdown_worsened" in comparison.elimination_signals
    assert "sharpe_worsened" in comparison.elimination_signals


def test_non_positive_after_profit_is_elimination_signal() -> None:
    service = HyperoptPerformanceComparisonService()

    comparison = service.compare(
        before_result=result_payload(),
        after_result=result_payload(
            result_path="reports/backtests/after.json",
            profit_total=-10.0,
            profit_pct=-0.01,
            total_trades=0,
        ),
    )

    assert comparison.status == "REGRESSED"
    assert "after_profit_pct_non_positive" in comparison.elimination_signals
    assert "after_total_trades_zero" in comparison.elimination_signals
    assert any("profit_pct" in warning for warning in comparison.warnings)


def test_blocks_when_no_shared_metrics_are_available() -> None:
    service = HyperoptPerformanceComparisonService()
    before = BacktestResultCreate(result_path="reports/backtests/before.json")
    after = BacktestResultCreate(result_path="reports/backtests/after.json")

    comparison = service.compare(before_result=before, after_result=after)

    assert comparison.status == "BLOCKED"
    assert comparison.blocked_reason == "BLOCKED: no shared before/after metrics are available"
    assert comparison.before_metrics["profit_pct"] is None
    assert comparison.after_metrics["profit_pct"] is None
