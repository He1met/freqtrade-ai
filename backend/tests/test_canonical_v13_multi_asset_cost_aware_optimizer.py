from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import importlib.util
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
WORKER_ROOT = ROOT / "containers/canonical-v13-research"
sys.path.insert(0, str(WORKER_ROOT))
SPEC = importlib.util.spec_from_file_location(
    "canonical_multi_asset_optimizer",
    WORKER_ROOT / "canonical_v13_multi_asset_cost_aware_optimizer.py",
)
assert SPEC is not None and SPEC.loader is not None
optimizer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(optimizer)


INSTRUMENTS = ("BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP")


def _plan() -> dict[str, object]:
    return {
        "contract": optimizer.PLAN_CONTRACT,
        "batch_key": "frozen-multi-asset",
        "target_set": [
            {
                "instrument": instrument,
                "pair": instrument.replace("-", "/", 1).replace("-SWAP", ":USDT"),
                "timeframe": "15m",
                "data_kind": "futures",
                "direction": "LONG_ONLY",
                "leverage": 1.0,
            }
            for instrument in INSTRUMENTS
        ],
        "portfolio_allocation": {
            "wallet_quote": 10_000,
            "total_stake_quote": 100,
            "weights": {instrument: 1 / 3 for instrument in INSTRUMENTS},
            "maximum_simultaneous_notional_wallet_fraction": 0.01,
            "maximum_open_positions": 3,
            "maximum_open_positions_per_asset": 1,
        },
        "portfolio_selection": {
            "minimum_aggregate_trades_30d": 30,
            "maximum_portfolio_drawdown": 0.15,
            "minimum_nonnegative_validation_assets": 2,
            "maximum_validation_asset_positive_net_share": 0.70,
            "maximum_validation_top_trade_profit_share": 0.35,
            "minimum_validation_asset_net_after_cost": -0.0025,
        },
        "execution": {"finalist_limit": 3},
        "objective": {
            "penalties": {
                "turnover": 1.0,
                "low_trade_count": 4.0,
                "profit_concentration": 3.0,
                "asset_concentration": 2.0,
                "train_validation_drift": 2.0,
                "maximum_drawdown": 2.0,
            }
        },
    }


def _metrics(*, net: float, trades: int, positive: float = 0.03) -> dict[str, object]:
    return {
        "trade_count": trades,
        "fee_inclusive_return": net + 0.001,
        "modeled_fee_cost": 0.001,
        "modeled_slippage_cost": 0.0002,
        "net_return_after_cost": net,
        "sensitivity_net_after_cost": net - 0.0001,
        "turnover_account": 2.0,
        "maximum_drawdown": 0.02,
        "positive_profit_account": positive,
        "largest_winning_trade_account": positive * 0.1,
    }


def _results() -> dict[str, dict[str, object]]:
    results: dict[str, dict[str, object]] = {}
    for instrument in INSTRUMENTS:
        rows = []
        for number in range(1, 97):
            rows.append(
                {
                    "trial_number": number,
                    "family_key": ("trend-pullback", "volatility-breakout", "regime-momentum")[(number - 1) // 32],
                    "parameters_json": {"identity": number},
                    "metrics_json": {
                        "train": _metrics(net=0.01, trades=160),
                        "validation": _metrics(net=0.01, trades=40),
                        "rule_complexity": 6,
                    },
                    "strategy_class": f"Candidate{number}",
                    "strategy_source": f"class Candidate{number}: pass\n",
                    "strategy_source_digest": f"{number:064x}",
                }
            )
        results[instrument] = {"trials": rows}
    return results


def test_equal_weight_portfolio_uses_one_account_not_three() -> None:
    plan = _plan()
    allocation = optimizer._allocation(plan, optimizer._targets(plan))
    observed = optimizer.aggregate_results(plan, _results(), allocation)

    trial = observed["trials"][0]["metrics_json"]["validation"]
    assert trial["net_return_after_cost"] == pytest.approx(0.01)
    assert trial["trade_count"] == 120
    assert trial["maximum_drawdown"] == pytest.approx(0.02)
    assert trial["top_trade_profit_share"] == pytest.approx(1 / 30)
    assert trial["maximum_asset_positive_net_share"] == pytest.approx(1 / 3)
    assert observed["selected_trial_numbers"] == [1, 33, 65]
    assert allocation["total_stake_quote"] / allocation["wallet_quote"] == 0.01


def test_target_specific_parameter_or_source_drift_is_blocked() -> None:
    plan = _plan()
    results = _results()
    results["ETH-USDT-SWAP"]["trials"][0]["parameters_json"] = {"identity": 999}

    with pytest.raises(optimizer.single.Blocked, match="shared source or parameter"):
        optimizer.aggregate_results(
            plan,
            results,
            optimizer._allocation(plan, optimizer._targets(plan)),
        )


def test_validation_asset_loss_and_concentration_fail_closed() -> None:
    plan = _plan()
    results = _results()
    for number in range(96):
        results["SOL-USDT-SWAP"]["trials"][number]["metrics_json"]["validation"] = _metrics(
            net=-0.01,
            trades=40,
        )
        results["BTC-USDT-SWAP"]["trials"][number]["metrics_json"]["validation"] = _metrics(
            net=0.04,
            trades=40,
        )

    observed = optimizer.aggregate_results(
        plan,
        results,
        optimizer._allocation(plan, optimizer._targets(plan)),
    )

    validation = observed["trials"][0]["metrics_json"]["validation"]
    assert validation["net_return_after_cost"] > 0
    assert validation["worst_asset_net_after_cost"] == -0.01
    assert validation["maximum_asset_positive_net_share"] > 0.70
    assert observed["trials"][0]["metrics_json"]["eligible"] is False
    assert observed["selected_trial_numbers"] == []


def test_allocation_rejects_full_wallet_per_asset_semantics() -> None:
    plan = _plan()
    drifted = deepcopy(plan)
    drifted["portfolio_allocation"]["total_stake_quote"] = 300

    with pytest.raises(optimizer.single.Blocked, match="stake and simultaneous"):
        optimizer._allocation(drifted, optimizer._targets(drifted))


def test_market_inputs_bind_all_three_artifacts_and_metadata(tmp_path: Path) -> None:
    plan = _plan()
    markets: dict[str, Path] = {}
    metadata: dict[str, Path] = {}
    market_inputs: dict[str, dict[str, str]] = {}
    for instrument in INSTRUMENTS:
        market_path = tmp_path / f"{instrument}.feather"
        metadata_path = tmp_path / f"{instrument}.json"
        market_path.write_bytes(f"market:{instrument}".encode())
        metadata_path.write_bytes(f"metadata:{instrument}".encode())
        markets[instrument] = market_path
        metadata[instrument] = metadata_path
        market_inputs[instrument] = {
            "snapshot_id": f"snapshot:{instrument}",
            "snapshot_digest": sha256(f"snapshot:{instrument}".encode()).hexdigest(),
            "artifact_digest": sha256(market_path.read_bytes()).hexdigest(),
            "metadata_digest": sha256(metadata_path.read_bytes()).hexdigest(),
        }
    plan["market_inputs"] = market_inputs

    assert optimizer._market_inputs(plan, markets, metadata) == market_inputs

    market_inputs["SOL-USDT-SWAP"]["artifact_digest"] = "0" * 64
    with pytest.raises(optimizer.single.Blocked, match="market artifact digest drifted"):
        optimizer._market_inputs(plan, markets, metadata)
