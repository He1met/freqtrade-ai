from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import math
from pathlib import Path

import pandas as pd
import pytest


ROOT = Path(__file__).resolve().parents[2]
WORKER = (
    ROOT
    / "containers/canonical-v13-research/"
    "canonical_v13_cost_aware_optimizer.py"
)
SPEC = importlib.util.spec_from_file_location("canonical_cost_optimizer", WORKER)
assert SPEC is not None and SPEC.loader is not None
optimizer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(optimizer)


FEE = 0.0005
SLIPPAGE = 0.0002
WALLET = 10_000.0
STAKE = 100.0


def _trade(
    *,
    is_short: bool,
    leverage: float,
    amount: float,
    open_rate: float,
    close_rate: float,
) -> dict[str, object]:
    price_pnl = amount * (
        open_rate - close_rate if is_short else close_rate - open_rate
    )
    fee_abs = FEE * amount * (open_rate + close_rate)
    return {
        "amount": amount,
        "open_rate": open_rate,
        "close_rate": close_rate,
        "stake_amount": STAKE,
        "leverage": leverage,
        "fee_open": FEE,
        "fee_close": FEE,
        "funding_fees": 0.0,
        "profit_abs": price_pnl - fee_abs,
        "profit_ratio": 999.0,
        "is_short": is_short,
    }


def _result(trades: list[dict[str, object]]) -> dict[str, object]:
    profit_total = sum(float(trade["profit_abs"]) for trade in trades) / WALLET
    return {
        "starting_balance": WALLET,
        "total_trades": len(trades),
        "profit_total": profit_total,
        "max_drawdown_account": 0.01,
        "trades": trades,
        "canonical_execution_evidence": {
            "protections_enabled": True,
            "window_end_exclusive": "2026-01-02T00:00:00+00:00",
            "command_digest": "b" * 64,
            "window_data_digest": "c" * 64,
        },
    }


def _metrics(
    trades: list[dict[str, object]], *, include_directional: bool = False
) -> dict[str, object]:
    return optimizer.metrics(
        _result(trades),
        FEE,
        SLIPPAGE,
        0.00075,
        0.0003,
        wallet=WALLET,
        stake_limit=STAKE,
        leverage_limit=2.0,
        sizing_digest="a" * 64,
        include_directional=include_directional,
    )


def test_account_cost_golden_long_short_and_leverage() -> None:
    long_trade = _trade(
        is_short=False,
        leverage=2.0,
        amount=2.0,
        open_rate=100.0,
        close_rate=101.0,
    )
    short_trade = _trade(
        is_short=True,
        leverage=1.0,
        amount=1.0,
        open_rate=100.0,
        close_rate=99.0,
    )

    observed = _metrics([long_trade, short_trade], include_directional=True)
    fee_inclusive = (
        float(long_trade["profit_abs"]) + float(short_trade["profit_abs"])
    ) / WALLET
    turnover = (2.0 * 201.0 + 1.0 * 199.0) / WALLET
    slippage_cost = SLIPPAGE * turnover

    assert observed["fee_inclusive_return"] == pytest.approx(fee_inclusive)
    assert observed["turnover_account"] == pytest.approx(turnover)
    assert observed["modeled_slippage_cost"] == pytest.approx(slippage_cost)
    assert observed["net_return_after_cost"] == pytest.approx(
        fee_inclusive - slippage_cost
    )
    assert observed["modeled_fee_cost"] == pytest.approx(FEE * turnover)
    assert observed["directional_net_reconciliation_error"] <= 1e-15
    assert observed["direction_attribution"]["long"]["trade_count"] == 1
    assert observed["direction_attribution"]["short"]["trade_count"] == 1


def test_fee_is_already_in_profit_abs_and_old_flat_formula_is_rejected() -> None:
    trade = _trade(
        is_short=False,
        leverage=1.0,
        amount=1.0,
        open_rate=100.0,
        close_rate=101.0,
    )
    observed = _metrics([trade])
    expected = float(trade["profit_abs"]) / WALLET - SLIPPAGE * 201.0 / WALLET
    old_flat_formula = float(trade["profit_abs"]) / WALLET - 2 * SLIPPAGE

    assert observed["net_return_after_cost"] == pytest.approx(expected)
    assert observed["net_return_after_cost"] != pytest.approx(old_flat_formula)
    assert observed["net_return_after_cost"] != pytest.approx(
        expected - FEE * 201.0 / WALLET
    )


