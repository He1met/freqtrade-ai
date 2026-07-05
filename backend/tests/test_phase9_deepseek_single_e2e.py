from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from app.db.session import create_database_engine, create_session_factory
from app.models import StrategyGenerationRun


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_deepseek_single_e2e_blocks_without_explicit_real_call() -> None:
    database_path = Path("/tmp") / f"freqtrade-ai-pytest-phase9-deepseek-{uuid4().hex}.sqlite"
    tmp_dir = Path("/tmp") / f"freqtrade-ai-pytest-phase9-deepseek-{uuid4().hex}"
    database_url = f"sqlite+pysqlite:///{database_path}"
    test_secret = "test-secret-value"
    env = {
        **os.environ,
        "DEEPSEEK_API_KEY": test_secret,
        "STRATEGY_BLUEPRINT_MODEL": "deepseek-v4-pro",
    }

    try:
        result = subprocess.run(
            [
                sys.executable,
                "scripts/phase9_deepseek_single_e2e.py",
                "--database-url",
                database_url,
                "--environment",
                "phase9",
                "--tmp-dir",
                str(tmp_dir),
                "--json",
            ],
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        assert result.returncode == 0
        assert test_secret not in result.stdout
        payload = json.loads(result.stdout)

        assert payload["status"] == "BLOCKED"
        assert payload["can_accept_as_real_run"] is False
        assert payload["provider"]["name"] == "deepseek"
        assert payload["provider"]["credential_env"] == "DEEPSEEK_API_KEY"
        assert payload["provider"]["credential_env_present"] is True
        assert payload["provider"]["credential_values_recorded"] is False
        assert payload["execution"]["allow_real_call"] is False
        assert payload["execution"]["real_call_attempted"] is False
        assert payload["generation_run"]["provider"] == "deepseek"
        assert payload["generation_run"]["status"] == "failed"
        assert payload["generation_run"]["data_source"]["source_type"] == "database"
        assert payload["generation_run"]["data_source"]["core_data"] is True
        assert payload["db_counts"] == {
            "strategy_generation_runs": 1,
            "strategies": 0,
            "strategy_versions": 0,
            "backtest_runs": 0,
            "backtest_tasks": 0,
            "backtest_results": 0,
            "strategy_scores": 0,
        }
        assert Path(payload["report_path"]).exists()
        assert test_secret not in Path(payload["report_path"]).read_text(encoding="utf-8")

        engine = create_database_engine(database_url)
        session_factory = create_session_factory(engine)
        with session_factory() as session:
            runs = session.scalars(select(StrategyGenerationRun)).all()
            assert len(runs) == 1
            assert runs[0].provider == "deepseek"
            assert runs[0].status == "failed"
            assert runs[0].failed_count == 1
            assert runs[0].params_snapshot["real_call_authorized"] is False
            assert runs[0].params_snapshot["real_call_attempted"] is False
            assert runs[0].params_snapshot["credential_env_present"] is True
            assert test_secret not in json.dumps(runs[0].params_snapshot)
            assert test_secret not in (runs[0].error_message or "")
    finally:
        database_path.unlink(missing_ok=True)
        shutil.rmtree(tmp_dir, ignore_errors=True)
