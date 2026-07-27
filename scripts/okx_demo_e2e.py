#!/usr/bin/env python3
"""Run the network-free OKX Demo E2E framework.

The controlled-real adapter intentionally arrives through the normal runtime
pipeline in issues #449/#450. This script never loads exchange credentials and
never contains an HTTP/order writer.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from uuid import uuid4


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.services.okx_demo_e2e import (
    AcceptanceMode,
    DeterministicOfflineGateway,
    run_acceptance,
)


class MissingNormalPipelineGateway:
    gateway_kind = "NORMAL_PIPELINE"

    def preflight(self, mode):
        from app.services.okx_demo_e2e import Preflight

        return Preflight(
            ready=False,
            blockers=(
                "NORMAL_PIPELINE_GATEWAY_NOT_INTEGRATED",
                "ISSUES_449_AND_450_REQUIRED",
            ),
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=("offline-ci", "controlled-real"),
        default="offline-ci",
    )
    parser.add_argument(
        "--allow-real-demo",
        action="store_true",
        help="authorize controlled Demo orchestration; never authorizes live funds",
    )
    parser.add_argument("--artifact", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    mode = (
        AcceptanceMode.CONTROLLED_REAL
        if args.mode == "controlled-real"
        else AcceptanceMode.OFFLINE_CI
    )
    gateway = (
        MissingNormalPipelineGateway()
        if mode == AcceptanceMode.CONTROLLED_REAL
        else DeterministicOfflineGateway()
    )
    artifact_path = args.artifact
    if artifact_path is None and mode == AcceptanceMode.CONTROLLED_REAL:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        artifact_path = (
            Path.home()
            / ".freqtrade-ai"
            / "runtime"
            / "okx-demo-e2e"
            / "controlled-real-{}-{}.json".format(stamp, uuid4().hex)
        )
    report = run_acceptance(
        gateway,
        mode=mode,
        allow_real_demo=args.allow_real_demo,
        artifact_path=artifact_path,
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return {
        "PASSED": 0,
        "FAILED": 1,
        "NOT_RUN": 2,
        "BLOCKED": 2,
        "DRIFTED": 3,
        "RECOVERY_REQUIRED": 3,
    }[report.status]


if __name__ == "__main__":
    raise SystemExit(main())
