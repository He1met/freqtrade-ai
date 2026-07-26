from functools import lru_cache
from pathlib import Path
from typing import Optional
import os

import yaml
from pydantic import BaseModel, Field

from app.core.execution_target import (
    ExecutionTargetManifest,
    parse_execution_target_manifest,
)


REPO_ROOT = Path(__file__).resolve().parents[3]
TEST_DISABLE_ENV_FILE_ENV = "FREQTRADE_AI_TEST_DISABLE_ENV_FILE"


class Settings(BaseModel):
    app_name: str = "freqtrade-ai"
    env: str = "dev"
    database_enabled: bool = True
    database_url: str = "postgresql+psycopg://freqtrade:change_me@localhost:5432/freqtrade_ai"
    canonical_repo_root: Path = Field(default=Path("~/Developer/Freqtrade Ai"))
    historical_read_only_roots: list[Path] = Field(default_factory=list)
    freqtrade_user_data: Path = Field(default=Path("./user_data"))
    strategy_output_dir: Path = Field(default=Path("./user_data/strategies/generated"))
    market_data_dir: Path = Field(default=Path("./user_data/data"))
    backtest_result_dir: Path = Field(default=Path("./reports/backtests"))
    log_dir: Path = Field(default=Path("./logs"))
    tmp_freqtrade_config_dir: Path = Field(default=Path("./tmp/freqtrade_configs"))
    max_parallel_backtests: int = 1
    task_poll_interval_seconds: int = 5
    frequi_enabled: bool = False
    frequi_url: Optional[str] = None
    frequi_environment_label: str = "local-dry-run"
    allow_live_trading: bool = False
    allow_dry_run_trading: bool = False
    allow_controlled_dry_run_process: bool = False
    execution_target_manifest: ExecutionTargetManifest


def load_env_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_app_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as file:
        return yaml.safe_load(file) or {}


@lru_cache
def get_settings() -> Settings:
    if os.getenv(TEST_DISABLE_ENV_FILE_ENV) != "1":
        load_env_file(REPO_ROOT / ".env")
    app_config = load_app_yaml(REPO_ROOT / "config" / "app.yaml")

    app_section = app_config.get("app", {})
    paths_section = app_config.get("paths", {})
    database_section = app_config.get("database", {})
    worker_section = app_config.get("worker", {})
    frequi_section = app_config.get("frequi", {})
    security_section = app_config.get("security", {})
    execution_section = app_config.get("execution")

    return Settings(
        app_name=app_section.get("name", "freqtrade-ai"),
        env=os.getenv("APP_ENV", app_section.get("env", "dev")),
        database_enabled=database_section.get("enabled", True),
        database_url=os.getenv(
            "DATABASE_URL",
            "postgresql+psycopg://freqtrade:change_me@localhost:5432/freqtrade_ai",
        ),
        canonical_repo_root=Path(
            os.getenv(
                "FREQTRADE_AI_CANONICAL_REPO_ROOT",
                paths_section.get("canonical_repo_root", "~/Developer/Freqtrade Ai"),
            )
        ),
        historical_read_only_roots=[
            Path(path)
            for path in paths_section.get("historical_read_only_roots", [])
            if isinstance(path, str) and path
        ],
        freqtrade_user_data=Path(paths_section.get("freqtrade_user_data", "./user_data")),
        strategy_output_dir=Path(paths_section.get("strategy_output_dir", "./user_data/strategies/generated")),
        market_data_dir=Path(paths_section.get("market_data_dir", "./user_data/data")),
        backtest_result_dir=Path(paths_section.get("backtest_result_dir", "./reports/backtests")),
        log_dir=Path(paths_section.get("log_dir", "./logs")),
        tmp_freqtrade_config_dir=Path(paths_section.get("tmp_freqtrade_config_dir", "./tmp/freqtrade_configs")),
        max_parallel_backtests=worker_section.get("max_parallel_backtests", 1),
        task_poll_interval_seconds=worker_section.get("task_poll_interval_seconds", 5),
        frequi_enabled=frequi_section.get("enabled", False),
        frequi_url=os.getenv("FREQUI_URL", frequi_section.get("url") or None),
        frequi_environment_label=os.getenv(
            "FREQUI_ENVIRONMENT_LABEL",
            frequi_section.get("environment_label", "local-dry-run"),
        ),
        allow_live_trading=security_section.get("allow_live_trading", False),
        allow_dry_run_trading=security_section.get("allow_dry_run_trading", False),
        allow_controlled_dry_run_process=security_section.get("allow_controlled_dry_run_process", False),
        execution_target_manifest=parse_execution_target_manifest(execution_section),
    )
