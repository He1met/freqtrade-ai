from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any, Iterable, Mapping

from sqlalchemy.orm import Session

from app.models.backtest import BacktestResult


PROMOTION_VALIDATION_SCHEMA_VERSION = "1"
MIN_OOS_TRADES = 30
WALK_FORWARD_FOLDS = 3
MARKET_MOVE_THRESHOLD = 0.002


class StrategyPromotionValidationBlocked(ValueError):
    """Persisted Freqtrade evidence cannot support promotion validation."""


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise StrategyPromotionValidationBlocked(f"{name} is missing or invalid")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise StrategyPromotionValidationBlocked(
            f"{name} is missing or invalid"
        ) from exc
    if not math.isfinite(number):
        raise StrategyPromotionValidationBlocked(f"{name} is missing or invalid")
    return number


def _timestamp(value: Any, name: str) -> int:
    number = _finite(value, name)
    if number <= 0:
        raise StrategyPromotionValidationBlocked(f"{name} is missing or invalid")
    return int(number)


def _iso_from_millis(value: int) -> str:
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()


def _market_state(trade: Mapping[str, Any]) -> str:
    open_rate = _finite(trade.get("open_rate"), "trade open_rate")
    close_rate = _finite(trade.get("close_rate"), "trade close_rate")
    if open_rate <= 0 or close_rate <= 0:
        raise StrategyPromotionValidationBlocked("trade market rates must be positive")
    move = close_rate / open_rate - 1
    if move >= MARKET_MOVE_THRESHOLD:
        return "bull"
    if move <= -MARKET_MOVE_THRESHOLD:
        return "bear"
    return "range"


def _profit_pct(trades: Iterable[Mapping[str, Any]], starting_balance: float) -> float:
    return sum(_finite(trade.get("profit_abs"), "trade profit_abs") for trade in trades) / starting_balance


class StrategyPromotionValidationService:
    """Derive promotion proof only from one persisted Freqtrade result.

    A generated strategy is static before this backtest starts, so the final
    chronological holdout is out-of-sample relative to any earlier trade
    observations. Walk-forward evidence is calculated from three consecutive
    folds, while market states come from the underlying open-to-close move of
    persisted trades rather than from a request label.
    """

    def __init__(self, db: Session) -> None:
        self.db = db

    def attach(self, backtest_result_id: int, *, commit: bool = True) -> dict:
        result = self.db.get(BacktestResult, backtest_result_id)
        if result is None:
            raise StrategyPromotionValidationBlocked(
                "backtest result does not exist"
            )
        metrics = result.metrics_snapshot
        if not isinstance(metrics, dict):
            raise StrategyPromotionValidationBlocked(
                "backtest metrics snapshot is missing"
            )
        parser = metrics.get("parser_metadata")
        if (
            not isinstance(parser, dict)
            or parser.get("source") != "freqtrade_result_parser"
        ):
            raise StrategyPromotionValidationBlocked(
                "promotion validation requires persisted Freqtrade parser provenance"
            )
        raw_trades = metrics.get("trades")
        if not isinstance(raw_trades, list) or not raw_trades:
            raise StrategyPromotionValidationBlocked(
                "promotion validation requires persisted Freqtrade trades"
            )
        trades: list[dict[str, Any]] = []
        for raw_trade in raw_trades:
            if not isinstance(raw_trade, dict):
                raise StrategyPromotionValidationBlocked(
                    "persisted trade evidence is malformed"
                )
            for fee_field in ("fee_open", "fee_close", "funding_fees"):
                _finite(raw_trade.get(fee_field), f"trade {fee_field}")
            _finite(raw_trade.get("profit_ratio"), "trade profit_ratio")
            _finite(raw_trade.get("profit_abs"), "trade profit_abs")
            _timestamp(raw_trade.get("open_timestamp"), "trade open_timestamp")
            _timestamp(raw_trade.get("close_timestamp"), "trade close_timestamp")
            _market_state(raw_trade)
            trades.append(raw_trade)
        trades.sort(
            key=lambda trade: (
                _timestamp(trade["close_timestamp"], "trade close_timestamp"),
                _timestamp(trade["open_timestamp"], "trade open_timestamp"),
            )
        )
        starting_balance = _finite(metrics.get("starting_balance"), "starting_balance")
        if starting_balance <= 0:
            raise StrategyPromotionValidationBlocked(
                "starting_balance must be positive"
            )

        holdout_count = max(MIN_OOS_TRADES, math.ceil(len(trades) * 0.2))
        holdout_count = min(len(trades), holdout_count)
        holdout = trades[-holdout_count:]
        holdout_profit = _profit_pct(holdout, starting_balance)

        fold_size = math.ceil(len(trades) / WALK_FORWARD_FOLDS)
        folds = []
        for fold_index in range(WALK_FORWARD_FOLDS):
            fold = trades[fold_index * fold_size : (fold_index + 1) * fold_size]
            if not fold:
                continue
            folds.append(
                {
                    "fold": fold_index + 1,
                    "total_trades": len(fold),
                    "profit_pct": _profit_pct(fold, starting_balance),
                    "started_at": _iso_from_millis(
                        _timestamp(fold[0]["open_timestamp"], "trade open_timestamp")
                    ),
                    "completed_at": _iso_from_millis(
                        _timestamp(fold[-1]["close_timestamp"], "trade close_timestamp")
                    ),
                }
            )
        market_states = sorted({_market_state(trade) for trade in trades})
        evidence = {
            "schema_version": PROMOTION_VALIDATION_SCHEMA_VERSION,
            "source": "persisted_freqtrade_backtest_trades",
            "source_backtest_result_id": result.id,
            "source_trade_count": len(trades),
            "fee_fields_verified": True,
            "net_of_costs": True,
            "out_of_sample": {
                "passed": (
                    len(holdout) >= MIN_OOS_TRADES and holdout_profit > 0
                ),
                "profit_pct": holdout_profit,
                "total_trades": len(holdout),
                "partition": "chronological_holdout_last_20_percent",
                "started_at": _iso_from_millis(
                    _timestamp(
                        holdout[0]["open_timestamp"],
                        "trade open_timestamp",
                    )
                ),
                "completed_at": _iso_from_millis(
                    _timestamp(
                        holdout[-1]["close_timestamp"],
                        "trade close_timestamp",
                    )
                ),
            },
            "walk_forward": {
                "passed": (
                    len(folds) == WALK_FORWARD_FOLDS
                    and all(fold["profit_pct"] > 0 for fold in folds)
                    and len(market_states) >= 3
                ),
                "market_states": market_states,
                "market_state_method": (
                    "persisted_trade_underlying_open_close_move"
                ),
                "market_move_threshold": MARKET_MOVE_THRESHOLD,
                "folds": folds,
            },
        }
        result.metrics_snapshot = {
            **metrics,
            "promotion_evidence": evidence,
        }
        if commit:
            self.db.commit()
            self.db.refresh(result)
        else:
            self.db.flush()
        return evidence
