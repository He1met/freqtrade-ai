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


def test_parses_hyperopt_best_result_shape(tmp_path) -> None:
    result_path = tmp_path / "hyperopt-result.json"
    result_path.write_text(
        json.dumps(
            {
                "strategy_name": "MvpRsiStrategy",
                "best_result": {
                    "epoch": 87,
                    "loss": -1.2345,
                    "score": 0.88,
                    "is_best": True,
                    "spaces": ["buy", "sell", "roi", "stoploss"],
                    "params": {
                        "buy": {"rsi_enabled": True, "rsi_value": 32},
                        "sell": {"sell_rsi": 70},
                        "roi": {"0": 0.05, "60": 0.02},
                        "stoploss": -0.12,
                    },
                    "results_metrics": {
                        "profit_total_abs": 12.34,
                        "profit_total_pct": 4.3,
                        "max_drawdown_pct": 2.5,
                        "wins": 29,
                        "losses": 20,
                        "draws": 1,
                        "total_trades": 50,
                        "sharpe": 1.44,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    parsed = FreqtradeResultParser().parse_hyperopt_result(result_path)

    assert parsed.result_path == str(result_path)
    assert parsed.strategy_name == "MvpRsiStrategy"
    assert parsed.best_epoch == 87
    assert parsed.loss == -1.2345
    assert parsed.score == 0.88
    assert parsed.is_best is True
    assert parsed.spaces == ["buy", "sell", "roi", "stoploss"]
    assert parsed.best_params["buy"] == {"rsi_enabled": True, "rsi_value": 32}
    assert parsed.best_params["stoploss"] == -0.12
    assert parsed.metrics_snapshot["normalized_metrics"]["profit_total"] == 12.34
    assert parsed.metrics_snapshot["normalized_metrics"]["profit_pct"] == pytest.approx(0.043)
    assert parsed.metrics_snapshot["normalized_metrics"]["max_drawdown_pct"] == pytest.approx(0.025)
    assert parsed.metrics_snapshot["normalized_metrics"]["win_rate"] == pytest.approx(29 / 50)
    assert parsed.metrics_snapshot["normalized_metrics"]["total_trades"] == 50
    assert parsed.metrics_snapshot["normalized_metrics"]["sharpe"] == 1.44
    assert parsed.metrics_snapshot["parser_metadata"] == {
        "source": "freqtrade_hyperopt_result_parser",
        "missing_metrics": [],
        "best_result_shape": "best_result",
        "loss": -1.2345,
        "score": 0.88,
        "best_epoch": 87,
        "spaces": ["buy", "sell", "roi", "stoploss"],
    }


def test_parses_hyperopt_export_rows_and_selects_best_epoch(tmp_path) -> None:
    result_path = tmp_path / "hyperopt-export.json"
    result_path.write_text(
        json.dumps(
            {
                "results": [
                    {
                        "current_epoch": 1,
                        "loss": 3.14,
                        "is_best": False,
                        "strategy": "MvpRsiStrategy",
                        "params_details": {"buy": {"rsi": 44}},
                        "total_profit_abs": 1.0,
                    },
                    {
                        "current_epoch": 2,
                        "loss": 1.25,
                        "is_best": True,
                        "strategy": "MvpRsiStrategy",
                        "params_details": {
                            "buy": {"rsi": 31},
                            "sell": {"sell_rsi": 75},
                        },
                        "results_metrics": {
                            "total_profit_abs": "45.5",
                            "total_profit_pct": 6.25,
                            "max_relative_drawdown": 2.4,
                            "winning_rate": 58.0,
                            "closed_trades": "31",
                            "sortino_ratio": "1.74",
                        },
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    parsed = FreqtradeResultParser().parse_hyperopt_result(result_path)

    assert parsed.strategy_name == "MvpRsiStrategy"
    assert parsed.best_epoch == 2
    assert parsed.loss == 1.25
    assert parsed.spaces == ["buy", "sell"]
    assert parsed.best_params == {"buy": {"rsi": 31}, "sell": {"sell_rsi": 75}}
    assert parsed.metrics_snapshot["normalized_metrics"]["profit_total"] == 45.5
    assert parsed.metrics_snapshot["normalized_metrics"]["profit_pct"] == pytest.approx(0.0625)
    assert parsed.metrics_snapshot["normalized_metrics"]["max_drawdown_pct"] == pytest.approx(0.024)
    assert parsed.metrics_snapshot["normalized_metrics"]["win_rate"] == pytest.approx(0.58)
    assert parsed.metrics_snapshot["normalized_metrics"]["total_trades"] == 31
    assert parsed.metrics_snapshot["normalized_metrics"]["sortino"] == 1.74
    assert parsed.metrics_snapshot["parser_metadata"]["best_result_shape"] == "results"


def test_hyperopt_result_parser_uses_explicit_strategy_name_for_direct_shape(tmp_path) -> None:
    result_path = tmp_path / "hyperopt-direct.json"
    result_path.write_text(
        json.dumps(
            {
                "current_epoch": "5",
                "loss": "0.42",
                "params_dict": {"buy_rsi": 31, "sell_rsi": 75},
                "profit_total": 0.12,
                "trades": 8,
            }
        ),
        encoding="utf-8",
    )

    parsed = FreqtradeResultParser().parse_hyperopt_result(
        result_path,
        strategy_name="MvpRsiStrategy",
    )

    assert parsed.strategy_name == "MvpRsiStrategy"
    assert parsed.best_epoch == 5
    assert parsed.loss == 0.42
    assert parsed.best_params == {"buy_rsi": 31, "sell_rsi": 75}
    assert parsed.spaces == ["buy_rsi", "sell_rsi"]
    assert parsed.metrics_snapshot["normalized_metrics"]["profit_pct"] == 0.12
    assert parsed.metrics_snapshot["normalized_metrics"]["total_trades"] == 8


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ({"results": [{"is_best": False, "epoch": 1, "loss": 1.0}]}, "no best epoch"),
        ({"best_result": {"epoch": 1, "loss": 1.0}}, "best params"),
        (
            {"best_result": {"epoch": 1, "loss": 1.0, "params": {"buy": {"rsi": 31}}}},
            "strategy name",
        ),
        (
            {
                "strategy_name": "MvpRsiStrategy",
                "best_result": {"loss": 1.0, "params": {"buy": {"rsi": 31}}},
            },
            "Required integer field",
        ),
        (
            {
                "strategy_name": "MvpRsiStrategy",
                "best_result": {"epoch": 1, "params": {"buy": {"rsi": 31}}},
            },
            "Required numeric field",
        ),
    ],
)
def test_hyperopt_result_parser_fails_closed_for_incomplete_results(
    tmp_path,
    payload,
    match,
) -> None:
    result_path = tmp_path / "hyperopt-invalid.json"
    result_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(FreqtradeResultParseError, match=match):
        FreqtradeResultParser().parse_hyperopt_result(result_path)


def test_hyperopt_result_parser_reports_corrupted_json(tmp_path) -> None:
    result_path = tmp_path / "hyperopt-corrupt.json"
    result_path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(FreqtradeResultParseError, match="not valid JSON"):
        FreqtradeResultParser().parse_hyperopt_result(result_path)
