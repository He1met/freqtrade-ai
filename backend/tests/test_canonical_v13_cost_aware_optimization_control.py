from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CONTROL = ROOT / "scripts/canonical_v13_cost_aware_optimization.py"
SPEC = importlib.util.spec_from_file_location("canonical_cost_control", CONTROL)
assert SPEC is not None and SPEC.loader is not None
control = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(control)


def test_objective_digest_binds_sizing_and_supersession_provenance() -> None:
    plan = {
        "data_isolation": {"train": {}, "validation": {}, "holdout": {}},
        "execution": {"trial_budget": 96},
        "costs": {"qualification_fee_rate": 0.0005},
        "hard_gates": {"trade_count": {"threshold": 30}},
        "objective": {"name": "frozen"},
        "families": [{"family_key": "frozen-family"}],
        "target": {"leverage": 1.0},
        "position_sizing": {
            "dry_run_wallet_quote": 10_000,
            "stake_amount_quote": 100,
        },
        "supersession": {
            "superseded_plan_digest": "a" * 64,
            "superseded_optimization_run_id": "ab295c0b-b723-4931-b161-710da10d4341",
            "defect_fix_release_digest": "b" * 40,
        },
    }
    observed = control.objective(
        plan,
        market_snapshot_id="snapshot",
        market_snapshot_digest="c" * 64,
        market_artifact_digest="d" * 64,
        executor_image_digest="sha256:" + "e" * 64,
    )

    assert observed["plan_digest"] == control.canonical_digest(plan)
    assert observed["position_sizing"] == plan["position_sizing"]
    assert observed["supersession"] == plan["supersession"]
    assert observed["holdout_results_observed"] is False


def test_objective_binds_multi_asset_target_allocation_and_market_inputs() -> None:
    plan = {
        "data_isolation": {"train": {}, "validation": {}, "holdout": {}},
        "execution": {"trial_budget": 96},
        "costs": {"qualification_fee_rate": 0.0005},
        "hard_gates": {"trade_count": {"threshold": 30}},
        "objective": {"name": "portfolio"},
        "families": [{"family_key": "frozen-family"}],
        "target_set": [{"instrument": "BTC-USDT-SWAP"}],
        "portfolio_allocation": {"wallet_quote": 10_000},
        "portfolio_selection": {"minimum_nonnegative_validation_assets": 2},
        "market_inputs": {"BTC-USDT-SWAP": {"snapshot_digest": "a" * 64}},
    }

    observed = control.objective(
        plan,
        market_snapshot_id="aggregate-snapshot",
        market_snapshot_digest="b" * 64,
        market_artifact_digest="c" * 64,
        executor_image_digest="sha256:" + "d" * 64,
    )

    assert observed["target_set"] == plan["target_set"]
    assert observed["portfolio_allocation"] == plan["portfolio_allocation"]
    assert observed["portfolio_selection"] == plan["portfolio_selection"]
    assert observed["market_inputs"] == plan["market_inputs"]
