#!/usr/bin/env python3
"""Materialize the deterministic 2x10 Blueprint-native research library."""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.schemas.strategy_blueprint import StrategyBlueprint  # noqa: E402
from app.services.strategy_renderer import StrategyCodeRenderer  # noqa: E402


def indicator(name: str, kind: str, period: int, source: str = "close") -> dict[str, Any]:
    payload: dict[str, Any] = {"name": name, "kind": kind, "period": period}
    if source != "close":
        payload["source"] = source
    return payload


def relation(indicator_name: str, operator: str, compare: str) -> dict[str, Any]:
    return {
        "indicator": indicator_name,
        "operator": operator,
        "compare_indicator": compare,
    }


def threshold(indicator_name: str, operator: str, value: float) -> dict[str, Any]:
    return {"indicator": indicator_name, "operator": operator, "value": value}


def trend(indicator_name: str, operator: str, lookback: int) -> dict[str, Any]:
    return {"indicator": indicator_name, "operator": operator, "lookback": lookback}


STRUCTURES: tuple[dict[str, Any], ...] = (
    {
        "stem": "pullback_reclaim",
        "label": "Pullback Reclaim",
        "family": "PULLBACK_TREND_CONTINUATION",
        "indicators": [indicator("close_raw", "raw", 1), indicator("ema_mid", "ema", 32), indicator("ema_slow", "ema", 96), indicator("rsi", "rsi", 14)],
        "entry_rules": [relation("close_raw", "crosses_above", "ema_mid"), relation("ema_mid", ">", "ema_slow"), threshold("rsi", ">", 52.0)],
        "short_entry_rules": [relation("close_raw", "crosses_below", "ema_mid"), relation("ema_mid", "<", "ema_slow"), threshold("rsi", "<", 48.0)],
        "exit_rules": [relation("ema_mid", "<", "ema_slow")],
        "short_exit_rules": [relation("ema_mid", ">", "ema_slow")],
    },
    {
        "stem": "atr_trend_breakout",
        "label": "ATR Trend Breakout",
        "family": "VOLATILITY_BREAKOUT",
        "indicators": [indicator("close_raw", "raw", 1), indicator("atr", "atr", 14), indicator("ema_fast", "ema", 24), indicator("ema_slow", "ema", 72)],
        "entry_rules": [trend("atr", "rising", 4), relation("close_raw", "crosses_above", "ema_slow"), relation("ema_fast", ">", "ema_slow")],
        "short_entry_rules": [trend("atr", "rising", 4), relation("close_raw", "crosses_below", "ema_slow"), relation("ema_fast", "<", "ema_slow")],
        "exit_rules": [relation("ema_fast", "<", "ema_slow")],
        "short_exit_rules": [relation("ema_fast", ">", "ema_slow")],
    },
    {
        "stem": "rsi_mean_recovery",
        "label": "RSI Mean Recovery",
        "family": "MEAN_REVERSION",
        "indicators": [indicator("close_raw", "raw", 1), indicator("mean", "sma", 48), indicator("rsi", "rsi", 14)],
        "entry_rules": [relation("close_raw", "<", "mean"), threshold("rsi", "<", 30.0), trend("rsi", "rising", 2)],
        "short_entry_rules": [relation("close_raw", ">", "mean"), threshold("rsi", ">", 70.0), trend("rsi", "falling", 2)],
        "exit_rules": [relation("close_raw", ">=", "mean")],
        "short_exit_rules": [relation("close_raw", "<=", "mean")],
    },
    {
        "stem": "volume_rsi_impulse",
        "label": "Volume RSI Impulse",
        "family": "MOMENTUM_VOLUME_CONFIRMATION",
        "indicators": [indicator("close_raw", "raw", 1), indicator("volume_raw", "raw", 1, "volume"), indicator("volume_mean", "sma", 36, "volume"), indicator("ema", "ema", 50), indicator("rsi", "rsi", 14)],
        "entry_rules": [relation("volume_raw", ">", "volume_mean"), threshold("rsi", ">", 58.0), relation("close_raw", ">", "ema")],
        "short_entry_rules": [relation("volume_raw", ">", "volume_mean"), threshold("rsi", "<", 42.0), relation("close_raw", "<", "ema")],
        "exit_rules": [threshold("rsi", "<", 50.0)],
        "short_exit_rules": [threshold("rsi", ">", 50.0)],
    },
    {
        "stem": "fast_ema_continuation",
        "label": "Fast EMA Continuation",
        "family": "PULLBACK_TREND_CONTINUATION",
        "indicators": [indicator("close_raw", "raw", 1), indicator("ema_fast", "ema", 20), indicator("ema_regime", "ema", 100), indicator("rsi", "rsi", 14)],
        "entry_rules": [relation("close_raw", "crosses_above", "ema_fast"), relation("ema_fast", ">", "ema_regime"), threshold("rsi", ">", 50.0)],
        "short_entry_rules": [relation("close_raw", "crosses_below", "ema_fast"), relation("ema_fast", "<", "ema_regime"), threshold("rsi", "<", 50.0)],
        "exit_rules": [relation("close_raw", "<", "ema_regime")],
        "short_exit_rules": [relation("close_raw", ">", "ema_regime")],
    },
    {
        "stem": "fast_rsi_exhaustion",
        "label": "Fast RSI Exhaustion",
        "family": "MEAN_REVERSION",
        "indicators": [indicator("close_raw", "raw", 1), indicator("mean", "sma", 96), indicator("rsi_fast", "rsi", 7)],
        "entry_rules": [relation("close_raw", "<", "mean"), threshold("rsi_fast", "<", 22.0)],
        "short_entry_rules": [relation("close_raw", ">", "mean"), threshold("rsi_fast", ">", 78.0)],
        "exit_rules": [threshold("rsi_fast", ">", 50.0)],
        "short_exit_rules": [threshold("rsi_fast", "<", 50.0)],
    },
    {
        "stem": "participation_momentum",
        "label": "Participation Momentum",
        "family": "MOMENTUM_VOLUME_CONFIRMATION",
        "indicators": [indicator("close_raw", "raw", 1), indicator("volume_raw", "raw", 1, "volume"), indicator("volume_mean", "sma", 24, "volume"), indicator("ema", "ema", 80), indicator("rsi_fast", "rsi", 10)],
        "entry_rules": [relation("volume_raw", ">", "volume_mean"), trend("rsi_fast", "rising", 3), threshold("rsi_fast", ">", 55.0), relation("close_raw", ">", "ema")],
        "short_entry_rules": [relation("volume_raw", ">", "volume_mean"), trend("rsi_fast", "falling", 3), threshold("rsi_fast", "<", 45.0), relation("close_raw", "<", "ema")],
        "exit_rules": [threshold("rsi_fast", "<", 50.0)],
        "short_exit_rules": [threshold("rsi_fast", ">", 50.0)],
    },
    {
        "stem": "ema_persistence_cross",
        "label": "EMA Persistence Cross",
        "family": "TREND_BREAKOUT_FOLLOWING",
        "indicators": [indicator("ema_fast", "ema", 18), indicator("ema_slow", "ema", 72), indicator("rsi", "rsi", 14)],
        "entry_rules": [relation("ema_fast", "crosses_above", "ema_slow"), threshold("rsi", ">", 55.0)],
        "short_entry_rules": [relation("ema_fast", "crosses_below", "ema_slow"), threshold("rsi", "<", 45.0)],
        "exit_rules": [relation("ema_fast", "<", "ema_slow")],
        "short_exit_rules": [relation("ema_fast", ">", "ema_slow")],
    },
    {
        "stem": "atr_volume_momentum",
        "label": "ATR Volume Momentum",
        "family": "MOMENTUM_VOLUME_CONFIRMATION",
        "indicators": [indicator("close_raw", "raw", 1), indicator("volume_raw", "raw", 1, "volume"), indicator("volume_mean", "sma", 48, "volume"), indicator("atr", "atr", 14), indicator("ema_fast", "ema", 34), indicator("ema_slow", "ema", 100)],
        "entry_rules": [relation("volume_raw", ">", "volume_mean"), trend("atr", "rising", 3), relation("ema_fast", ">", "ema_slow"), relation("close_raw", ">", "ema_fast")],
        "short_entry_rules": [relation("volume_raw", ">", "volume_mean"), trend("atr", "rising", 3), relation("ema_fast", "<", "ema_slow"), relation("close_raw", "<", "ema_fast")],
        "exit_rules": [relation("ema_fast", "<", "ema_slow")],
        "short_exit_rules": [relation("ema_fast", ">", "ema_slow")],
    },
    {
        "stem": "range_liquidity_cross",
        "label": "Range Liquidity Cross",
        "family": "RANGE_LIQUIDITY_FILTER",
        "indicators": [indicator("close_raw", "raw", 1), indicator("volume_raw", "raw", 1, "volume"), indicator("volume_mean", "sma", 72, "volume"), indicator("range_mean", "sma", 32), indicator("rsi", "rsi", 14)],
        "entry_rules": [relation("volume_raw", ">", "volume_mean"), relation("close_raw", "crosses_above", "range_mean"), threshold("rsi", "<", 60.0)],
        "short_entry_rules": [relation("volume_raw", ">", "volume_mean"), relation("close_raw", "crosses_below", "range_mean"), threshold("rsi", ">", 40.0)],
        "exit_rules": [threshold("rsi", ">", 65.0)],
        "short_exit_rules": [threshold("rsi", "<", 35.0)],
    },
)


