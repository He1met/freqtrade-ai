#!/usr/bin/env python3
"""Seed local-only backend API payloads for frontend debugging."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_PATH = REPO_ROOT / "backend"
DEFAULT_SQLITE_PATH = Path("/tmp/freqtrade-ai-debug.sqlite")
VENV_PYTHON = BACKEND_PATH / ".venv" / "bin" / "python"

if (
    os.environ.get("FREQTRADE_AI_DEBUG_SEED_REEXEC") != "1"
    and VENV_PYTHON.exists()
    and Path(sys.executable).absolute() != VENV_PYTHON
):
    os.environ["FREQTRADE_AI_DEBUG_SEED_REEXEC"] = "1"
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv])

if str(BACKEND_PATH) not in sys.path:
    sys.path.insert(0, str(BACKEND_PATH))

from app.db.session import create_database_engine, create_session_factory, redact_database_url  # noqa: E402
from app.models import Base  # noqa: E402
from app.repositories.debug_mvp_seed_data import DebugMvpSeedDataRepository  # noqa: E402
from app.services.debug_mvp_seed_data import build_debug_mvp_seed_payloads  # noqa: E402


def default_database_url() -> str:
    return os.environ.get("DATABASE_URL") or f"sqlite+pysqlite:///{DEFAULT_SQLITE_PATH}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Insert deterministic fake MVP/debug data into a local database so the "
            "frontend can render backend API responses during local debugging."
        )
    )
    parser.add_argument(
        "--database-url",
        default=default_database_url(),
        help=(
            "Target SQLAlchemy database URL. Defaults to DATABASE_URL when set, "
            "otherwise sqlite+pysqlite:////tmp/freqtrade-ai-debug.sqlite."
        ),
    )
    return parser.parse_args()


def seed_database(database_url: str) -> int:
    engine = create_database_engine(database_url)
    Base.metadata.create_all(bind=engine)
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        return DebugMvpSeedDataRepository(session).upsert_payloads(build_debug_mvp_seed_payloads())


def main() -> int:
    args = parse_args()
    row_count = seed_database(args.database_url)
    print("Seeded frontend debug MVP API payloads.")
    print(f"Database: {redact_database_url(args.database_url)}")
    print(f"Payload rows: {row_count}")
    print("Start backend with the same DATABASE_URL, then open the Vite frontend.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
