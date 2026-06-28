from __future__ import annotations

from pathlib import Path

from app.core.config import REPO_ROOT, get_settings


def resolve_repo_path(path: str | Path) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    return (REPO_ROOT / candidate).resolve()


def ensure_runtime_dirs() -> None:
    settings = get_settings()
    for path in (
        settings.strategy_output_dir,
        settings.market_data_dir,
        settings.backtest_result_dir,
        settings.log_dir,
        settings.tmp_freqtrade_config_dir,
    ):
        resolve_repo_path(path).mkdir(parents=True, exist_ok=True)
