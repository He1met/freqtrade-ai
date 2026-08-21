#!/usr/bin/env python3
"""Secret-free process boundary for the canonical long-lived Demo runtime image."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
import signal
import sys
import time


CONTRACT = "canonical-v13-runtime-container-v1"
CAPABILITY = {
    "allow_real_funds": False,
    "demo_only": True,
    "order_submission": "DISABLED",
    "research_executor_capability": False,
    "runtime_class": "LONG_LIVED_TRADING_RUNTIME",
}
_STOP = False


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()


def _envelope(status: str, reason_code: str) -> dict[str, object]:
    payload = {"contract": CONTRACT, "status": status, "reason_code": reason_code, "capability": CAPABILITY}
    return {**payload, "receipt_digest": sha256(_canonical(payload)).hexdigest()}


def _emit(value: object) -> None:
    sys.stdout.buffer.write(_canonical(value) + b"\n")
    sys.stdout.buffer.flush()


def _request_stop(_signum: int, _frame: object) -> None:
    global _STOP
    _STOP = True


def preflight() -> int:
    forbidden_fragments = (
        "API_KEY",
        "CREDENTIAL",
        "DATABASE_URL",
        "PASSWORD",
        "PASSPHRASE",
        "PRIVATE_KEY",
        "SECRET",
    )
    if os.getuid() == 0 or any(
        fragment in key.upper()
        for key in os.environ
        for fragment in forbidden_fragments
    ):
        _emit(_envelope("BLOCKED", "RUNTIME_CONTAINER_SECURITY_DRIFT"))
        return 2
    _emit(_envelope("READY", "RUNTIME_IMAGE_PREFLIGHT_ACCEPTED"))
    return 0


def health() -> int:
    _emit(_envelope("HEALTHY", "RUNTIME_CONTAINER_ALIVE"))
    return 0


def serve() -> int:
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    _emit(_envelope("HEALTHY", "NO_ORDER_SOAK_SIGNAL_EVALUATION_DISABLED"))
    while not _STOP:
        time.sleep(1)
    _emit(_envelope("STOPPED", "RUNTIME_CONTAINER_STOP_SIGNAL"))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preflight", "health", "serve"))
    command = parser.parse_args(argv).command
    return {"preflight": preflight, "health": health, "serve": serve}[command]()


if __name__ == "__main__":
    raise SystemExit(main())
