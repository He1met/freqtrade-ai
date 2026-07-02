import json

import pytest

from app.adapters.freqtrade.exceptions import FreqtradeResultParseError
from app.adapters.freqtrade.result_parser import FreqtradeResultParser


def test_parses_freqtrade_strategy_summary(tmp_path) -> None:
    result_path = tmp_path / "backtest-result.json"
    result_path.write_text(
        json.dumps(
            {
                "strategy": {
                    "MvpRsiStrategy": {
                        "profit_total_abs": 123.4,
                        "profit_total_pct": 12.3,
                        "max_drawdown_pct": 4.5,
                        "wins": 30,
                        "losses": 10,
                        "draws": 2,
                        "total_trades": 42,
                        "backtest_start": "20240101",
                        "backtest_end": "20240201",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    parsed = FreqtradeResultParser().parse_backtest_result(
        result_path,
        strategy_name="MvpRsiStrategy",
    )

    assert parsed.result_path == str(result_path)
    assert parsed.profit_total == 123.4
    assert parsed.profit_pct == pytest.approx(0.123)
    assert parsed.max_drawdown_pct == pytest.approx(0.045)
    assert parsed.win_rate == pytest.approx(30 / 42)
    assert parsed.total_trades == 42
    assert parsed.timerange == "20240101-20240201"
    assert parsed.metrics_snapshot["profit_total_abs"] == 123.4
    assert parsed.metrics_snapshot["normalized_metrics"] == {
        "profit_total": 123.4,
        "profit_pct": pytest.approx(0.123),
        "max_drawdown_pct": pytest.approx(0.045),
        "win_rate": pytest.approx(30 / 42),
        "total_trades": 42,
        "timerange": "20240101-20240201",
        "sharpe": None,
        "sortino": None,
        "calmar": None,
        "sqn": None,
        "expectancy_ratio": None,
    }


def test_parses_strategy_comparison_shape_with_risk_metrics(tmp_path) -> None:
    result_path = tmp_path / "backtest-result.json"
    result_path.write_text(
        json.dumps(
            {
                "strategy_comparison": [
                    {
                        "key": "MvpRsiStrategy",
                        "total_profit_abs": "45.5",
                        "total_profit_pct": 6.25,
                        "max_relative_drawdown": 2.4,
                        "winning_rate": 58.0,
                        "closed_trades": "31",
                        "timerange": "20240101-20240301",
                        "sharpe": 1.32,
                        "sortino_ratio": "1.74",
                        "calmar_ratio": 0.98,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    parsed = FreqtradeResultParser().parse_backtest_result(
        result_path,
        strategy_name="MvpRsiStrategy",
    )

    assert parsed.profit_total == 45.5
    assert parsed.profit_pct == pytest.approx(0.0625)
    assert parsed.max_drawdown_pct == pytest.approx(0.024)
    assert parsed.win_rate == pytest.approx(0.58)
    assert parsed.total_trades == 31
    assert parsed.timerange == "20240101-20240301"
    assert parsed.metrics_snapshot["normalized_metrics"]["sharpe"] == 1.32
    assert parsed.metrics_snapshot["normalized_metrics"]["sortino"] == 1.74
    assert parsed.metrics_snapshot["normalized_metrics"]["calmar"] == 0.98
    assert parsed.metrics_snapshot["parser_metadata"]["risk_metrics_available"] == [
        "sharpe",
        "sortino",
        "calmar",
    ]


def test_result_parser_records_missing_metric_reasons(tmp_path) -> None:
    result_path = tmp_path / "backtest-result.json"
    result_path.write_text(
        json.dumps(
            {
                "strategy": {
                    "MvpRsiStrategy": {
                        "profit_total_abs": 10.0,
                        "total_trades": 4,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    parsed = FreqtradeResultParser().parse_backtest_result(
        result_path,
        strategy_name="MvpRsiStrategy",
    )

    assert parsed.profit_total == 10.0
    assert parsed.profit_pct is None
    missing = parsed.metrics_snapshot["parser_metadata"]["missing_metrics"]
    assert {entry["metric"] for entry in missing} == {
        "profit_pct",
        "max_drawdown_pct",
        "win_rate",
        "timerange",
    }
    assert all("No supported field" in entry["reason"] for entry in missing)


def test_result_parser_reports_missing_file(tmp_path) -> None:
    with pytest.raises(FreqtradeResultParseError, match="does not exist"):
        FreqtradeResultParser().parse_backtest_result(tmp_path / "missing.json")


def test_result_parser_requires_strategy_name_for_multiple_strategies(tmp_path) -> None:
    result_path = tmp_path / "backtest-result.json"
    result_path.write_text(
        json.dumps({"strategy": {"A": {"total_trades": 1}, "B": {"total_trades": 2}}}),
        encoding="utf-8",
    )

    with pytest.raises(FreqtradeResultParseError, match="strategy_name is required"):
        FreqtradeResultParser().parse_backtest_result(result_path)


def test_result_parser_reports_invalid_numeric_aliases(tmp_path) -> None:
    result_path = tmp_path / "backtest-result.json"
    result_path.write_text(
        json.dumps({"strategy": {"MvpRsiStrategy": {"profit_total_abs": "not-a-number"}}}),
        encoding="utf-8",
    )

    with pytest.raises(FreqtradeResultParseError, match="profit_total_abs"):
        FreqtradeResultParser().parse_backtest_result(
            result_path,
            strategy_name="MvpRsiStrategy",
        )
