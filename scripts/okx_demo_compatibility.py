#!/usr/bin/env python3
"""Offline-first compatibility diagnostic for OKX Demo SWAP (Issue #444)."""

import argparse
import json
import os
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_PATH = REPO_ROOT / "backend"
if str(BACKEND_PATH) not in sys.path:
    sys.path.insert(0, str(BACKEND_PATH))

from app.spikes.okx_demo_compatibility import (  # noqa: E402
    detect_versions,
    exit_code_for_status,
    load_target,
    run_diagnostics,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate the OKX_DEMO integration contract. Default mode performs no network calls "
            "and never prints credential values."
        )
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=REPO_ROOT / "config" / "compatibility" / "okx_demo_contract.json",
    )
    parser.add_argument("--freqtrade-bin", default=os.environ.get("FREQTRADE_BINARY", "freqtrade"))
    parser.add_argument("--python-bin", default=sys.executable)
    parser.add_argument("--agent-bin", default="okx")
    parser.add_argument(
        "--probe-public-rest",
        action="store_true",
        help="Perform one unauthenticated public SWAP instrument request; no private API call.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        target = load_target(args.target)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "FAILED", "error": str(exc)}, ensure_ascii=False))
        return 1
    report = run_diagnostics(
        environ=os.environ,
        target=target,
        versions=detect_versions(
            freqtrade_binary=args.freqtrade_bin,
            python_binary=args.python_bin,
            agent_binary=args.agent_bin,
        ),
        probe_public=args.probe_public_rest,
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return exit_code_for_status(report.status)


if __name__ == "__main__":
    raise SystemExit(main())