def test_sizing_and_total_reconciliation_fail_closed() -> None:
    trade = _trade(
        is_short=False,
        leverage=1.0,
        amount=1.0,
        open_rate=100.0,
        close_rate=101.0,
    )
    wrong_wallet = _result([trade])
    wrong_wallet["starting_balance"] = WALLET + 1
    with pytest.raises(optimizer.Blocked, match="wallet"):
        optimizer.metrics(
            wrong_wallet,
            FEE,
            SLIPPAGE,
            0.00075,
            0.0003,
            wallet=WALLET,
            stake_limit=STAKE,
            leverage_limit=2.0,
            sizing_digest="a" * 64,
        )

    wrong_total = _result([trade])
    wrong_total["profit_total"] = float(wrong_total["profit_total"]) + 0.01
    with pytest.raises(optimizer.Blocked, match="profit_abs"):
        optimizer.metrics(
            wrong_total,
            FEE,
            SLIPPAGE,
            0.00075,
            0.0003,
            wallet=WALLET,
            stake_limit=STAKE,
            leverage_limit=2.0,
            sizing_digest="a" * 64,
        )

    wrong_notional = _result([deepcopy(trade)])
    wrong_notional["trades"][0]["amount"] = 2.0
    with pytest.raises(optimizer.Blocked, match="stake and leverage"):
        optimizer.metrics(
            wrong_notional,
            FEE,
            SLIPPAGE,
            0.00075,
            0.0003,
            wallet=WALLET,
            stake_limit=STAKE,
            leverage_limit=2.0,
            sizing_digest="a" * 64,
        )


def _market_rows() -> list[dict[str, object]]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        {
            "opened_at": (start + timedelta(minutes=15 * index)).isoformat(),
            "open": 100.0 + index,
            "high": 101.0 + index,
            "low": 99.0 + index,
            "close": 100.5 + index,
            "volume": 10.0,
        }
        for index in range(4)
    ]


def _write_market(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_market_integrity_and_boundary_next_candle_invariance(tmp_path: Path) -> None:
    first_path = tmp_path / "first.jsonl"
    second_path = tmp_path / "second.jsonl"
    rows = _market_rows()
    mutated = deepcopy(rows)
    mutated[-1].update({"open": 900.0, "high": 901.0, "low": 899.0, "close": 900.5})
    _write_market(first_path, rows)
    _write_market(second_path, mutated)

    first = optimizer.load_market_frame(first_path)
    second = optimizer.load_market_frame(second_path)
    end = datetime(2026, 1, 1, 0, 45, tzinfo=timezone.utc)
    visible_first = optimizer.window_frame(first, start=first.iloc[0]["date"], end=end)
    visible_second = optimizer.window_frame(second, start=second.iloc[0]["date"], end=end)

    pd.testing.assert_frame_equal(visible_first, visible_second)
    assert visible_first["date"].max().to_pydatetime() == end - timedelta(minutes=15)


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda rows: rows.__delitem__(1), "900 seconds"),
        (lambda rows: rows[1].update({"low": 102.0}), "OHLC"),
        (lambda rows: rows[1].update({"open": math.nan}), "non-finite"),
        (lambda rows: rows[1].update({"volume": -1}), "nonnegative"),
        (
            lambda rows: rows[1].update({"opened_at": "2026-01-01T08:15:00+08:00"}),
            "UTC",
        ),
        (
            lambda rows: rows[1].update({"opened_at": "2026-01-01T00:16:00Z"}),
            "900-second boundary",
        ),
        (lambda rows: rows.reverse(), "continuity"),
    ],
)
def test_market_integrity_rejects_invalid_rows(
    tmp_path: Path, mutation: object, reason: str
) -> None:
    rows = _market_rows()
    mutation(rows)  # type: ignore[operator]
    path = tmp_path / "market.jsonl"
    _write_market(path, rows)
    with pytest.raises(optimizer.Blocked, match=reason):
        optimizer.load_market_frame(path)


def test_backtest_command_enables_protections_and_replay_is_exact() -> None:
    command = optimizer.backtest_command(
        strategy_class="Strategy",
        strategy_path=Path("/work/strategies/Strategy.py"),
        data_root=Path("/work/window-data/train"),
        result_dir=Path("/work/results/Strategy-train"),
        start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end=datetime(2026, 1, 2, tzinfo=timezone.utc),
        metadata=Path("/work/metadata.json"),
        fee=FEE,
    )
    assert command.count("--enable-protections") == 1

    trade = _trade(
        is_short=True,
        leverage=1.0,
        amount=1.0,
        open_rate=100.0,
        close_rate=99.0,
    )
    first = _metrics([trade], include_directional=True)
    second = _metrics([trade], include_directional=True)
    assert first == second
    assert optimizer.canonical_digest(first) == optimizer.canonical_digest(second)


def test_metrics_require_protection_and_window_evidence() -> None:
    trade = _trade(
        is_short=False,
        leverage=1.0,
        amount=1.0,
        open_rate=100.0,
        close_rate=101.0,
    )
    result = _result([trade])
    del result["canonical_execution_evidence"]["window_data_digest"]
    with pytest.raises(optimizer.Blocked, match="protections and window evidence"):
        optimizer.metrics(
            result,
            FEE,
            SLIPPAGE,
            0.00075,
            0.0003,
            wallet=WALLET,
            stake_limit=STAKE,
            leverage_limit=2.0,
            sizing_digest="a" * 64,
        )
