import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_phase4_smoke_runs_offline_without_frontend_build(tmp_path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "scripts/smoke_phase4.py",
            "--offline",
            "--tmp-dir",
            str(tmp_path / "phase4-smoke"),
            "--skip-frontend-build",
        ],
        check=False,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "[PASS] Phase 4 smoke completed" in result.stdout
    assert "manifest_statuses=SUCCESS,FAILED,BLOCKED" in result.stdout
    assert "before_after_status=IMPROVED" in result.stdout
