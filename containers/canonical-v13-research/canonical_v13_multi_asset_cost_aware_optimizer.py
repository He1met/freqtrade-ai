#!/usr/bin/env python3
"""Network-none equal-weight BTC/ETH/SOL cost-aware OOS optimizer."""

from __future__ import annotations

import argparse
from copy import deepcopy
from hashlib import sha256
import json
import math
from pathlib import Path
from typing import Any

import canonical_v13_cost_aware_optimizer as single


CONTRACT = "canonical-v13-multi-asset-cost-aware-oos-optimization-result-v1"
PLAN_CONTRACT = "canonical-v13-multi-asset-cost-aware-oos-optimization-plan-v1"
EXPECTED_INSTRUMENTS = (
    "BTC-USDT-SWAP",
    "ETH-USDT-SWAP",
    "SOL-USDT-SWAP",
)


def _number(value: object, *, field: str, positive: bool = False) -> float:
    try:
        observed = float(value)
    except (TypeError, ValueError) as exc:
        raise single.Blocked(f"{field} is invalid") from exc
    if not math.isfinite(observed) or (positive and observed <= 0):
        raise single.Blocked(f"{field} is invalid")
    return observed


def _targets(plan: dict[str, object]) -> list[dict[str, object]]:
    raw = plan.get("target_set")
    if not isinstance(raw, list) or len(raw) != 3:
        raise single.Blocked("target set must contain exactly three assets")
    targets = [dict(item) for item in raw if isinstance(item, dict)]
    if len(targets) != 3 or tuple(item.get("instrument") for item in targets) != EXPECTED_INSTRUMENTS:
        raise single.Blocked("target set identity or order drifted")
    for target in targets:
        if (
            target.get("timeframe") != "15m"
            or target.get("data_kind") != "futures"
            or target.get("direction") != "LONG_ONLY"
            or _number(target.get("leverage"), field="target leverage") != 1.0
        ):
            raise single.Blocked("target contract drifted")
    return targets


def _sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _market_inputs(
    plan: dict[str, object],
    market_paths: dict[str, Path],
    metadata_paths: dict[str, Path],
) -> dict[str, dict[str, str]]:
    raw = plan.get("market_inputs")
    if not isinstance(raw, dict) or set(raw) != set(EXPECTED_INSTRUMENTS):
        raise single.Blocked("market input lineage does not match target set")
    observed: dict[str, dict[str, str]] = {}
    for instrument in EXPECTED_INSTRUMENTS:
        item = raw[instrument]
        if not isinstance(item, dict):
            raise single.Blocked("market input lineage is invalid")
        normalized = {
            key: str(item.get(key, ""))
            for key in (
                "snapshot_id",
                "snapshot_digest",
                "artifact_digest",
                "metadata_digest",
            )
        }
        if not normalized["snapshot_id"] or any(
            len(normalized[key]) != 64
            or any(character not in "0123456789abcdef" for character in normalized[key])
            for key in ("snapshot_digest", "artifact_digest", "metadata_digest")
        ):
            raise single.Blocked("market input lineage is invalid")
        if _sha256(market_paths[instrument]) != normalized["artifact_digest"]:
            raise single.Blocked("market artifact digest drifted")
        if _sha256(metadata_paths[instrument]) != normalized["metadata_digest"]:
            raise single.Blocked("market metadata digest drifted")
        observed[instrument] = normalized
    return observed