def payload_for(structure: dict[str, Any], *, slot: int, timeframe: str) -> dict[str, Any]:
    timeframe_token = timeframe.upper().replace("M", "M")
    class_name = f"Candidate{slot:02d}{''.join(part.title() for part in structure['stem'].split('_'))}{timeframe_token}"
    return {
        "schema_version": "2",
        "name": f"{structure['label']} {timeframe}",
        "slug": f"candidate-{slot:02d}-{structure['stem'].replace('_', '-')}-{timeframe}",
        "class_name": class_name,
        "description": f"Deterministic {structure['family']} research structure for {timeframe} closed candles.",
        "timeframe": timeframe,
        "stoploss": -0.1,
        "minimal_roi": {"0": 0.03},
        "indicators": deepcopy(structure["indicators"]),
        "entry_rules": deepcopy(structure["entry_rules"]),
        "exit_rules": deepcopy(structure["exit_rules"]),
        "can_short": True,
        "short_entry_rules": deepcopy(structure["short_entry_rules"]),
        "short_exit_rules": deepcopy(structure["short_exit_rules"]),
        "regime_rules": [],
        "tags": ["formal-research", "blueprint-native", structure["family"].lower()],
    }


def main() -> int:
    root = REPO_ROOT / "research" / "strategy_candidates"
    renderer = StrategyCodeRenderer()
    written = 0
    for timeframe in ("5m", "15m"):
        target = root / timeframe
        target.mkdir(parents=True, exist_ok=True)
        for slot, structure in enumerate(STRUCTURES, start=1):
            payload = payload_for(structure, slot=slot, timeframe=timeframe)
            blueprint = StrategyBlueprint.model_validate(payload)
            stem = f"{slot:02d}_{structure['stem']}"
            (target / f"{stem}.blueprint.json").write_text(
                json.dumps(blueprint.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            (target / f"{stem}.py").write_text(renderer.render(blueprint), encoding="utf-8")
            written += 1
    print(f"materialized={written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
