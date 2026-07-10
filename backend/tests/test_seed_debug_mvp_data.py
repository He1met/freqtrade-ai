import importlib.util
from pathlib import Path

from sqlalchemy import inspect


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "seed_debug_mvp_data.py"


def load_seed_module():
    spec = importlib.util.spec_from_file_location("seed_debug_mvp_data", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_seed_database_initializes_core_schema(tmp_path, monkeypatch):
    monkeypatch.setenv("FREQTRADE_AI_DEBUG_SEED_REEXEC", "1")
    module = load_seed_module()
    database_path = tmp_path / "debug.sqlite"
    database_url = f"sqlite+pysqlite:///{database_path}"

    row_count = module.seed_database(database_url)

    engine = module.create_database_engine(database_url)
    table_names = set(inspect(engine).get_table_names())
    assert row_count > 0
    assert {
        "debug_mvp_seed_payloads",
        "strategies",
        "strategy_versions",
        "strategy_generation_runs",
        "backtest_runs",
        "backtest_tasks",
        "backtest_results",
        "strategy_scores",
    } <= table_names
