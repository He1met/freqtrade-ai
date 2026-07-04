#!/usr/bin/env python3
"""Run the Phase 5 dry-run readiness preflight without starting dry-run."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_PATH = REPO_ROOT / "backend"
VENV_PYTHON = BACKEND_PATH / ".venv" / "bin" / "python"
if (
    os.environ.get("FREQTRADE_AI_PHASE5_PREFLIGHT_REEXEC") != "1"
    and VENV_PYTHON.exists()
    and Path(sys.executable).absolute() != VENV_PYTHON
):
    os.environ["FREQTRADE_AI_PHASE5_PREFLIGHT_REEXEC"] = "1"
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv])

if str(BACKEND_PATH) not in sys.path:
    sys.path.insert(0, str(BACKEND_PATH))

from app.spikes.phase5_dry_run_preflight import (  # noqa: E402
    DEFAULT_OPTIONAL_ENV_VARS,
    DEFAULT_REQUIRED_ENV_VARS,
    DryRunPreflightConfig,
    exit_code_for_status,
    run_preflight,
)


def parse_env_names(raw: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if raw is None:
        return default
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Check local Freqtrade dry-run prerequisites without starting "
            "dry-run, connecting to an exchange, or printing secrets."
        )
    )
    parser.add_argument(
        "--tmp-dir",
        type=Path,
        default=Path("/tmp/freqtrade-ai-phase5-preflight"),
        help="Temporary workspace used only for preflight report/config-dir checks.",
    )
    parser.add_argument(
        "--report-path",
        type=Path,
        default=Path("reports/spikes/phase5_dry_run_preflight_latest.md"),
        help="Markdown report path. The default reports directory is ignored by git.",
    )
    parser.add_argument(
        "--user-data-dir",
        type=Path,
        default=Path("user_data"),
        help="Existing local Freqtrade user_data directory.",
    )
    parser.add_argument(
        "--freqtrade-bin",
        default=os.environ.get("FREQTRADE_BIN"),
        help="Optional explicit freqtrade binary path. Defaults to PATH/common local venvs.",
    )
    parser.add_argument(
        "--required-env",
        default=None,
        help=(
            "Comma-separated required ENV variable names. Defaults to the Phase 5 "
            "dry-run readiness variables."
        ),
    )
    parser.add_argument(
        "--optional-env",
        default=None,
        help="Comma-separated optional ENV variable names.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = run_preflight(
        DryRunPreflightConfig(
            tmp_dir=args.tmp_dir,
            report_path=args.report_path,
            user_data_dir=args.user_data_dir,
            freqtrade_binary=args.freqtrade_bin,
            required_env_vars=parse_env_names(args.required_env, DEFAULT_REQUIRED_ENV_VARS),
            optional_env_vars=parse_env_names(args.optional_env, DEFAULT_OPTIONAL_ENV_VARS),
        )
    )
    print(f"[{report.status}] report={report.report_path}")
    for blocker in report.blockers:
        print(f"[BLOCKED] {blocker}")
    for failure in report.failures:
        print(f"[FAIL] {failure}")
    return exit_code_for_status(report.status)


if __name__ == "__main__":
    raise SystemExit(main())