def _allocation(plan: dict[str, object], targets: list[dict[str, object]]) -> dict[str, object]:
    raw = plan.get("portfolio_allocation")
    if not isinstance(raw, dict):
        raise single.Blocked("portfolio allocation is missing")
    wallet = _number(raw.get("wallet_quote"), field="portfolio wallet", positive=True)
    stake = _number(raw.get("total_stake_quote"), field="portfolio stake", positive=True)
    weights = raw.get("weights")
    if not isinstance(weights, dict) or set(weights) != {str(item["instrument"]) for item in targets}:
        raise single.Blocked("portfolio weights do not match target set")
    numeric_weights = {key: _number(value, field=f"weight {key}", positive=True) for key, value in weights.items()}
    if not math.isclose(sum(numeric_weights.values()), 1.0, rel_tol=0.0, abs_tol=1e-12):
        raise single.Blocked("portfolio weights must sum to one")
    if any(not math.isclose(value, 1.0 / 3.0, rel_tol=0.0, abs_tol=1e-12) for value in numeric_weights.values()):
        raise single.Blocked("portfolio allocation must be equal weight")
    maximum_fraction = _number(
        raw.get("maximum_simultaneous_notional_wallet_fraction"),
        field="portfolio simultaneous notional fraction",
        positive=True,
    )
    if not math.isclose(stake / wallet, maximum_fraction, rel_tol=0.0, abs_tol=1e-12):
        raise single.Blocked("portfolio stake and simultaneous notional cap drifted")
    if raw.get("maximum_open_positions") != 3 or raw.get("maximum_open_positions_per_asset") != 1:
        raise single.Blocked("portfolio position capacity drifted")
    return {
        "wallet_quote": wallet,
        "total_stake_quote": stake,
        "weights": numeric_weights,
        "maximum_simultaneous_notional_wallet_fraction": maximum_fraction,
        "maximum_open_positions": 3,
        "maximum_open_positions_per_asset": 1,
    }


def _aggregate_window(
    members: dict[str, dict[str, object]],
    *,
    weights: dict[str, float],
) -> dict[str, object]:
    weighted = lambda key: sum(weights[name] * float(metrics[key]) for name, metrics in members.items())
    positive_profit = weighted("positive_profit_account")
    largest_trade = max(
        weights[name] * float(metrics["largest_winning_trade_account"])
        for name, metrics in members.items()
    )
    positive_net = {name: max(0.0, float(metrics["net_return_after_cost"])) for name, metrics in members.items()}
    positive_net_total = sum(positive_net.values())
    asset_concentration = (
        max(positive_net.values()) / positive_net_total if positive_net_total > 0 else 1.0
    )
    values: dict[str, object] = {
        "trade_count": sum(int(metrics["trade_count"]) for metrics in members.values()),
        "fee_inclusive_return": weighted("fee_inclusive_return"),
        "modeled_fee_cost": weighted("modeled_fee_cost"),
        "modeled_slippage_cost": weighted("modeled_slippage_cost"),
        "net_return_after_cost": weighted("net_return_after_cost"),
        "sensitivity_net_after_cost": weighted("sensitivity_net_after_cost"),
        "turnover_account": weighted("turnover_account"),
        "maximum_drawdown": weighted("maximum_drawdown"),
        "positive_profit_account": positive_profit,
        "largest_winning_trade_account": largest_trade,
        "top_trade_profit_share": largest_trade / positive_profit if positive_profit > 0 else 1.0,
        "nonnegative_asset_count": sum(float(item["net_return_after_cost"]) >= 0 for item in members.values()),
        "worst_asset_net_after_cost": min(float(item["net_return_after_cost"]) for item in members.values()),
        "maximum_asset_positive_net_share": asset_concentration,
        "per_asset": members,
    }
    if not all(math.isfinite(float(value)) for key, value in values.items() if key not in {"per_asset"}):
        raise single.Blocked("non-finite portfolio metrics")
    return values


