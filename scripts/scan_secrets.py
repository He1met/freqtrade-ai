#!/usr/bin/env python3
"""Repo-local secret scanning gate for Phase 7.

The scanner reports only path, line number, key name, and rule id. It never
prints matched values.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_PATH = REPO_ROOT / "backend"
if str(BACKEND_PATH) not in sys.path:
    sys.path.insert(0, str(BACKEND_PATH))

from app.services.secret_scanning import (  # noqa: E402
    format_secret_scan_report,
    scan_repo_for_secrets,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Scan repo-local tracked code, config, docs, fixture, and report paths "
            "for secret-shaped values."
        )
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT,
        help="Repository root to scan. Defaults to the parent of this script.",
    )
    parser.add_argument(
        "--path",
        dest="paths",
        action="append",
        default=None,
        help="Path to scan relative to repo root. May be passed multiple times.",
    )
    parser.add_argument(
        "--include-untracked",
        action="store_true",
        help="Scan matching files on disk instead of limiting to git-tracked files.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a JSON report without matched values.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = scan_repo_for_secrets(
        repo_root=args.repo_root,
        scan_paths=args.paths,
        tracked_only=not args.include_untracked,
    )
    if args.json:
        print(report.to_json())
    else:
        print(format_secret_scan_report(report))
    return 1 if report.findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
