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