def aggregate_results(
    plan: dict[str, object],
    results: dict[str, dict[str, object]],
    allocation: dict[str, object],
) -> dict[str, object]:
    targets = _targets(plan)
    instruments = [str(item["instrument"]) for item in targets]
    if set(results) != set(instruments):
        raise single.Blocked("result target set drifted")
    weights = dict(allocation["weights"])
    by_instrument = {
        instrument: {int(row["trial_number"]): row for row in results[instrument]["trials"]}
        for instrument in instruments
    }
    if any(set(rows) != set(range(1, 97)) for rows in by_instrument.values()):
        raise single.Blocked("trial identity set drifted")
    rules = plan.get("portfolio_selection")
    if not isinstance(rules, dict):
        raise single.Blocked("portfolio selection contract is missing")
    min_trades = int(rules["minimum_aggregate_trades_30d"])
    max_dd = _number(rules["maximum_portfolio_drawdown"], field="portfolio drawdown")
    min_nonnegative = int(rules["minimum_nonnegative_validation_assets"])
    max_asset_concentration = _number(rules["maximum_validation_asset_positive_net_share"], field="asset concentration")
    max_trade_concentration = _number(rules["maximum_validation_top_trade_profit_share"], field="trade concentration")
    min_asset_net = _number(rules["minimum_validation_asset_net_after_cost"], field="minimum asset net")
    penalties = plan["objective"]["penalties"]
    rows: list[dict[str, object]] = []
    for number in range(1, 97):
        members = [by_instrument[instrument][number] for instrument in instruments]
        first = members[0]
        if any(
            row["family_key"] != first["family_key"]
            or row["parameters_json"] != first["parameters_json"]
            or row["strategy_source_digest"] != first["strategy_source_digest"]
            for row in members[1:]
        ):
            raise single.Blocked("shared source or parameter identity drifted across assets")
        train = _aggregate_window(
            {instrument: dict(by_instrument[instrument][number]["metrics_json"]["train"]) for instrument in instruments},
            weights=weights,
        )
        validation = _aggregate_window(
            {instrument: dict(by_instrument[instrument][number]["metrics_json"]["validation"]) for instrument in instruments},
            weights=weights,
        )
        scaled_train_trades = float(train["trade_count"]) / 4.0
        drift = abs(float(train["net_return_after_cost"]) / 4.0 - float(validation["net_return_after_cost"]))
        eligible = (
            float(train["net_return_after_cost"]) > 0
            and float(validation["net_return_after_cost"]) > 0
            and scaled_train_trades >= min_trades
            and int(validation["trade_count"]) >= min_trades
            and float(train["maximum_drawdown"]) <= max_dd
            and float(validation["maximum_drawdown"]) <= max_dd
            and int(validation["nonnegative_asset_count"]) >= min_nonnegative
            and float(validation["worst_asset_net_after_cost"]) >= min_asset_net
            and float(validation["maximum_asset_positive_net_share"]) <= max_asset_concentration
            and float(validation["top_trade_profit_share"]) <= max_trade_concentration
        )
        low_trade_penalty = float(penalties["low_trade_count"]) * (
            max(0.0, min_trades - scaled_train_trades)
            + max(0.0, min_trades - float(validation["trade_count"]))
        ) / min_trades * 0.01
        turnover_penalty = float(penalties["turnover"]) * (
            float(validation["modeled_fee_cost"]) + float(validation["modeled_slippage_cost"])
        )
        asset_concentration_penalty = float(penalties["asset_concentration"]) * max(
            0.0,
            float(validation["maximum_asset_positive_net_share"]) - max_asset_concentration,
        )
        objective = (
            min(float(train["net_return_after_cost"]) / 4.0, float(validation["net_return_after_cost"]))
            - float(penalties["train_validation_drift"]) * drift
            - float(penalties["maximum_drawdown"]) * float(validation["maximum_drawdown"])
            - float(penalties["profit_concentration"]) * max(0.0, float(validation["top_trade_profit_share"]) - max_trade_concentration)
            - asset_concentration_penalty
            - turnover_penalty
            - low_trade_penalty
        )
        rows.append(
            {
                "trial_number": number,
                "family_key": first["family_key"],
                "parameters_json": first["parameters_json"],
                "metrics_json": {
                    "train": train,
                    "validation": validation,
                    "scaled_train_trade_count_30d": scaled_train_trades,
                    "train_validation_drift": drift,
                    "turnover_penalty": turnover_penalty,
                    "low_trade_count_penalty": low_trade_penalty,
                    "asset_concentration_penalty": asset_concentration_penalty,
                    "objective": objective,
                    "eligible": eligible,
                    "rule_complexity": first["metrics_json"]["rule_complexity"],
                },
                "strategy_class": first["strategy_class"],
                "strategy_source": first["strategy_source"],
                "strategy_source_digest": first["strategy_source_digest"],
            }
        )
    ranked = sorted(
        (row for row in rows if row["metrics_json"]["eligible"]),
        key=lambda row: (
            -float(row["metrics_json"]["validation"]["net_return_after_cost"]),
            -float(row["metrics_json"]["validation"]["worst_asset_net_after_cost"]),
            float(row["metrics_json"]["train_validation_drift"]),
            float(row["metrics_json"]["validation"]["maximum_drawdown"]),
            int(row["metrics_json"]["rule_complexity"]),
            int(row["trial_number"]),
        ),
    )
    finalists: list[int] = []
    used_families: set[str] = set()
    for row in ranked:
        family = str(row["family_key"])
        if family in used_families:
            continue
        finalists.append(int(row["trial_number"]))
        used_families.add(family)
        if len(finalists) == int(plan["execution"]["finalist_limit"]):
            break
    evidence = [
        {"trial_number": row["trial_number"], "parameters_json": row["parameters_json"], "metrics_json": row["metrics_json"]}
        for row in rows
    ]
    return {
        "contract": CONTRACT,
        "plan_digest": single.canonical_digest(plan),
        "target_set_digest": single.canonical_digest(targets),
        "allocation_contract": allocation,
        "allocation_digest": single.canonical_digest(allocation),
        "child_result_digests": {key: single.canonical_digest(value) for key, value in results.items()},
        "trial_count": len(rows),
        "selected_trial_numbers": finalists,
        "trial_evidence_digest": single.canonical_digest(evidence),
        "trials": rows,
        "execution_side_effects": 0,
        "credential_access": "NONE",
        "trading_capability": "TRADING_DISABLED",
    }


