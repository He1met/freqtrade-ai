from __future__ import annotations

import json
import os
from json import JSONDecodeError
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from app.core.config import REPO_ROOT


router = APIRouter(prefix="/api", tags=["mvp-runtime"])

DEFAULT_RUNTIME_DATA_PATH = REPO_ROOT / "tmp" / "runtime" / "mvp-data.json"

SECTION_KEYS = {
    "strategies",
    "generationRuns",
    "backtestRuns",
    "backtestTasks",
    "ranking",
    "failureReasons",
    "versionLineage",
}


def runtime_data_path() -> Path:
    configured = os.getenv("FREQTRADE_AI_MVP_DATA_PATH")
    if configured:
        return Path(configured).expanduser().resolve()
    return DEFAULT_RUNTIME_DATA_PATH


def load_runtime_data() -> dict[str, Any]:
    path = runtime_data_path()
    if not path.exists():
        raise HTTPException(
            status_code=503,
            detail=(
                "runtime data is not available; run "
                "`python3 scripts/seed_runtime_mvp.py` first"
            ),
        )

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"runtime data is invalid JSON: {path}") from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=500, detail="runtime data root must be an object")

    return payload


def section(name: str) -> list[dict[str, Any]]:
    payload = load_runtime_data()
    value = payload.get(name)
    if not isinstance(value, list):
        raise HTTPException(status_code=500, detail=f"runtime data section is missing or invalid: {name}")
    return value


@router.get("/runtime-data")
@router.get("/mvp/runtime-data")
def read_runtime_data() -> dict[str, Any]:
    payload = load_runtime_data()
    missing = sorted(key for key in SECTION_KEYS if not isinstance(payload.get(key), list))
    if missing:
        raise HTTPException(
            status_code=500,
            detail=f"runtime data sections are missing or invalid: {', '.join(missing)}",
        )
    return payload


@router.get("/strategies")
@router.get("/mvp/strategies")
def read_strategies() -> list[dict[str, Any]]:
    return section("strategies")


@router.get("/generation-runs")
@router.get("/strategy-generation-runs")
@router.get("/mvp/generation-runs")
def read_generation_runs() -> list[dict[str, Any]]:
    return section("generationRuns")


@router.get("/backtest-runs")
@router.get("/mvp/backtest-runs")
def read_backtest_runs() -> list[dict[str, Any]]:
    return section("backtestRuns")


@router.get("/backtest-tasks")
@router.get("/mvp/backtest-tasks")
def read_backtest_tasks() -> list[dict[str, Any]]:
    return section("backtestTasks")


@router.get("/ranking")
@router.get("/strategy-ranking")
@router.get("/mvp/ranking")
def read_ranking() -> list[dict[str, Any]]:
    return section("ranking")


@router.get("/strategy-failure-reasons")
@router.get("/mvp/strategy-failure-reasons")
def read_strategy_failure_reasons() -> list[dict[str, Any]]:
    return section("failureReasons")


@router.get("/strategy-version-lineage")
@router.get("/strategy-versions/lineage")
@router.get("/mvp/strategy-version-lineage")
def read_strategy_version_lineage() -> list[dict[str, Any]]:
    return section("versionLineage")