def evaluate(
    plan: dict[str, object],
    market_paths: dict[str, Path],
    metadata_paths: dict[str, Path],
) -> dict[str, object]:
    if plan.get("contract") != PLAN_CONTRACT:
        raise single.Blocked("multi-asset plan contract drifted")
    targets = _targets(plan)
    allocation = _allocation(plan, targets)
    instruments = {str(item["instrument"]) for item in targets}
    if set(market_paths) != instruments or set(metadata_paths) != instruments:
        raise single.Blocked("market or metadata input target set drifted")
    market_inputs = _market_inputs(plan, market_paths, metadata_paths)
    results: dict[str, dict[str, object]] = {}
    for target in targets:
        instrument = str(target["instrument"])
        weight = float(allocation["weights"][instrument])
        child = deepcopy(plan)
        child["contract"] = "canonical-v13-cost-aware-oos-optimization-plan-v1"
        child["batch_key"] = f"{plan['batch_key']}:{instrument}"
        child["target"] = target
        child["position_sizing"] = {
            "dry_run_wallet_quote": float(allocation["wallet_quote"]) * weight,
            "stake_amount_quote": float(allocation["total_stake_quote"]) * weight,
            "maximum_nominal_wallet_fraction": float(allocation["maximum_simultaneous_notional_wallet_fraction"]),
            "maximum_open_trades": 1,
            "position_adjustment": False,
        }
        child.pop("target_set", None)
        child.pop("portfolio_allocation", None)
        child.pop("portfolio_selection", None)
        results[instrument] = single.evaluate(child, market_paths[instrument], metadata_paths[instrument])
    aggregated = aggregate_results(plan, results, allocation)
    aggregated["market_inputs"] = market_inputs
    aggregated["market_inputs_digest"] = single.canonical_digest(market_inputs)
    return aggregated


def _mapping(values: list[str], *, field: str) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        instrument, separator, path = value.partition("=")
        if not separator or instrument in result or not path:
            raise single.Blocked(f"{field} mapping is invalid")
        result[instrument] = Path(path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--market", action="append", default=[], required=True)
    parser.add_argument("--metadata", action="append", default=[], required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(
        single.load_json(args.plan),
        _mapping(args.market, field="market"),
        _mapping(args.metadata, field="metadata"),
    )
    args.output.write_text(json.dumps(result, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
